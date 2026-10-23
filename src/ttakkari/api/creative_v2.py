"""Creative Studio v2: 카탈로그·레지스트리·의도·매칭·Style DNA·길이 정책·제작 계획 API.

모두 소유자 인증 라우트다. 파일 경로나 URL을 입력으로 받지 않는다.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import Field

from ttakkari.api.creative import perform
from ttakkari.api.deps import Db
from ttakkari.creative.service import default_studio
from ttakkari.creative.v2 import dispatch as production
from ttakkari.creative.v2.capabilities import CAPABILITIES, Strict
from ttakkari.creative.v2.catalog_data import CATALOG_NAMES, ENTRIES
from ttakkari.creative.v2.duration import (
    ContentProfile,
    DurationRequest,
    SceneEdit,
    plan_duration,
    policy_summary,
    recheck_duration,
)
from ttakkari.creative.v2.intent import (
    IntentSpec,
    extraction_contract,
    hint_intent,
    validate_intent,
)
from ttakkari.creative.v2.matching import match_intent
from ttakkari.creative.v2.plans import build_plan, default_plan_store
from ttakkari.creative.v2.registry import Registry
from ttakkari.creative.v2.run_extraction import (
    read_intent_run,
    resume_intent_run,
    start_intent_run,
)
from ttakkari.creative.v2.style_dna import (
    Resolution,
    StyleDNADoc,
    StyleRef,
    UnresolvedStyleConflict,
    default_style_store,
)
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/creative/v2", tags=["creative-studio-v2"])

PlanId = Annotated[uuid.UUID, PathParam()]
StyleId = Annotated[str, PathParam(pattern=r"^[a-z][a-z0-9_]{1,59}$")]
Version = Annotated[str, PathParam(pattern=r"^(?:\d{1,3}\.\d{1,3}\.\d{1,3}|latest)$")]


class BriefInput(Strict):
    brief: str = Field(min_length=1, max_length=6000)


class IntentCheck(Strict):
    brief: str = Field(min_length=1, max_length=6000)
    intent: IntentSpec


class PreviewInput(Strict):
    profile: ContentProfile
    request: DurationRequest = Field(default_factory=DurationRequest)


class RecheckInput(Strict):
    scenes: list[SceneEdit] = Field(min_length=1, max_length=400)
    request: DurationRequest = Field(default_factory=DurationRequest)
    fps: int = Field(default=30, ge=12, le=120)
    music_seconds: float | None = Field(default=None, gt=0, le=900)


class PlanCreate(Strict):
    brief: str = Field(min_length=1, max_length=6000)
    name: str = Field(default="", max_length=120)
    intent: IntentSpec | None = None
    duration: DurationRequest | None = None
    aspect: Literal["16:9", "9:16", "1:1"] | None = None
    style: list[StyleRef] = Field(default_factory=list, max_length=8)
    resolutions: list[Resolution] = Field(default_factory=list, max_length=60)


class PlanEdit(Strict):
    revision: int = Field(ge=1)
    scenes: list[SceneEdit] | None = Field(default=None, min_length=1, max_length=400)
    duration: DurationRequest | None = None


class PlanApprove(Strict):
    revision: int = Field(ge=1)


class ComposeInput(Strict):
    inputs: list[StyleRef] = Field(min_length=1, max_length=8)
    resolutions: list[Resolution] = Field(default_factory=list, max_length=60)


class PinInput(ComposeInput):
    expected_revision: int | None = Field(default=None, ge=0)


def _registry() -> Registry:
    return Registry()


@router.get("/catalog")
async def catalog(_: User):
    return {
        "catalogs": {
            cat: {
                "name": name,
                "entries": [
                    e.model_dump(mode="json") | {"evidence_status": e.evidence_status}
                    for e in ENTRIES.values()
                    if e.catalog == cat
                ],
            }
            for cat, name in CATALOG_NAMES.items()
        },
        "capabilities": {
            k: {"name": v[0], "description": v[1]} for k, v in CAPABILITIES.items()
        },
    }


@router.get("/capabilities")
async def capabilities(_: User):
    """능력별 최고 수준과 공급자. 어떤 능력이 실제 제작 런타임에 연결됐는지 한 번에 본다."""

    def build():
        registry = _registry()
        return {
            "snapshot": registry.snapshot_meta(),
            "capabilities": {
                cap: {
                    "name": name,
                    "description": description,
                    "best_level": registry.capability_level(cap),
                    "providers": [
                        p.model_dump(mode="json") for p in registry.providers(cap)
                    ],
                }
                for cap, (name, description) in CAPABILITIES.items()
            },
        }

    return await asyncio.to_thread(build)


@router.get("/registries")
async def registries(_: User):
    return await asyncio.to_thread(lambda: _registry().summary())


@router.get("/matrix")
async def matrix(_: User):
    registry = _registry()
    return {
        "snapshot": registry.snapshot_meta(),
        "recipes": [r.model_dump(mode="json") for r in registry.recipe_matrix()],
    }


@router.get("/duration/policy")
async def duration_policy(_: User):
    return policy_summary()


@router.post("/duration/preview")
async def duration_preview(body: PreviewInput, _: User):
    return plan_duration(body.profile, body.request)


@router.post("/duration/recheck")
async def duration_recheck(body: RecheckInput, _: User):
    return perform(
        recheck_duration,
        body.scenes,
        body.request,
        fps=body.fps,
        music_seconds=body.music_seconds,
    )


@router.get("/intent/contract")
async def intent_contract(_: User):
    return extraction_contract()


@router.post("/intent/runs", status_code=202)
async def intent_run_start(
    body: BriefInput,
    _: User,
    db: Db,
    request_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=120)] = None,
):
    return await start_intent_run(db, body.brief, request_key=request_key)


@router.get("/intent/runs/{run_id}")
async def intent_run_get(run_id: uuid.UUID, _: User, db: Db):
    return await read_intent_run(db, run_id)


@router.post("/intent/runs/{run_id}/resume", status_code=202)
async def intent_run_resume(
    run_id: uuid.UUID,
    _: User,
    db: Db,
    request_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=120)] = None,
):
    return await resume_intent_run(db, run_id, request_key=request_key)


@router.post("/intent/validate")
async def intent_validate(body: IntentCheck, _: User):
    return validate_intent(body.brief, body.intent)


@router.post("/intent/hint")
async def intent_hint(body: BriefInput, _: User):
    intent, hint = hint_intent(body.brief)
    return {
        "intent": intent,
        "duration_hint": hint,
        "validation": validate_intent(body.brief, intent),
    }


@router.post("/match")
async def match(body: IntentCheck, _: User):
    return perform(match_intent, body.brief, body.intent, _registry())


@router.get("/style-dna")
async def style_list(_: User):
    return default_style_store().list()


@router.get("/style-dna/{style_id}/{version}")
async def style_get(style_id: StyleId, version: Version, _: User):
    store = default_style_store()

    def load():
        ref = store.resolve_ref(style_id, version)
        return {
            "ref": str(ref),
            "dna": store.effective(ref),
            "provenance": store.provenance_of(ref),
            "maturity": store.effective(ref).maturity,
        }

    return perform(load)


@router.post("/style-dna", status_code=201)
async def style_put(body: StyleDNADoc, _: User):
    return perform(default_style_store().put, body)


@router.post("/style-dna/compose")
async def style_compose(body: ComposeInput, _: User):
    return perform(default_style_store().compose, body.inputs, body.resolutions)


@router.post("/style-dna/works/{work_id}/pin")
async def style_pin(work_id: uuid.UUID, body: PinInput, _: User):
    store = default_style_store()

    def pin():
        try:
            return store.pin_work(
                str(work_id),
                body.inputs,
                body.resolutions,
                expected_revision=body.expected_revision,
            )
        except UnresolvedStyleConflict as e:
            # 전역 오류 핸들러는 {code, message}만 남기므로 충돌 목록은 응답 본문으로 직접 돌려준다.
            return JSONResponse(
                {
                    "code": "style_conflict",
                    "message": str(e),
                    "conflicts": [c.model_dump(mode="json") for c in e.conflicts],
                },
                status_code=409,
            )

    return perform(pin)


@router.get("/style-dna/works/{work_id}")
async def style_work(work_id: uuid.UUID, _: User):
    return perform(default_style_store().get_work, str(work_id))


@router.post("/plans", status_code=201)
async def plan_create(body: PlanCreate, _: User):
    def create() -> dict[str, Any]:
        plan = build_plan(
            body.brief,
            body.intent,
            name=body.name,
            duration=body.duration,
            aspect=body.aspect,
            registry=_registry(),
            styles=default_style_store(),
            style_refs=body.style,
            resolutions=body.resolutions,
        )
        return default_plan_store().create(plan)

    return await asyncio.to_thread(perform, create)


@router.get("/plans")
async def plan_list(_: User):
    return default_plan_store().list()


@router.get("/plans/{pid}")
async def plan_get(pid: PlanId, _: User):
    return perform(default_plan_store().get, str(pid))


@router.patch("/plans/{pid}")
async def plan_edit(pid: PlanId, body: PlanEdit, _: User):
    return await asyncio.to_thread(
        perform,
        default_plan_store().edit,
        str(pid),
        body.revision,
        scenes=body.scenes,
        duration=body.duration,
        registry=_registry(),
    )


@router.post("/plans/{pid}/approve")
async def plan_approve(pid: PlanId, body: PlanApprove, _: User):
    return perform(default_plan_store().approve, str(pid), body.revision)


@router.get("/plans/{pid}/production")
async def production_get(pid: PlanId, _: User):
    return await asyncio.to_thread(
        perform, production.view, default_plan_store(), default_studio(), str(pid)
    )


@router.post("/plans/{pid}/production", status_code=201)
async def production_dispatch(pid: PlanId, body: PlanApprove, _: User):
    return await asyncio.to_thread(
        perform,
        production.dispatch,
        default_plan_store(),
        default_studio(),
        str(pid),
        body.revision,
    )


@router.post("/plans/{pid}/production/render", status_code=202)
async def production_render(pid: PlanId, body: PlanApprove, _: User):
    return await asyncio.to_thread(
        perform,
        production.request_render,
        default_plan_store(),
        default_studio(),
        str(pid),
        body.revision,
    )
