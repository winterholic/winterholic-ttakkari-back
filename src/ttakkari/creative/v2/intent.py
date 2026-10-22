"""의도 추출 결과의 구조화 계약과 검증.

의도 해석은 LLM·사용자 입력이 만든 구조(IntentSpec)를 이 모듈이 타입·제약·근거로 검증하는 방식이다.
사전 단어 포함 여부로 분류하는 `hint_intent`는 입력 보조이며 항상 soft·낮은 신뢰도·확인 필요로 표시된다.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ttakkari.creative.v2.capabilities import Strict
from ttakkari.creative.v2.catalog_data import CATALOG_NAMES, ENTRIES
from ttakkari.creative.v2.duration import MAX_SECONDS, MIN_SECONDS, DurationRequest

Source = Literal["user_explicit", "llm_inferred", "lexical_hint", "default"]
# 한 작품에서 주된 축이 하나여야 하는 카탈로그. 나머지는 보조 선택이 여러 개일 수 있다.
SINGLE_PRIMARY = ("C01", "C03", "C08")


class Span(Strict):
    quote: str = Field(min_length=1, max_length=200)


class Selection(Strict):
    entry_id: str = Field(max_length=80)
    role: Literal["primary", "secondary"] = "secondary"
    strength: Literal["hard", "soft"] = "soft"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: Source = "llm_inferred"
    evidence: list[Span] = Field(default_factory=list, max_length=5)


class IntentConstraint(Strict):
    id: str = Field(min_length=1, max_length=60)
    kind: Literal[
        "duration",
        "aspect",
        "platform",
        "budget",
        "resource",
        "style",
        "content",
        "legal",
    ]
    hard: bool = False
    statement: str = Field(min_length=1, max_length=400)
    source: Source = "llm_inferred"
    evidence: list[Span] = Field(default_factory=list, max_length=5)


class InfoItem(Strict):
    """전달해야 하는 정보 단위. 길이는 이 개수와 발화에서 나오며 목표 시간에서 만들어지지 않는다."""

    id: str = Field(min_length=1, max_length=50)
    title: str = Field(default="", max_length=160)
    narration: str = Field(default="", max_length=4000)
    weight: float = Field(default=1.0, ge=0.0, le=10.0)
    essential: bool = True
    evidence: list[Span] = Field(default_factory=list, max_length=5)


class Extraction(Strict):
    method: Literal["llm", "user_structured", "lexical_hint"]
    model: str | None = Field(default=None, max_length=80)


class IntentSpec(Strict):
    schema_version: Literal[1] = 1
    selections: list[Selection] = Field(default_factory=list, max_length=80)
    constraints: list[IntentConstraint] = Field(default_factory=list, max_length=40)
    duration: DurationRequest = Field(default_factory=DurationRequest)
    aspect: Literal["16:9", "9:16", "1:1"] | None = None
    info_items: list[InfoItem] = Field(default_factory=list, max_length=400)
    music_seconds: float | None = Field(default=None, gt=0.0, le=MAX_SECONDS)
    open_questions: list[str] = Field(default_factory=list, max_length=10)
    extraction: Extraction = Field(
        default_factory=lambda: Extraction(method="user_structured")
    )

    @field_validator("music_seconds", mode="before")
    @classmethod
    def _no_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("숫자를 입력해 주세요.")  # noqa: TRY004 - pydantic은 ValueError만 검증 오류로 바꾼다
        return value

    @model_validator(mode="after")
    def _unique_ids(self):
        if len({i.id for i in self.info_items}) != len(self.info_items):
            raise ValueError("정보 항목 ID가 중복됐습니다.")
        return self


class IntentIssue(Strict):
    code: str
    message: str
    path: str = ""


class IntentValidation(Strict):
    valid: bool
    needs_confirmation: bool
    errors: list[IntentIssue]
    warnings: list[IntentIssue]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def validate_intent(brief: str, intent: IntentSpec) -> IntentValidation:
    """타입·카탈로그·근거 검증. 환각된 근거(브리프에 없는 인용)와 근거 없는 hard 선택을 거부한다."""
    errors: list[IntentIssue] = []
    warnings: list[IntentIssue] = []
    haystack = _normalize(brief)
    seen: set[str] = set()
    primaries: dict[str, list[str]] = {}
    for i, sel in enumerate(intent.selections):
        path = f"selections[{i}]"
        if sel.entry_id not in ENTRIES:
            errors.append(
                IntentIssue(
                    code="unknown_entry",
                    message=f"카탈로그에 없는 항목입니다: {sel.entry_id}",
                    path=path,
                )
            )
            continue
        if sel.entry_id in seen:
            errors.append(
                IntentIssue(
                    code="duplicate_selection",
                    message=f"같은 항목을 두 번 선택했습니다: {sel.entry_id}",
                    path=path,
                )
            )
        seen.add(sel.entry_id)
        for span in sel.evidence:
            if _normalize(span.quote) not in haystack:
                errors.append(
                    IntentIssue(
                        code="evidence_not_in_brief",
                        message=f"근거 문구가 브리프에 없습니다: {span.quote!r}",
                        path=path,
                    )
                )
        if sel.strength == "hard" and not sel.evidence:
            errors.append(
                IntentIssue(
                    code="hard_without_evidence",
                    message="필수(hard) 선택은 브리프 인용 근거가 필요합니다.",
                    path=path,
                )
            )
        if sel.source == "user_explicit" and not sel.evidence:
            errors.append(
                IntentIssue(
                    code="explicit_without_evidence",
                    message="사용자 명시 선택은 인용이 필요합니다.",
                    path=path,
                )
            )
        if sel.source == "lexical_hint" and (
            sel.strength == "hard" or sel.role == "primary"
        ):
            errors.append(
                IntentIssue(
                    code="hint_cannot_decide",
                    message="단어 힌트는 필수·주 선택이 될 수 없습니다. 확인 후 다시 제출하세요.",
                    path=path,
                )
            )
        if sel.role == "primary":
            primaries.setdefault(ENTRIES[sel.entry_id].catalog, []).append(sel.entry_id)
    for catalog, ids in primaries.items():
        if catalog in SINGLE_PRIMARY and len(ids) > 1:
            errors.append(
                IntentIssue(
                    code="multiple_primary",
                    message=f"{catalog} {CATALOG_NAMES[catalog]}의 주 선택은 하나여야 합니다: {', '.join(ids)}",
                    path="selections",
                )
            )
    for i, c in enumerate(intent.constraints):
        for span in c.evidence:
            if _normalize(span.quote) not in haystack:
                errors.append(
                    IntentIssue(
                        code="evidence_not_in_brief",
                        message=f"제약 근거 문구가 브리프에 없습니다: {span.quote!r}",
                        path=f"constraints[{i}]",
                    )
                )
        if c.hard and not c.evidence:
            errors.append(
                IntentIssue(
                    code="hard_without_evidence",
                    message="필수 제약은 브리프 인용 근거가 필요합니다.",
                    path=f"constraints[{i}]",
                )
            )
        if c.source == "user_explicit" and not c.evidence:
            errors.append(
                IntentIssue(
                    code="explicit_without_evidence",
                    message="사용자 명시 제약은 인용이 필요합니다.",
                    path=f"constraints[{i}]",
                )
            )
    if intent.extraction.method == "llm":
        for i, selection in enumerate(intent.selections):
            if not selection.evidence:
                errors.append(
                    IntentIssue(
                        code="missing_evidence",
                        message="AI 선택 제안에는 브리프 인용 근거가 필요합니다.",
                        path=f"selections[{i}]",
                    )
                )
        for i, constraint in enumerate(intent.constraints):
            if not constraint.evidence:
                errors.append(
                    IntentIssue(
                        code="missing_evidence",
                        message="AI 제약 제안에는 브리프 인용 근거가 필요합니다.",
                        path=f"constraints[{i}]",
                    )
                )
        for i, item in enumerate(intent.info_items):
            if not item.evidence:
                errors.append(
                    IntentIssue(
                        code="missing_evidence",
                        message="AI 전달 항목에는 브리프 인용 근거가 필요합니다.",
                        path=f"info_items[{i}]",
                    )
                )
            for span in item.evidence:
                if _normalize(span.quote) not in haystack:
                    errors.append(
                        IntentIssue(
                            code="evidence_not_in_brief",
                            message=f"전달 항목 근거 문구가 브리프에 없습니다: {span.quote!r}",
                            path=f"info_items[{i}]",
                        )
                    )
    if not any(
        ENTRIES[s.entry_id].catalog == "C01"
        for s in intent.selections
        if s.entry_id in ENTRIES
    ):
        warnings.append(
            IntentIssue(
                code="no_content_type",
                message="콘텐츠 유형(C01)이 없습니다. 계획 전에 확인이 필요합니다.",
                path="selections",
            )
        )
    if intent.duration.mode == "platform_cap" and not any(
        c.kind == "platform" and c.hard for c in intent.constraints
    ):
        warnings.append(
            IntentIssue(
                code="platform_cap_without_constraint",
                message="플랫폼 상한 요청에 대응하는 hard 제약 기록이 없습니다.",
                path="duration",
            )
        )
    if intent.extraction.method == "lexical_hint":
        warnings.append(
            IntentIssue(
                code="unconfirmed_hint",
                message="단어 힌트로 만든 초안입니다. 확인 전에는 결정으로 쓰지 않습니다.",
                path="extraction",
            )
        )
    return IntentValidation(
        valid=not errors,
        needs_confirmation=intent.extraction.method in ("lexical_hint", "llm")
        or bool(intent.open_questions),
        errors=errors,
        warnings=warnings,
    )


_DURATION_HINT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(초|분|sec|seconds?|min|minutes?)", re.IGNORECASE
)


def duration_hint(brief: str) -> dict[str, Any] | None:
    """브리프의 길이 언급을 후보로만 돌려준다. 자동 적용하지 않는다."""
    for match in _DURATION_HINT.finditer(brief):
        value = float(match.group(1))
        seconds = (
            value * 60
            if match.group(2).lower() in ("분", "min", "minute", "minutes")
            else value
        )
        if MIN_SECONDS <= seconds <= MAX_SECONDS:
            return {
                "seconds": seconds,
                "quote": match.group(0),
                "applied": False,
                "note": "확인 후 희망 시간으로 입력해야 적용됩니다.",
            }
    return None


def hint_intent(brief: str) -> tuple[IntentSpec, dict[str, Any] | None]:
    """사전 힌트로 만든 초안. 모든 선택은 soft·secondary·낮은 신뢰도이며 사용자가 확인해야 한다."""
    selections: list[Selection] = []
    lowered = brief.casefold()
    for entry in ENTRIES.values():
        for hint in entry.hints:
            if len(hint) < 2:
                continue
            at = lowered.find(hint.casefold())
            if at >= 0:
                selections.append(
                    Selection(
                        entry_id=entry.id,
                        role="secondary",
                        strength="soft",
                        confidence=0.3,
                        source="lexical_hint",
                        evidence=[Span(quote=brief[at : at + len(hint)])],
                    )
                )
                break
    spec = IntentSpec(
        selections=selections,
        extraction=Extraction(method="lexical_hint"),
        open_questions=["주된 콘텐츠 유형과 시각 스타일을 확인해 주세요."],
    )
    return spec, duration_hint(brief)


def extraction_contract() -> dict[str, Any]:
    """LLM 추출기에 넘기는 스키마와 규칙. 서버는 추출 결과를 validate_intent로 검증한다."""
    return {
        "schema": IntentSpec.model_json_schema(),
        "catalogs": {
            cat: {
                "name": name,
                "entries": [e.id for e in ENTRIES.values() if e.catalog == cat],
            }
            for cat, name in CATALOG_NAMES.items()
        },
        "rules": [
            "필수(hard) 선택과 제약에는 브리프의 정확한 인용(evidence.quote)을 붙인다. 인용은 브리프에 그대로 있어야 한다.",
            "브리프에 없는 취향을 hard로 만들지 않는다. 추정은 llm_inferred soft로 표시한다.",
            "C01·C03·C08의 주 선택(primary)은 각각 하나뿐이다.",
            "길이는 duration에 mode(auto/preferred/exact/platform_cap)와 근거로 적는다. 명시적 정확한 길이와 플랫폼 상한만 strict다.",
            "info_items는 전달해야 할 정보 단위이며 길이를 맞추려고 만들어 넣지 않는다.",
            "확신이 낮으면 open_questions에 적는다.",
        ],
    }
