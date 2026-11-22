"""제작 의도 계약 v2(IntentSpec v2). 계약 문서: docs/design/creative-studio-v2-intent-v2.md

v1(`intent.py`)은 그대로 두고 읽을 때 v2로 승격한다. 기존 계획·매칭 경로는 계속 v1을 받는다.
v2가 v1과 다른 점은 네 가지다.
- 출처(origin)가 "누가 이 값을 정했는가"를 뜻한다. 사용자가 구조화 입력이나 확인 동작으로 정한 값만
  user_confirmed다. AI 해석은 인용이 있어도 ai_inferred다.
- 충돌은 한쪽을 버리지 않고 후보를 모두 남긴 채 open으로 기록한다. 해결 전에는 유효값을 비워 둔다.
- 카탈로그 밖 조건과 아직 받지 못하는 참조 자산은 unsupported로 남긴다.
- revision과 history로 의도 자체의 변경 이력을 남긴다.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ttakkari.creative.v2.capabilities import Strict
from ttakkari.creative.v2.catalog_data import CATALOG_NAMES, ENTRIES
from ttakkari.creative.v2.duration import MAX_SECONDS, MIN_SECONDS, DurationRequest
from ttakkari.creative.v2.intent import (
    SINGLE_PRIMARY,
    InfoItem,
    IntentSpec,
    Span,
    hint_intent,
)

Origin = Literal["user_confirmed", "user_free_text", "ai_inferred", "default"]
Strength = Literal["required", "preferred"]
Actor = Literal["user", "ai", "system", "migration"]
Aspect = Literal["16:9", "9:16", "1:1"]

# C01~C12에 없는 분위기 축. 카탈로그를 늘리면 매칭·레시피 판정까지 바뀌므로 여기서는 노출 규칙에만 쓴다.
TONES: dict[str, dict[str, Any]] = {
    "tone.horror": {
        "label": "공포",
        "hints": ("공포", "호러", "horror", "무서운", "오싹"),
    },
    "tone.action": {"label": "액션", "hints": ("액션", "action", "격투", "추격")},
    "tone.emotional": {"label": "감성", "hints": ("감성", "서정", "잔잔한", "애틋")},
    "tone.comedy": {"label": "코미디", "hints": ("코미디", "유머", "웃긴", "개그")},
    "tone.epic": {"label": "웅장", "hints": ("웅장", "장엄", "epic")},
    "tone.calm": {"label": "차분", "hints": ("차분", "평온", "힐링")},
}
# 사용자 문장이 콘텐츠 유형을 직접 말하지 않았지만 주제로 유형을 추정할 수 있는 단서. 추정이라 ai_inferred다.
DOMAIN_CUES: dict[str, tuple[str, ...]] = {
    "C01.tech_explainer": (
        "Kafka",
        "Producer",
        "Broker",
        "Consumer",
        "API",
        "데이터베이스",
        "알고리즘",
        "서버",
        "프로토콜",
        "쿠버네티스",
    ),
}
DIMENSIONS: tuple[str, ...] = (*CATALOG_NAMES, "tone")
# 희망 길이(target)에서 이 범위를 넘게 다르면 충돌로 본다. 엄격(strict)은 허용 오차가 없다.
TARGET_TOLERANCE = 0.15


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dimension_of(condition_id: str) -> str | None:
    if condition_id in ENTRIES:
        return ENTRIES[condition_id].catalog
    if condition_id in TONES:
        return "tone"
    return None


def label_of(condition_id: str) -> str:
    if condition_id in ENTRIES:
        return ENTRIES[condition_id].name
    if condition_id in TONES:
        return TONES[condition_id]["label"]
    return condition_id


class Confirmation(Strict):
    at: str = Field(max_length=40)
    by: Actor = "user"
    revision: int = Field(ge=1)


class Condition(Strict):
    """구조화 조건 하나. id는 카탈로그 항목(C03.japanese_anime) 또는 톤(tone.horror)이다."""

    id: str = Field(max_length=80)
    dimension: str = Field(max_length=10)
    role: Literal["primary", "secondary"] = "secondary"
    strength: Strength = "preferred"
    origin: Origin = "ai_inferred"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[Span] = Field(default_factory=list, max_length=5)
    confirmation: Confirmation | None = None
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _origin_rules(self):
        if dimension_of(self.id) != self.dimension:
            raise ValueError(f"조건 {self.id}의 차원이 맞지 않습니다: {self.dimension}")
        if self.origin == "user_confirmed" and self.confirmation is None:
            raise ValueError(f"사용자 확정 조건에는 확정 기록이 필요합니다: {self.id}")
        if self.origin != "user_confirmed" and self.confirmation is not None:
            raise ValueError(f"확정 기록은 사용자 확정 조건에만 둡니다: {self.id}")
        if self.strength == "required" and self.origin in ("ai_inferred", "default"):
            raise ValueError(
                f"AI 추론이나 기본값은 필수(required)가 될 수 없습니다: {self.id}"
            )
        if self.origin == "user_free_text" and not self.evidence:
            raise ValueError(f"자유 입력 출처에는 브리프 인용이 필요합니다: {self.id}")
        return self


class DurationPolicy(Strict):
    """길이 정책. auto=자동, target=근접 목표, strict=위반 시 명시 처리."""

    mode: Literal["auto", "target", "strict"] = "auto"
    seconds: float | None = None
    platform: str | None = Field(default=None, max_length=40)
    origin: Origin = "default"
    evidence: list[Span] = Field(default_factory=list, max_length=3)

    @field_validator("seconds", mode="before")
    @classmethod
    def _no_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("숫자를 입력해 주세요.")  # noqa: TRY004 - pydantic 검증 오류로 바뀐다
        return value

    @model_validator(mode="after")
    def _check(self):
        if self.mode == "auto":
            if self.seconds is not None:
                raise ValueError("자동 길이에는 초를 지정할 수 없습니다.")
        elif self.seconds is None or not MIN_SECONDS <= self.seconds <= MAX_SECONDS:
            raise ValueError(
                f"길이는 {MIN_SECONDS:g}~{MAX_SECONDS:g}초 범위여야 합니다."
            )
        return self

    def to_request(self) -> DurationRequest:
        """기존 길이 계산기(duration.plan_duration)가 받는 v1 요청으로 바꾼다."""
        if self.mode == "auto":
            return DurationRequest()
        return DurationRequest(
            mode="preferred" if self.mode == "target" else "exact",
            seconds=self.seconds,
        )


class AspectChoice(Strict):
    value: Aspect
    origin: Origin = "default"
    evidence: list[Span] = Field(default_factory=list, max_length=3)


class ReferenceAsset(Strict):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["image", "video", "audio", "document"]
    role: Literal["style", "character", "music", "content", "layout", "motion"]
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    name: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=400)
    origin: Origin = "user_confirmed"
    # 서버가 실제로 이 자산을 제작에 쓸 수 있는지. 받기만 하고 못 쓰면 unsupported로 함께 기록한다.
    usable: bool = False


class ConflictCandidate(Strict):
    value: str = Field(max_length=120)
    label: str = Field(default="", max_length=120)
    origin: Origin
    strength: Strength = "preferred"
    evidence: list[Span] = Field(default_factory=list, max_length=3)


class ConflictResolution(Strict):
    choice: str = Field(max_length=120)
    by: Actor = "user"
    revision: int = Field(ge=1)
    note: str = Field(default="", max_length=300)


class IntentConflict(Strict):
    id: str = Field(max_length=200)
    kind: Literal["duration", "aspect", "catalog_conflict", "single_primary", "rule"]
    dimension: str = Field(max_length=20)
    message: str = Field(max_length=400)
    candidates: list[ConflictCandidate] = Field(min_length=1, max_length=8)
    status: Literal["open", "resolved"] = "open"
    # open이면서 blocking이면 계획 생성 전에 사용자가 골라야 한다.
    blocking: bool = True
    resolution: ConflictResolution | None = None


class UnsupportedItem(Strict):
    id: str = Field(max_length=120)
    subject: str = Field(max_length=200)
    reason: Literal[
        "not_in_catalog", "unknown_tone", "reference_not_ingested", "rule_blocked"
    ]
    message: str = Field(max_length=400)
    alternatives: list[str] = Field(default_factory=list, max_length=6)


class Answer(Strict):
    field_id: str = Field(max_length=80)
    values: list[str] = Field(min_length=1, max_length=12)
    origin: Literal["user_confirmed"] = "user_confirmed"
    revision: int = Field(default=1, ge=1)


class HistoryEntry(Strict):
    revision: int = Field(ge=1)
    at: str = Field(max_length=40)
    actor: Actor
    action: str = Field(max_length=60)
    detail: str = Field(default="", max_length=400)


class IntentSpecV2(Strict):
    schema_version: Literal[2] = 2
    revision: int = Field(default=1, ge=1)
    brief: str = Field(default="", max_length=6000)
    conditions: list[Condition] = Field(default_factory=list, max_length=120)
    duration: DurationPolicy | None = Field(default_factory=DurationPolicy)
    aspect: AspectChoice | None = None
    info_items: list[InfoItem] = Field(default_factory=list, max_length=400)
    music_seconds: float | None = Field(default=None, gt=0.0, le=MAX_SECONDS)
    references: list[ReferenceAsset] = Field(default_factory=list, max_length=20)
    answers: list[Answer] = Field(default_factory=list, max_length=80)
    conflicts: list[IntentConflict] = Field(default_factory=list, max_length=60)
    unsupported: list[UnsupportedItem] = Field(default_factory=list, max_length=60)
    # 사용자가 지운 조건. 같은 브리프를 다시 읽어도 되살리지 않는다.
    rejected: list[str] = Field(default_factory=list, max_length=120)
    history: list[HistoryEntry] = Field(default_factory=list, max_length=400)
    migrated_from: Literal[1] | None = None

    @model_validator(mode="after")
    def _unique(self):
        ids = [c.id for c in self.conditions]
        if len(set(ids)) != len(ids):
            raise ValueError("같은 조건이 두 번 들어 있습니다.")
        if len({c.id for c in self.conflicts}) != len(self.conflicts):
            raise ValueError("충돌 ID가 중복됐습니다.")
        return self

    def condition(self, condition_id: str) -> Condition | None:
        return next((c for c in self.conditions if c.id == condition_id), None)

    @property
    def blocking_conflicts(self) -> list[IntentConflict]:
        return [c for c in self.conflicts if c.status == "open" and c.blocking]


# --- v1 읽기 호환 -------------------------------------------------------------


def _v1_origin(source: str, method: str) -> Origin:
    if source == "user_explicit":
        # v1 구조화 제출(user_structured)의 user_explicit는 사용자가 직접 낸 값이다.
        return "user_confirmed" if method == "user_structured" else "user_free_text"
    if source == "lexical_hint":
        return "user_free_text"
    if source == "default":
        return "default"
    return "ai_inferred"


def promote_v1(v1: IntentSpec, brief: str = "") -> IntentSpecV2:
    """v1 의도를 v2로 승격한다. 원본은 바꾸지 않고 바뀐 해석은 history에 남긴다."""
    at = now_iso()
    history = [
        HistoryEntry(
            revision=1,
            at=at,
            actor="migration",
            action="migrated_from_v1",
            detail=f"v1 추출 방식 {v1.extraction.method}",
        )
    ]
    conditions: list[Condition] = []
    for sel in v1.selections:
        if sel.entry_id not in ENTRIES or any(c.id == sel.entry_id for c in conditions):
            continue
        origin = _v1_origin(sel.source, v1.extraction.method)
        if origin == "user_free_text" and not sel.evidence:
            origin = "ai_inferred"
        strength: Strength = "required" if sel.strength == "hard" else "preferred"
        if strength == "required" and origin in ("ai_inferred", "default"):
            strength = "preferred"
            history.append(
                HistoryEntry(
                    revision=1,
                    at=at,
                    actor="migration",
                    action="strength_downgraded",
                    detail=f"{sel.entry_id}: AI 추론 hard 선택을 preferred로 낮춤",
                )
            )
        conditions.append(
            Condition(
                id=sel.entry_id,
                dimension=ENTRIES[sel.entry_id].catalog,
                role=sel.role,
                strength=strength,
                origin=origin,
                confidence=sel.confidence,
                evidence=sel.evidence,
                confirmation=Confirmation(at=at, by="migration", revision=1)
                if origin == "user_confirmed"
                else None,
            )
        )
    d = v1.duration
    if d.mode == "auto":
        duration = DurationPolicy()
    else:
        duration = DurationPolicy(
            mode="target" if d.mode == "preferred" else "strict",
            seconds=d.seconds,
            platform=d.platform,
            origin="user_confirmed"
            if v1.extraction.method == "user_structured"
            else "ai_inferred",
        )
    questions = [
        HistoryEntry(
            revision=1,
            at=at,
            actor="migration",
            action="legacy_open_question",
            detail=q[:400],
        )
        for q in v1.open_questions
    ]
    return IntentSpecV2(
        brief=brief,
        conditions=conditions,
        duration=duration,
        aspect=AspectChoice(
            value=v1.aspect,
            origin="user_confirmed"
            if v1.extraction.method == "user_structured"
            else "ai_inferred",
        )
        if v1.aspect
        else None,
        info_items=v1.info_items,
        music_seconds=v1.music_seconds,
        history=history + questions,
        migrated_from=1,
    )


def load_intent(doc: dict[str, Any] | None, brief: str = "") -> IntentSpecV2:
    """저장된 의도 문서를 읽는다. schema_version이 없거나 1이면 v1으로 읽어 승격한다."""
    if not doc:
        return IntentSpecV2(brief=brief)
    version = doc.get("schema_version", 1)
    if version == 2:
        spec = IntentSpecV2.model_validate(doc)
        return spec.model_copy(update={"brief": brief}) if brief else spec
    if version == 1:
        return promote_v1(IntentSpec.model_validate(doc), brief)
    raise ValueError(f"지원하지 않는 의도 schema_version입니다: {version}")


# --- 자유 입력 해석(결정적 규칙) ------------------------------------------------


def _find(hint: str, brief: str) -> str | None:
    found = re.search(re.escape(hint), brief, re.IGNORECASE)
    return found.group(0) if found else None


def _quoted(evidence, brief: str) -> bool:
    folded = brief.casefold()
    return any(span.quote.casefold() in folded for span in evidence)


def stale_text_items(spec: IntentSpecV2, brief: str, *, include_confirmed: bool = False) -> list[str]:
    """브리프가 바뀐 뒤에도 남은, 현재 브리프에 근거 인용이 없는 항목의 id.

    기본은 자유 입력·AI 해석만 본다. 이 둘은 인용이 근거의 전부라서 인용이 사라지면 이전 문장의 의도가 새 계획에
    섞인다(자연어 수정 직후 이전 의도로 계획이 만들어지는 경로). 사용자가 확정(user_confirmed)한 항목은 인용이
    사라져도 남기고 인용만 지운다(`prune_evidence`). include_confirmed=True는 계획 생성 검사용으로, 확정 항목의
    낡은 인용도 거부 사유로 센다. 인용이 처음부터 없는 AI 해석은 판정할 수 없어 남긴다.
    """
    if not brief:
        return []
    origins = ("user_free_text", "ai_inferred", *(("user_confirmed",) if include_confirmed else ()))
    stale = [
        c.id
        for c in spec.conditions
        if c.origin in origins and c.evidence and not _quoted(c.evidence, brief)
    ]
    for name, choice in (("duration", spec.duration), ("aspect", spec.aspect)):
        if (
            choice is not None
            and choice.origin in origins
            and choice.evidence
            and not _quoted(choice.evidence, brief)
        ):
            stale.append(name)
    return stale


def prune_evidence(spec: IntentSpecV2, brief: str) -> tuple[IntentSpecV2, list[str]]:
    """확정 항목에 남은, 현재 브리프에 없는 인용을 지운다. 확정은 사용자의 결정이라 항목은 유지한다."""
    if not brief:
        return spec, []
    folded = brief.casefold()
    pruned: list[str] = []

    def keep(evidence):
        return [e for e in evidence if e.quote.casefold() in folded]

    conditions = []
    for c in spec.conditions:
        if c.origin == "user_confirmed" and c.evidence and not _quoted(c.evidence, brief):
            c = c.model_copy(update={"evidence": keep(c.evidence)})
            pruned.append(c.id)
        conditions.append(c)
    update: dict[str, Any] = {"conditions": conditions}
    for name, choice in (("duration", spec.duration), ("aspect", spec.aspect)):
        if choice is not None and choice.origin == "user_confirmed" and choice.evidence and not _quoted(choice.evidence, brief):
            update[name] = choice.model_copy(update={"evidence": keep(choice.evidence)})
            pruned.append(name)
    return (spec.model_copy(update=update) if pruned else spec), pruned


_STRICT_WORDS = re.compile(r"(정확히|딱|반드시|이내|넘지\s*않)")
_ASPECT_WORDS: tuple[tuple[str, Aspect], ...] = (
    ("세로", "9:16"),
    ("9:16", "9:16"),
    ("가로", "16:9"),
    ("16:9", "16:9"),
    ("정사각", "1:1"),
    ("1:1", "1:1"),
)


class FreeTextReading(Strict):
    conditions: list[Condition]
    duration: DurationPolicy | None = None
    aspect: AspectChoice | None = None


def read_free_text(brief: str) -> FreeTextReading:
    """기존 단어 힌트 경로(hint_intent·duration_hint)를 재사용해 자유 입력을 후보로 읽는다.

    사용자가 해당 낱말을 직접 쓴 조건은 user_free_text, 주제로 추정한 조건은 ai_inferred다.
    어느 쪽도 확정이 아니다.
    """
    conditions: list[Condition] = []
    spec, hint = hint_intent(brief)
    for sel in spec.selections:
        conditions.append(
            Condition(
                id=sel.entry_id,
                dimension=ENTRIES[sel.entry_id].catalog,
                origin="user_free_text",
                confidence=0.4,
                evidence=sel.evidence,
            )
        )
    for tone_id, tone in TONES.items():
        for word in tone["hints"]:
            quote = _find(word, brief)
            if quote:
                conditions.append(
                    Condition(
                        id=tone_id,
                        dimension="tone",
                        origin="user_free_text",
                        confidence=0.5,
                        evidence=[Span(quote=quote)],
                    )
                )
                break
    seen = {c.id for c in conditions}
    for entry_id, cues in DOMAIN_CUES.items():
        if entry_id in seen:
            continue
        quotes = [q for q in (_find(c, brief) for c in cues) if q]
        if quotes:
            conditions.append(
                Condition(
                    id=entry_id,
                    dimension=ENTRIES[entry_id].catalog,
                    origin="ai_inferred",
                    confidence=0.4,
                    evidence=[Span(quote=q) for q in quotes[:3]],
                    note="주제어로 유형을 추정함. 사용자가 유형을 직접 말하지 않았다.",
                )
            )
    duration = None
    if hint:
        duration = DurationPolicy(
            mode="strict" if _STRICT_WORDS.search(brief) else "target",
            seconds=hint["seconds"],
            origin="user_free_text",
            evidence=[Span(quote=hint["quote"])],
        )
    aspect = None
    for word, value in _ASPECT_WORDS:
        quote = _find(word, brief)
        if quote:
            aspect = AspectChoice(
                value=value, origin="user_free_text", evidence=[Span(quote=quote)]
            )
            break
    return FreeTextReading(conditions=conditions, duration=duration, aspect=aspect)


# --- 병합·충돌 -------------------------------------------------------------------

_RANK: dict[str, int] = {
    "user_confirmed": 3,
    "user_free_text": 2,
    "ai_inferred": 1,
    "default": 0,
}


def _duration_label(d: DurationPolicy) -> str:
    if d.mode == "auto":
        return "자동"
    return f"{d.seconds:g}초 ({'엄격' if d.mode == 'strict' else '희망'})"


def durations_conflict(a: DurationPolicy, b: DurationPolicy) -> bool:
    if a.mode == "auto" or b.mode == "auto":
        return False
    assert a.seconds is not None and b.seconds is not None
    if "strict" in (a.mode, b.mode):
        return abs(a.seconds - b.seconds) > 1e-6
    return abs(a.seconds - b.seconds) > TARGET_TOLERANCE * max(a.seconds, b.seconds)


def duration_value(d: DurationPolicy) -> str:
    return "auto" if d.mode == "auto" else f"{d.mode}:{d.seconds:g}"


def catalog_conflicts(conditions: list[Condition]) -> list[IntentConflict]:
    """활성 조건 사이의 카탈로그 conflicts_with와 단일 주 선택 위반을 충돌로 만든다."""
    by_id = {c.id: c for c in conditions}
    out: list[IntentConflict] = []
    seen: set[tuple[str, str]] = set()
    for c in conditions:
        entry = ENTRIES.get(c.id)
        if entry is None:
            continue
        for rel in entry.relations:
            if rel.type != "conflicts_with" or rel.target not in by_id:
                continue
            pair = tuple(sorted((c.id, rel.target)))
            if pair in seen:
                continue
            seen.add(pair)
            other = by_id[rel.target]
            out.append(
                IntentConflict(
                    id=f"conflict.catalog.{pair[0]}~{pair[1]}",
                    kind="catalog_conflict",
                    dimension=entry.catalog,
                    message=f"{label_of(pair[0])}와(과) {label_of(pair[1])}는 함께 쓸 수 없습니다. 하나를 고르거나 혼합 방식을 정해 주세요.",
                    candidates=[
                        _candidate(by_id[pair[0]]),
                        _candidate(by_id[pair[1]]),
                    ],
                    blocking=rel.strength == "hard"
                    or "user_confirmed" in (c.origin, other.origin),
                )
            )
    for catalog in SINGLE_PRIMARY:
        primaries = sorted(
            (c for c in conditions if c.dimension == catalog and c.role == "primary"),
            key=lambda c: c.id,
        )
        if len(primaries) > 1:
            out.append(
                IntentConflict(
                    id=f"conflict.primary.{catalog}",
                    kind="single_primary",
                    dimension=catalog,
                    message=f"{CATALOG_NAMES[catalog]}의 주 선택은 하나여야 합니다.",
                    candidates=[_candidate(c) for c in primaries],
                )
            )
    return out


def _candidate(c: Condition) -> ConflictCandidate:
    return ConflictCandidate(
        value=c.id,
        label=label_of(c.id),
        origin=c.origin,
        strength=c.strength,
        evidence=c.evidence[:3],
    )
