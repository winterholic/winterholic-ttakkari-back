"""명시적 서술 패턴에서 개념 간 관계를 뽑는 결정적 추출기(비용 0).

"디플로이먼트는 레플리카셋을 관리한다", "Deployment manages ReplicaSet" 처럼 한 문장 안에서 두 개념과 관계 동사가
정해진 모양으로 붙어 있을 때만 관계 후보를 만든다. 헤딩 계층에서 유추한 구조 관계(part_of)와 달리 문장이 직접 말한
관계라서 basis="explicit" 로 표시하고, 인용 검증을 통과하면 persist 단계가 active 로 승격한다(리드 결정 1).

보수성 원칙:
- 양쪽 끝점은 이미 아는 개념(시드 사전 + 이번 문서의 L0/L2 후보)의 표기만 인정한다. 새 개념을 지어내지 않는다.
- 두 끝점 사이에 다른 개념 표기가 끼면 인접 쌍이 아니므로 만들지 않는다(정규식이 두 표기를 직접 이어 붙인다).
- 부정·조건·추측 서술(하지 않는다, not, 할 수 있다)은 동사 패턴에 없으므로 자연히 걸러진다.
- 인용은 표기 A 의 시작부터 동사 어미 끝까지의 정확한 부분 문자열이고, 오프셋도 함께 넘겨 검증기가 그대로 대조한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ttakkari.common.entity_norm import norm_name
from ttakkari.kb_models import KbPassage
from ttakkari.kb_pipeline.alias_dictionary import AMBIGUOUS_ALIASES, MIN_ALIAS_CHARS
from ttakkari.kb_pipeline.quote_verifier import CandidateMention, CandidateRelation

BASIS_EXPLICIT = "explicit"

# 한국어: A(은|는|이|가) [부사] B(조사) 동사. 조사는 관계마다 다르다.
_KO_ADVERB = r"(?:(?:항상|직접|자동으로|내부적으로|선언적으로|주로)\s+)?"
_KO_ENDING = r"(?:한다|합니다|하며|하고|하는|해요|함)"
_KO_RULES: tuple[tuple[str, str, str], ...] = (
    ("manages", r"(?:을|를)\s*", r"(?:관리|제어|운영)" + _KO_ENDING),
    ("depends_on", r"(?:에|에게)\s*", r"의존" + _KO_ENDING),
    ("depends_on", r"(?:을|를)\s*", r"필요로\s*(?:한다|합니다|하며|하고|함)"),
    ("uses", r"(?:을|를)\s*", r"(?:사용|이용|활용)" + _KO_ENDING),
    ("implements", r"(?:을|를)\s*", r"구현" + _KO_ENDING),
    ("part_of", r"(?:의|에)\s*", r"(?:일부|구성\s*요소|하위\s*리소스)(?:이다|입니다)"),
    ("part_of", r"(?:에)\s*", r"(?:속한다|속합니다|속하며)"),
    ("alternative_to", r"(?:의)\s*", r"대안(?:이다|입니다)"),
    ("alternative_to", r"(?:을|를)\s*", r"대체" + _KO_ENDING),
)
_EN_ARTICLE = r"(?:(?:the|a|an)\s+)?"
_EN_RULES: tuple[tuple[str, str], ...] = (
    ("manages", r"(?:manages|controls|operates)\s+"),
    ("depends_on", r"(?:depends\s+on|relies\s+on|requires)\s+"),
    ("uses", r"(?:uses|utilizes|leverages)\s+"),
    ("implements", r"implements\s+"),
    ("part_of", r"(?:is\s+(?:a\s+)?part\s+of|belongs\s+to|is\s+a\s+component\s+of)\s+"),
    ("alternative_to", r"(?:is\s+an\s+alternative\s+to|replaces)\s+"),
)
_SENTENCE = re.compile(r"[^.!?\n。]+[.!?。]?")
_WORD = r"A-Za-z0-9_"


@dataclass
class ExplicitRelationResult:
    relations: list[CandidateRelation] = field(default_factory=list)
    mentions: list[CandidateMention] = field(default_factory=list)
    # 끝점 중 시드 사전에만 있고 이번 후보 목록에는 없는 개념(호출자가 후보로 올린다)
    seed_endpoints: set[str] = field(default_factory=set)


def build_label_index(entities: dict[str, dict], extra: dict[str, list[str]] | None = None) -> dict[str, str]:
    """표기(원문) -> 정규 이름. 시드 사전의 정규 이름·별칭과 이번 문서 후보의 이름·별칭을 합친다."""
    index: dict[str, str] = {}
    sources: list[tuple[str, list[str]]] = [(name, list(meta.get("aliases", []))) for name, meta in entities.items()]
    sources.extend((name, aliases) for name, aliases in (extra or {}).items())
    for canonical, aliases in sources:
        for label in [canonical, *aliases]:
            label = (label or "").strip()
            normalized = norm_name(label)
            if len(normalized) < MIN_ALIAS_CHARS or normalized in AMBIGUOUS_ALIASES or " " in label:
                continue
            index.setdefault(label, canonical)
    return index


def _compile(index: dict[str, str]):
    labels = sorted(index, key=len, reverse=True)
    if not labels:
        return None
    alt = "|".join(re.escape(label) for label in labels)
    # 앞: 단어 중간에서 시작하는 표기(예: 하위디플로이먼트)를 거른다. 뒤: ASCII 표기가 더 긴 단어의 앞부분이면 거른다.
    label = rf"(?<![{_WORD}가-힣])(?:{alt})(?![{_WORD}])"
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for relation_type, particle, verb in _KO_RULES:
        patterns.append((
            relation_type,
            re.compile(rf"(?P<a>{label})(?:은|는|이|가)\s+{_KO_ADVERB}(?P<b>{label}){particle}{verb}", re.IGNORECASE),
        ))
    for relation_type, verb in _EN_RULES:
        patterns.append((
            relation_type,
            re.compile(rf"(?P<a>{label})\s+{verb}{_EN_ARTICLE}(?P<b>{label})", re.IGNORECASE),
        ))
    return patterns


def extract_explicit_relations(
    passages: list[KbPassage],
    label_index: dict[str, str],
    *,
    known_candidates: set[str] | None = None,
) -> ExplicitRelationResult:
    """패시지마다 문장 단위로 관계 패턴을 찾는다. 문장을 가로지르지 않는다."""
    result = ExplicitRelationResult()
    patterns = _compile(label_index)
    if not patterns:
        return result
    lowered_index = {label.lower(): canonical for label, canonical in label_index.items()}
    seen: set[tuple[object, int, int, str]] = set()
    for passage in passages:
        body = passage.body or ""
        if not body.strip():
            continue
        for sentence in _SENTENCE.finditer(body):
            text = sentence.group(0)
            if not text.strip():
                continue
            base = sentence.start()
            for relation_type, pattern in patterns:
                for match in pattern.finditer(text):
                    a_text, b_text = match.group("a"), match.group("b")
                    source = lowered_index.get(a_text.lower())
                    target = lowered_index.get(b_text.lower())
                    if not source or not target or source == target:
                        continue
                    start = base + match.start("a")
                    end = base + match.end()
                    key = (passage.id, start, end, relation_type)
                    if key in seen:
                        continue
                    seen.add(key)
                    result.relations.append(
                        CandidateRelation(
                            source_name=source,
                            target_name=target,
                            relation_type=relation_type,
                            weight=1.0,
                            status="candidate",
                            basis=BASIS_EXPLICIT,
                            evidence_quote=body[start:end],
                            passage_id=passage.id,
                            span_start=start,
                            span_end=end,
                        )
                    )
                    for name, label_text, off in ((source, a_text, match.start("a")), (target, b_text, match.start("b"))):
                        m_start = base + off
                        result.mentions.append(
                            CandidateMention(
                                passage_id=passage.id,
                                entity_name=name,
                                role="mentioned",
                                salience=0.6,
                                evidence_quote=label_text,
                                span_start=m_start,
                                span_end=m_start + len(label_text),
                            )
                        )
                        if known_candidates is not None and name not in known_candidates:
                            result.seed_endpoints.add(name)
    return result
