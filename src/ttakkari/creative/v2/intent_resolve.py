"""POST /intent/resolve의 본체. 자유 입력·구조화 입력·답변·충돌 해결을 IntentSpec v2 하나로 합친다.

상태를 저장하지 않는다. 클라이언트가 직전 응답의 intent를 다시 보내면 revision이 1 오르고 history가 이어진다.
저장(PlanStore 연결)은 후속 작업이며 계약 문서 §9에 적었다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ttakkari.creative.v2.capabilities import Strict
from ttakkari.creative.v2.catalog_data import ENTRIES
from ttakkari.creative.v2.duration import Beat, ContentProfile, plan_duration
from ttakkari.creative.v2.facets import FIELDS, Layout, resolve_layout
from ttakkari.creative.v2.intent import InfoItem
from ttakkari.creative.v2.intent_v2 import (
    _RANK,
    TONES,
    Answer,
    AspectChoice,
    Condition,
    Confirmation,
    ConflictCandidate,
    ConflictResolution,
    DurationPolicy,
    HistoryEntry,
    IntentConflict,
    IntentSpecV2,
    ReferenceAsset,
    UnsupportedItem,
    _duration_label,
    catalog_conflicts,
    dimension_of,
    duration_value,
    durations_conflict,
    label_of,
    load_intent,
    now_iso,
    prune_evidence,
    read_free_text,
    stale_text_items,
)

# 값이 없으면 계획을 만들 수 없는 설정. 빈 답은 답을 지우고 미확정 상태로 되돌린다(기본값으로 채우지 않는다).
CLEARED_REQUIRED = ("content.type", "output.duration", "output.aspect")

# 지금 업로드 경로가 있는 참조 자산 종류(creative_v2 API의 이미지·음악 업로드). 영상·문서는 아직 받지 못한다.
INGESTIBLE = ("image", "audio")


class StructuredSelection(Strict):
    id: str = Field(max_length=80)
    role: Literal["primary", "secondary"] = "secondary"
    strength: Literal["required", "preferred"] = "preferred"


class StructuredDuration(Strict):
    mode: Literal["auto", "target", "strict"] = "auto"
    seconds: float | None = None


class StructuredInput(Strict):
    selections: list[StructuredSelection] = Field(default_factory=list, max_length=60)
    duration: StructuredDuration | None = None
    aspect: Literal["16:9", "9:16", "1:1"] | None = None


class AnswerInput(Strict):
    field_id: str = Field(max_length=80)
    # 빈 목록은 "선택을 모두 해제"다(복수 선택 해제). 아래 CLEARED_REQUIRED·resolve_intent의 처리 참고.
    values: list[str] = Field(default_factory=list, max_length=12)


class ResolutionInput(Strict):
    conflict_id: str = Field(max_length=200)
    # 후보 value 하나, 또는 둘 다 유지하는 keep_both(비차단 충돌과 혼합 의도에만).
    choice: str = Field(max_length=120)
    note: str = Field(default="", max_length=300)


class ResolveRequest(Strict):
    brief: str = Field(default="", max_length=6000)
    intent: dict[str, Any] | None = None
    structured: StructuredInput | None = None
    answers: list[AnswerInput] = Field(default_factory=list, max_length=80)
    confirm: list[str] = Field(default_factory=list, max_length=60)
    reject: list[str] = Field(default_factory=list, max_length=60)
    resolutions: list[ResolutionInput] = Field(default_factory=list, max_length=60)
    references: list[ReferenceAsset] = Field(default_factory=list, max_length=20)
    read_free_text: bool = True
    # 전달 항목과 음악 길이. 없으면 직전 의도의 값을 이어받는다(빈 목록은 "비우기").
    info_items: list[InfoItem] | None = Field(default=None, max_length=400)
    music_seconds: float | None = Field(default=None, gt=0.0, le=900)


class LengthAssessment(Strict):
    status: Literal[
        "auto",
        "target_met",
        "deviated",
        "strict_met",
        "conflict",
        "needs_content",
        "blocked_by_conflict",
    ]
    message: str
    planned_seconds: float | None = None
    needed_seconds: dict[str, float] | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)


class ResolveResult(Strict):
    intent: IntentSpecV2
    layout: Layout
    length: LengthAssessment
    ready_for_plan: bool
    blocking: list[str]


class _Ctx:
    def __init__(self, spec: IntentSpecV2, revision: int):
        self.revision = revision
        self.at = now_iso()
        self.conditions: dict[str, Condition] = {c.id: c for c in spec.conditions}
        self.rejected: list[str] = list(spec.rejected)
        self.history: list[HistoryEntry] = list(spec.history)
        self.unsupported: dict[str, UnsupportedItem] = {
            u.id: u for u in spec.unsupported
        }

    def log(self, actor: str, action: str, detail: str = "") -> None:
        self.history.append(
            HistoryEntry(
                revision=self.revision,
                at=self.at,
                actor=actor,
                action=action,
                detail=detail[:400],
            )
        )

    def confirmation(self) -> Confirmation:
        return Confirmation(at=self.at, by="user", revision=self.revision)

    def confirm(
        self, cid: str, *, role: str | None = None, strength: str | None = None
    ) -> None:
        dim = dimension_of(cid)
        if dim is None:
            self.unsupported[f"unsupported.{cid}"] = UnsupportedItem(
                id=f"unsupported.{cid}",
                subject=cid,
                reason="unknown_tone" if cid.startswith("tone.") else "not_in_catalog",
                message=f"'{cid}'은(는) 카탈로그·톤 목록에 없어 노출 규칙과 계획에 쓰지 못합니다. 설명에 남기면 AI 감독이 참고합니다.",
                alternatives=[],
            )
            self.log("system", "unsupported_condition", cid)
            return
        old = self.conditions.get(cid)
        self.conditions[cid] = Condition(
            id=cid,
            dimension=dim,
            role=role or (old.role if old else "secondary"),
            strength=strength or (old.strength if old else "preferred"),
            origin="user_confirmed",
            confidence=1.0,
            evidence=old.evidence if old else [],
            confirmation=old.confirmation
            if old and old.origin == "user_confirmed" and not (role or strength)
            else self.confirmation(),
        )
        if cid in self.rejected:
            self.rejected.remove(cid)
        if not old or old.origin != "user_confirmed":
            self.log(
                "user",
                "condition_confirmed",
                f"{cid} ({old.origin if old else '새 선택'} → user_confirmed)",
            )

    def reject(self, cid: str) -> None:
        if self.conditions.pop(cid, None) is not None or cid not in self.rejected:
            self.log("user", "condition_rejected", cid)
        if cid not in self.rejected:
            self.rejected.append(cid)


def _merge_choice(
    kind: str,
    prior,
    text,
    structured,
    prior_conflict: IntentConflict | None,
    ctx: _Ctx,
    *,
    value_of,
    label_of_,
    conflicts_fn,
):
    """길이·비율 공통 병합. 서로 어긋나는 후보가 있으면 유효값을 비우고 충돌로 돌려준다."""
    cands = []
    for c in (structured, text, prior):
        if c is None or c.origin == "default":
            continue
        if all(value_of(c) != value_of(x) for x in cands):
            cands.append(c)
    if not cands:
        return prior, None
    clash = [b for b in cands[1:] if conflicts_fn(cands[0], b)]
    if not clash:
        best = max(cands, key=lambda c: _RANK[c.origin])
        return best, None
    group = [cands[0], *clash]
    values = {value_of(c) for c in group}
    conflict = IntentConflict(
        id=f"conflict.{kind}",
        kind=kind,
        dimension="C11",
        message=f"{'길이' if kind == 'duration' else '화면 비율'} 조건이 서로 다릅니다: "
        + " / ".join(f"{label_of_(c)}({c.origin})" for c in group)
        + ". 한쪽을 조용히 버리지 않으니 골라 주세요.",
        candidates=[
            ConflictCandidate(
                value=value_of(c),
                label=label_of_(c),
                origin=c.origin,
                evidence=c.evidence[:3],
            )
            for c in group
        ],
    )
    if (
        prior_conflict
        and prior_conflict.status == "resolved"
        and prior_conflict.resolution
        and prior_conflict.resolution.choice in values
    ):
        chosen = next(
            c for c in group if value_of(c) == prior_conflict.resolution.choice
        )
        return chosen, conflict.model_copy(
            update={"status": "resolved", "resolution": prior_conflict.resolution}
        )
    ctx.log("system", f"{kind}_conflict", conflict.message)
    return None, conflict


def _length(spec: IntentSpecV2) -> LengthAssessment:
    if spec.duration is None:
        return LengthAssessment(
            status="blocked_by_conflict",
            message="길이 충돌을 먼저 해결해야 정보량과 길이를 맞출 수 있습니다.",
        )
    if not spec.info_items:
        return LengthAssessment(
            status="needs_content" if spec.duration.mode != "auto" else "auto",
            message="전달 항목(info_items)이 아직 없어 정보량과 길이의 균형은 스토리보드 단계에서 기존 길이 계산기로 판정합니다.",
        )
    profile = ContentProfile(
        beats=[
            Beat(
                id=i.id,
                title=i.title,
                narration=i.narration,
                info_weight=i.weight,
                essential=i.essential,
            )
            for i in spec.info_items
        ],
        music_seconds=spec.music_seconds,
    )
    plan = plan_duration(profile, spec.duration.to_request())
    return LengthAssessment(
        status=plan.status,
        message=" ".join(plan.reasons[:3]) or "길이 계산 완료",
        planned_seconds=plan.planned_seconds,
        needed_seconds={
            "min": plan.needed.min_seconds,
            "ideal": plan.needed.ideal_seconds,
            "max": plan.needed.max_seconds,
        },
        alternatives=[a.model_dump() for a in plan.alternatives],
    )


def content_type_blocking(spec: IntentSpecV2) -> list[str]:
    """계획 가능 판정 중 콘텐츠 유형(C01) 조건. resolve와 계획 생성이 같은 기준을 쓴다."""
    types = [c for c in spec.conditions if c.dimension == "C01"]
    if not types:
        return ["missing.content_type"]
    if not any(c.origin == "user_confirmed" for c in types):
        # 자유 입력·AI가 읽은 유형은 후보일 뿐이다. 계획은 사용자가 유형을 확정한 뒤에 만든다.
        return ["unconfirmed.content_type"]
    return []


def resolve_intent(req: ResolveRequest) -> ResolveResult:
    prior = load_intent(req.intent, req.brief)
    revision = prior.revision + 1 if req.intent else 1
    # 자연어로 브리프를 고친 뒤 다시 읽을 때, 새 브리프에 근거 인용이 없는 이전 자유 입력·AI 해석은 걷어 낸다.
    stale = stale_text_items(prior, req.brief) if req.intent else []
    if stale:
        prior = prior.model_copy(
            update={
                "conditions": [c for c in prior.conditions if c.id not in stale],
                "duration": DurationPolicy()
                if "duration" in stale
                else prior.duration,
                "aspect": None if "aspect" in stale else prior.aspect,
            }
        )
    pruned: list[str] = []
    if req.intent:
        prior, pruned = prune_evidence(prior, req.brief)
    ctx = _Ctx(prior, revision)
    if not req.intent:
        ctx.log("user", "created", "새 제작 의도")
    if stale:
        ctx.log("system", "stale_text_removed", ", ".join(stale))
    if pruned:
        ctx.log("system", "evidence_pruned", ", ".join(pruned))

    for cid in req.reject:
        ctx.reject(cid)

    text = read_free_text(req.brief) if req.read_free_text and req.brief else None
    if text:
        for c in text.conditions:
            if c.id in ctx.rejected:
                continue
            old = ctx.conditions.get(c.id)
            if old is None:
                ctx.conditions[c.id] = c
                ctx.log(
                    "ai" if c.origin == "ai_inferred" else "system",
                    "condition_read",
                    f"{c.id} ({c.origin})",
                )
            elif _RANK[old.origin] < _RANK[c.origin]:
                ctx.conditions[c.id] = c

    s = req.structured
    for sel in s.selections if s else []:
        ctx.confirm(sel.id, role=sel.role, strength=sel.strength)
    for cid in req.confirm:
        if cid not in ctx.conditions:
            raise ValueError(f"확정할 조건이 의도에 없습니다: {cid}")
        ctx.confirm(cid)

    answers = {a.field_id: a for a in prior.answers}
    reset_duration = reset_aspect = False
    for a in req.answers:
        field = FIELDS.get(a.field_id)
        if field is None:
            raise ValueError(f"알 수 없는 설정입니다: {a.field_id}")
        if field.options:
            allowed = {o.id for o in field.options}
            bad = [v for v in a.values if v not in allowed]
            if bad:
                raise ValueError(f"{field.label}에 없는 선택지입니다: {', '.join(bad)}")
            if field.kind == "single" and len(a.values) > 1:
                raise ValueError(f"{field.label}은(는) 하나만 고를 수 있습니다.")
        if not a.values:
            # 해제: 선택지가 곧 조건인 값(예 C06.dolly)은 사용자가 지운 조건이 되어 같은 브리프를 다시 읽어도 되살아나지 않는다.
            for opt in field.options:
                if opt.id in ctx.conditions:
                    ctx.reject(opt.id)
            if a.field_id == "content.type":
                # 선택지에 없는 유형(예 C01.vlog)도 콘텐츠 유형이므로 함께 지운다. 일부만 남으면 미확정이 되지 않는다.
                for cid in [c.id for c in ctx.conditions.values() if c.dimension == "C01"]:
                    ctx.reject(cid)
            if a.field_id in CLEARED_REQUIRED:
                # 필수 설정은 저장된 답을 지우고 미확정으로 돌린다. 규칙 추천값을 확정으로 채우지 않는다.
                answers.pop(a.field_id, None)
                if a.field_id == "output.duration":
                    reset_duration = True
                elif a.field_id == "output.aspect":
                    reset_aspect = True
                ctx.log("user", "answer_cleared", f"{a.field_id} (필수 설정: 미확정으로 되돌림)")
            else:
                # 선택 설정은 "아무것도 고르지 않음"을 명시적 빈 답으로 남겨 추천값이 답처럼 되살아나지 않게 한다.
                answers[a.field_id] = Answer(field_id=a.field_id, values=[], revision=revision)
                ctx.log("user", "answer_cleared", a.field_id)
            continue
        answers[a.field_id] = Answer(
            field_id=a.field_id, values=a.values, revision=revision
        )
        ctx.log("user", "answered", f"{a.field_id}={','.join(a.values)}")
        for v in a.values:
            if v in ENTRIES or v in TONES:
                ctx.confirm(v)

    prior_conflicts = {c.id: c for c in prior.conflicts}
    s_duration = (
        DurationPolicy(
            mode=s.duration.mode, seconds=s.duration.seconds, origin="user_confirmed"
        )
        if s and s.duration
        else None
    )
    duration, d_conflict = _merge_choice(
        "duration",
        DurationPolicy() if reset_duration else prior.duration,
        text.duration if text else None,
        s_duration,
        prior_conflicts.get("conflict.duration"),
        ctx,
        value_of=duration_value,
        label_of_=_duration_label,
        conflicts_fn=durations_conflict,
    )
    s_aspect = (
        AspectChoice(value=s.aspect, origin="user_confirmed")
        if s and s.aspect
        else None
    )
    aspect, a_conflict = _merge_choice(
        "aspect",
        None if reset_aspect else prior.aspect,
        text.aspect if text else None,
        s_aspect,
        prior_conflicts.get("conflict.aspect"),
        ctx,
        value_of=lambda a: a.value,
        label_of_=lambda a: a.value,
        conflicts_fn=lambda x, y: x.value != y.value,
    )

    references = {r.id: r for r in prior.references}
    for ref in req.references:
        usable = ref.kind in INGESTIBLE
        references[ref.id] = ref.model_copy(update={"usable": usable})
        if not usable:
            ctx.unsupported[f"unsupported.reference.{ref.id}"] = UnsupportedItem(
                id=f"unsupported.reference.{ref.id}",
                subject=f"{ref.kind} 참조 {ref.name or ref.id}",
                reason="reference_not_ingested",
                message=f"{ref.kind} 참조는 아직 제작 입력으로 받지 못합니다. 기록만 하고 생성에는 쓰지 않습니다.",
                alternatives=["대표 프레임을 이미지 참조로 올리기"]
                if ref.kind == "video"
                else ["핵심 내용을 브리프나 전달 항목으로 옮기기"],
            )
        ctx.log("user", "reference_added", f"{ref.kind}:{ref.id}")
    if req.info_items is not None:
        ctx.log("user", "info_items_set", f"전달 항목 {len(req.info_items)}개")
    if req.music_seconds is not None:
        ctx.log("user", "music_seconds_set", f"{req.music_seconds:g}초")

    # 충돌 해결은 마지막에 적용한다. 같은 요청 안에서 생긴 충돌도 바로 풀 수 있다.
    def known() -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        if duration is not None:
            out["output.duration"] = (duration.mode, duration.origin)
        if aspect is not None:
            out["output.aspect"] = (aspect.value, aspect.origin)
        return out

    conditions = list(ctx.conditions.values())
    layout = resolve_layout(conditions, list(answers.values()), known())
    conflicts: dict[str, IntentConflict] = {}
    for c in [
        d_conflict,
        a_conflict,
        *catalog_conflicts(conditions),
        *layout.conflicts,
    ]:
        if c is None:
            continue
        old = prior_conflicts.get(c.id)
        if (
            old
            and old.status == "resolved"
            and c.status == "open"
            and old.resolution
            and old.resolution.choice == "keep_both"
        ):
            c = c.model_copy(
                update={"status": "resolved", "resolution": old.resolution}
            )
        conflicts[c.id] = c

    for res in req.resolutions:
        c = conflicts.get(res.conflict_id)
        if c is None:
            raise ValueError(f"해결할 충돌이 없습니다: {res.conflict_id}")
        values = [x.value for x in c.candidates]
        if res.choice == "keep_both":
            if c.kind in ("duration", "aspect", "single_primary"):
                raise ValueError("길이·비율·주 선택 충돌은 하나를 골라야 합니다.")
        elif res.choice not in values:
            raise ValueError(f"충돌 후보에 없는 선택입니다: {res.choice}")
        if c.kind == "duration" and res.choice != "keep_both":
            picked = next(
                x
                for x in (s_duration, text.duration if text else None, prior.duration)
                if x and duration_value(x) == res.choice
            )
            duration = picked.model_copy(update={"origin": "user_confirmed"})
        elif c.kind == "aspect" and res.choice != "keep_both":
            aspect = AspectChoice(value=res.choice, origin="user_confirmed")
        elif c.kind == "catalog_conflict" and res.choice != "keep_both":
            for v in values:
                if v != res.choice:
                    ctx.reject(v)
            ctx.confirm(res.choice)
        elif c.kind == "single_primary":
            for v in values:
                if v != res.choice and v in ctx.conditions:
                    ctx.conditions[v] = ctx.conditions[v].model_copy(
                        update={"role": "secondary"}
                    )
            ctx.confirm(res.choice, role="primary")
        conflicts[c.id] = c.model_copy(
            update={
                "status": "resolved",
                "resolution": ConflictResolution(
                    choice=res.choice, revision=revision, note=res.note
                ),
            }
        )
        ctx.log("user", "conflict_resolved", f"{c.id} → {res.choice}")

    if req.resolutions or req.answers:
        # 해결·답변으로 조건이 바뀌었을 수 있으니 화면 구성을 다시 계산한다.
        conditions = list(ctx.conditions.values())
        layout = resolve_layout(conditions, list(answers.values()), known())
        live = {c.id for c in catalog_conflicts(conditions)} | {
            c.id for c in layout.conflicts
        }
        conflicts = {
            k: v
            for k, v in conflicts.items()
            if k in live or v.kind in ("duration", "aspect") or v.status == "resolved"
        }

    layout = layout.model_copy(update={"conflicts": list(conflicts.values())})
    spec = IntentSpecV2(
        revision=revision,
        brief=req.brief or prior.brief,
        conditions=sorted(ctx.conditions.values(), key=lambda c: (c.dimension, c.id)),
        duration=duration,
        aspect=aspect,
        info_items=prior.info_items if req.info_items is None else req.info_items,
        music_seconds=prior.music_seconds
        if req.music_seconds is None
        else req.music_seconds,
        references=list(references.values()),
        answers=list(answers.values()),
        conflicts=list(conflicts.values()),
        unsupported=list(ctx.unsupported.values()),
        rejected=ctx.rejected,
        history=ctx.history[-400:],
        migrated_from=prior.migrated_from,
    )
    blocking = [c.id for c in spec.blocking_conflicts]
    blocking += content_type_blocking(spec)
    return ResolveResult(
        intent=spec,
        layout=layout,
        length=_length(spec),
        ready_for_plan=not blocking,
        blocking=blocking,
    )


__all__ = [
    "ResolveRequest",
    "ResolveResult",
    "content_type_blocking",
    "label_of",
    "resolve_intent",
]
