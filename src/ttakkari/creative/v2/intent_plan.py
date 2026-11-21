"""제작 의도 v2를 계획(plans.py)에 연결한다. 계약 문서: docs/design/creative-studio-v2-intent-v2.md §14

계획의 매칭·길이 계산·Style DNA는 v1 IntentSpec과 StyleRef를 입력으로 받는다. 이 모듈은 v2 의도를 그 입력으로 투영하고,
무엇이 어디에 반영됐고 무엇이 반영되지 않았는지를 `intent_projection`으로 남긴다. 규칙은 세 가지다.
- 계획 생성은 서버가 의도를 다시 검사한다. 열린 차단 충돌이나 사용자가 확정하지 않은 콘텐츠 유형이 있으면 거부한다.
- 출처(origin)는 투영 단계에서 올라가지 않는다. AI가 해석한 값은 사용자 확정으로 바뀌지 않고 필수(hard)가 되지 않는다.
- 소비하는 경로가 없는 값(답변·참조·미지원 조건)은 조용히 버리지 않고 not_applied에 이유와 함께 적는다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ttakkari.creative.v2.catalog_data import ENTRIES
from ttakkari.creative.v2.duration import DurationRequest
from ttakkari.creative.v2.facets import resolve_layout
from ttakkari.creative.v2.intent import (
    Extraction,
    IntentSpec,
    Selection,
    validate_intent,
)
from ttakkari.creative.v2.intent_resolve import content_type_blocking
from ttakkari.creative.v2.intent_v2 import (
    Condition,
    IntentSpecV2,
    catalog_conflicts,
    duration_value,
    label_of,
)
from ttakkari.creative.v2.style_dna import StyleRef, StyleStore

# 화풍·톤·세계관 조건 → 내장 Style DNA. 시드에 있는 스타일만 연결한다(없는 화풍에 비슷한 스타일을 붙이지 않는다).
STYLE_BINDINGS: dict[str, str] = {
    "C03.japanese_anime": "anime_cel",
    "tone.horror": "horror_lighting",
    "C02.space_sf": "sf_world",
}
# Style DNA는 결과물의 룩을 고정하므로 사용자가 쓴 말(user_free_text)과 확정(user_confirmed)에서만 고른다.
# 매칭은 v1처럼 AI 추론을 soft 선택으로 받는다. 출처는 투영에도 그대로 남는다.
STYLE_ORIGINS = ("user_confirmed", "user_free_text")

BLOCKING_MESSAGES = {
    "missing.content_type": "콘텐츠 유형(C01) 조건이 없습니다.",
    "unconfirmed.content_type": "콘텐츠 유형(C01)을 사용자가 확정하지 않았습니다. 자유 입력이나 AI가 읽은 유형은 후보입니다.",
    "unresolved.duration": "길이 조건이 확정되지 않았습니다(충돌 미해결).",
}


class PlanIntentError(ValueError):
    """계획 생성이 의도 때문에 거부된 사유. API는 422 {code, reasons}로 돌려준다."""

    def __init__(self, code: str, reasons: list[dict[str, str]]):
        self.code = code
        self.reasons = reasons
        super().__init__(
            f"{code}: " + "; ".join(f"{r['id']} {r['message']}" for r in reasons)
        )


@dataclass
class PreparedIntent:
    spec: IntentSpecV2
    v1: IntentSpec
    tone: list[str] = field(default_factory=list)


def readiness(spec: IntentSpecV2) -> list[str]:
    """계획을 만들어도 되는지에 대한 차단 사유 id. 클라이언트가 보낸 conflicts를 믿지 않고 조건에서 다시 계산한다."""
    resolved = {
        c.id
        for c in spec.conflicts
        if c.status == "resolved" and c.resolution is not None
    }
    out = [c.id for c in spec.blocking_conflicts]
    known: dict[str, tuple[str, str]] = {}
    if spec.duration is not None:
        known["output.duration"] = (spec.duration.mode, spec.duration.origin)
    if spec.aspect is not None:
        known["output.aspect"] = (spec.aspect.value, spec.aspect.origin)
    live = [
        *catalog_conflicts(spec.conditions),
        *resolve_layout(spec.conditions, spec.answers, known).conflicts,
    ]
    for c in live:
        if c.blocking and c.status == "open" and c.id not in resolved and c.id not in out:
            out.append(c.id)
    if spec.duration is None and "conflict.duration" not in out:
        out.append("unresolved.duration")
    return [*out, *content_type_blocking(spec)]


def _message(spec: IntentSpecV2, blocking_id: str) -> str:
    if blocking_id in BLOCKING_MESSAGES:
        return BLOCKING_MESSAGES[blocking_id]
    live = {c.id: c for c in spec.conflicts}
    live.update({c.id: c for c in catalog_conflicts(spec.conditions)})
    found = live.get(blocking_id)
    return found.message if found else "열린 충돌이 있습니다. 후보 중 하나를 골라 주세요."


def _source(origin: str) -> str:
    return {
        "user_confirmed": "user_explicit",
        "user_free_text": "user_explicit",
        "ai_inferred": "llm_inferred",
    }.get(origin, "default")


def to_v1(spec: IntentSpecV2) -> IntentSpec:
    """v2 의도를 v1 입력(매칭·길이 계산기)으로 바꾼다. 출처는 낮추기만 하고 올리지 않는다."""
    selections = [
        Selection(
            entry_id=c.id,
            role=c.role,
            strength="hard" if c.strength == "required" else "soft",
            confidence=c.confidence,
            source=_source(c.origin),
            evidence=c.evidence,
            confirmed=c.origin == "user_confirmed",
        )
        for c in spec.conditions
        if c.id in ENTRIES
    ]
    return IntentSpec(
        selections=selections,
        duration=spec.duration.to_request() if spec.duration else DurationRequest(),
        aspect=spec.aspect.value if spec.aspect else None,
        info_items=spec.info_items,
        music_seconds=spec.music_seconds,
        extraction=Extraction(method="user_structured"),
    )


def prepare(brief: str, spec: IntentSpecV2) -> PreparedIntent:
    """계획 생성 직전의 서버 검사와 v1 투영. 거부 사유는 PlanIntentError로 던진다."""
    if spec.brief and spec.brief != brief:
        raise PlanIntentError(
            "intent_invalid",
            [
                {
                    "id": "brief_mismatch",
                    "message": "intent_v2.brief가 계획 요청의 brief와 다릅니다. 근거 인용과 출처가 다른 문장을 가리키게 됩니다.",
                }
            ],
        )
    blocking = readiness(spec)
    if blocking:
        raise PlanIntentError(
            "intent_not_ready",
            [{"id": b, "message": _message(spec, b)} for b in blocking],
        )
    try:
        v1 = to_v1(spec)
    except ValidationError as exc:
        raise PlanIntentError(
            "intent_invalid", [{"id": "projection", "message": str(exc)[:400]}]
        ) from exc
    validation = validate_intent(brief, v1)
    if not validation.valid:
        raise PlanIntentError(
            "intent_invalid",
            [{"id": e.code, "message": f"{e.path} {e.message}"} for e in validation.errors],
        )
    tone = [c.id for c in spec.conditions if c.dimension == "tone"]
    return PreparedIntent(spec=spec, v1=v1, tone=tone)


def derive_style_refs(
    spec: IntentSpecV2, styles: StyleStore
) -> tuple[list[StyleRef], dict[str, StyleRef], list[dict[str, str]]]:
    """조건에서 Style DNA 참조를 고른다. (참조 목록, 조건→참조, 건너뛴 이유)."""
    refs: list[StyleRef] = []
    by_condition: dict[str, StyleRef] = {}
    skipped: list[dict[str, str]] = []
    for c in sorted(spec.conditions, key=lambda c: c.id):
        style_id = STYLE_BINDINGS.get(c.id)
        if style_id is None:
            continue
        if c.origin not in STYLE_ORIGINS:
            skipped.append(
                {
                    "id": c.id,
                    "reason": f"{c.origin} 조건은 Style DNA를 고르지 않습니다. 사용자가 확정하면 반영됩니다.",
                }
            )
            continue
        try:
            ref = styles.resolve_ref(style_id)
        except FileNotFoundError:
            skipped.append({"id": c.id, "reason": f"Style DNA {style_id}가 저장소에 없습니다."})
            continue
        if ref not in refs:
            refs.append(ref)
        by_condition[c.id] = ref
    return refs, by_condition, skipped


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def projection(
    prepared: PreparedIntent,
    *,
    match: dict[str, Any],
    style_by_condition: dict[str, StyleRef],
    style_skipped: list[dict[str, str]],
    style_applied: bool,
    explicit_style: bool,
) -> dict[str, Any]:
    """무엇이 어디에 반영됐는지의 기록. 출처(origin)와 강도를 v2 원본 그대로 싣는다."""
    spec = prepared.spec
    kept = {s["entry_id"] for s in match["selections"]}
    dropped = {d["entry_id"] for d in match["dropped"]}
    items: list[dict[str, Any]] = []
    not_applied: list[dict[str, str]] = []

    def condition_item(c: Condition) -> dict[str, Any]:
        applied: list[str] = []
        if c.id in kept:
            applied.append("match.selection")
        if c.dimension == "tone":
            applied.append("scene.tone")
        ref = style_by_condition.get(c.id)
        if ref is not None and style_applied:
            applied.append(f"style_dna:{ref}")
        return {
            "kind": "condition",
            "id": c.id,
            "label": label_of(c.id),
            "origin": c.origin,
            "strength": c.strength,
            "applied_to": applied,
        }

    for c in sorted(spec.conditions, key=lambda c: c.id):
        item = condition_item(c)
        items.append(item)
        if c.id in dropped:
            not_applied.append(
                {"id": c.id, "reason": "카탈로그 충돌 규칙으로 매칭에서 제외됐습니다."}
            )
        if (
            c.id in style_by_condition
            and not style_applied
            and explicit_style
        ):
            not_applied.append(
                {
                    "id": c.id,
                    "reason": "요청에 Style DNA가 직접 지정돼 조건에서 고른 Style DNA는 쓰지 않았습니다.",
                }
            )
    if spec.duration is not None:
        items.append(
            {
                "kind": "duration",
                "id": "duration",
                "origin": spec.duration.origin,
                "value": duration_value(spec.duration),
                "applied_to": ["duration.request"],
            }
        )
    if spec.aspect is not None:
        items.append(
            {
                "kind": "aspect",
                "id": "aspect",
                "origin": spec.aspect.origin,
                "value": spec.aspect.value,
                "applied_to": ["plan.aspect"],
            }
        )
    not_applied += [{"id": s["id"], "reason": s["reason"]} for s in style_skipped]
    not_applied += [
        {
            "id": f"answer.{a.field_id}",
            "reason": "답변은 의도에 저장되지만 이 설정을 소비하는 제작 경로가 아직 없습니다(조건이 된 선택지만 매칭에 반영).",
        }
        for a in spec.answers
        if not all(v in ENTRIES or v.startswith("tone.") for v in a.values)
    ]
    not_applied += [
        {"id": u.id, "reason": f"미지원 조건({u.reason}): {u.message}"}
        for u in spec.unsupported
    ]
    not_applied += [
        {
            "id": f"reference.{r.id}",
            "reason": "참조 자산은 의도에 기록만 되며 제작 입력으로 연결되지 않았습니다.",
        }
        for r in spec.references
    ]
    return {
        "schema": 1,
        "source": {
            "schema_version": 2,
            "revision": spec.revision,
            "sha256": _digest(spec.model_dump(mode="json")),
        },
        "items": items,
        "not_applied": not_applied,
    }
