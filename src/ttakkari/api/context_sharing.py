"""프로젝트 맥락 공유 REST. 대화 에이전트는 명시된 프로젝트 게시·키·사이트 제어를 할 수 있다.

에이전트(`/api/agent/context-sharing`)는 기존 세션 검사를 통과한 자연어 요청에 따라 범위가 제한된 작업을 한다.
게시·키 발급·폐기·사이트 변경 외의 편집 및 프로젝트 전체 철회는 소유자 경로에 둔다.
오류는 {code, message[, ...]} 로 내려간다. control 토큰은 응답·로그·DB에 남기지 않고, 원문 키는 발급 응답에서만 한 번 반환하며 로그·DB에는 저장하지 않는다.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ttakkari.api.deps import Db
from ttakkari.context_sharing import service
from ttakkari.context_sharing.adapter import LibraryAdapter
from ttakkari.context_sharing.errors import ShareError
from ttakkari.context_sharing.gateway import (
    GatewayBadResponse,
    GatewayControl,
    GatewayNotConfigured,
    GatewayRejected,
    GatewayUnavailable,
    build_control,
    gateway_status,
)
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User, client_ip

SLUG = r"^[a-z0-9][a-z0-9-]{1,78}$"


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: uuid.UUID
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=service.MAX_DOCUMENTS)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    confirm_work_scope: bool = False


class DocumentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    title: str | None = Field(default=None, min_length=1, max_length=500)
    content: str | None = Field(default=None, min_length=1, max_length=service.MAX_DOCUMENT_CHARS)
    owner_verified: bool | None = None


class DraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    documents: list[DocumentEdit] = Field(default_factory=list, max_length=service.MAX_DOCUMENTS)
    remove_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=service.MAX_DOCUMENTS)
    expected_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str = Field(pattern=SLUG)
    label: str = Field(default="", max_length=160)
    scopes: list[Literal["read", "search", "export"]] = Field(default_factory=lambda: ["read", "search"], min_length=1)
    ttl_seconds: Literal[3600] = 3600
    credential_type: Literal["single_use_v1", "reusable_api_v2"] = "single_use_v1"
    ttl_days: Literal[1, 3, 7, 30] = 3
    snapshot_policy: Literal["pinned", "latest"] = "pinned"


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str = Field(pattern=SLUG)
    label: str = Field(default="", max_length=160)
    ttl_days: Literal[1, 3, 7, 30] = 3
    snapshot_policy: Literal["pinned", "latest"] = "pinned"
    allow_export: bool = False


class SiteEnabledIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class AgentPublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: uuid.UUID
    confirm_work_scope: bool = False


async def user_access(_: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db) -> None:
    from fastapi import HTTPException

    if await db.get(ChatSession, sid) is None:
        raise HTTPException(403, "세션이 없습니다.")


async def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def get_gateway() -> GatewayControl:
    return build_control()


Gateway = Annotated[GatewayControl, Depends(get_gateway)]
router = APIRouter(prefix="/api/context-sharing", tags=["context-sharing"], dependencies=[Depends(user_access), Depends(no_store)])
agent_router = APIRouter(prefix="/api/agent/context-sharing", tags=["context-sharing"],
                         dependencies=[Depends(agent_access), Depends(no_store)])


def _meta(request: Request) -> dict[str, Any]:
    return {"ip": client_ip(request), "user_agent": request.headers.get("user-agent")}


async def _handler(request: Request, exc: ShareError) -> JSONResponse:
    return JSONResponse({"code": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status,
                        headers={"Cache-Control": "private, no-store", **exc.headers})


def _gateway_site_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, GatewayNotConfigured):
        return JSONResponse(status_code=503, content={"code": "gateway_not_configured", "message": "Gateway control is not configured",
                                                     "gateway_status": exc.status}, headers={"Cache-Control": "private, no-store"})
    if isinstance(exc, GatewayUnavailable):
        return JSONResponse(status_code=503, content={"code": "gateway_unavailable", "message": "Gateway site status could not be reached"},
                             headers={"Cache-Control": "private, no-store"})
    if isinstance(exc, GatewayRejected):
        return JSONResponse(status_code=502, content={"code": "gateway_rejected", "message": "Gateway rejected site control",
                                                       "gateway_status": exc.http_status}, headers={"Cache-Control": "private, no-store"})
    return JSONResponse(status_code=502, content={"code": "gateway_bad_response", "message": "Gateway site control response was invalid"},
                        headers={"Cache-Control": "private, no-store"})


def _site_configuration_error(gateway: GatewayControl) -> JSONResponse | None:
    if gateway.public_url:
        return None
    status = dict(gateway.status)
    missing = list(status.get("missing", []))
    if "context_gateway_public_url" not in missing:
        missing.append("context_gateway_public_url")
    status.update(configured=False, missing=missing)
    return _gateway_site_error(GatewayNotConfigured(status))


def install(app: FastAPI) -> None:
    app.add_exception_handler(ShareError, _handler)  # type: ignore[arg-type]


def _project_out(p, state: dict[str, Any] | None) -> dict[str, Any]:
    state = state or {}
    return {"entity_id": str(p.entity_id), "name": p.name, "aliases": p.aliases, "summary": p.summary[:240],
            "scope": p.scope, "source_count": p.source_count, "document_count": p.document_count,
            "slug": state.get("slug"), "publication": state.get("publication"),
            "active_key_count": state.get("active_key_count", 0),
            "active_link_count": state.get("active_link_count", 0),
            "last_published_at": state.get("last_published_at"),
            "revoke_state": state.get("revoke_state", "active")}


def _source_out(g) -> dict[str, Any]:
    return {"source_id": str(g.source_id), "label": g.label, "category": g.category, "scope": g.scope,
            "enabled": g.enabled, "truncated": g.truncated,
            "documents": [{"document_id": str(d.document_id), "title": d.title, "logical_path": d.logical_path,
                           "chunk_count": d.chunk_count, "chars": d.chars, "source_revision": d.source_revision,
                           "indexed_at": service.iso(d.indexed_at)} for d in g.documents]}


async def _projects(db) -> dict[str, Any]:
    adapter = LibraryAdapter(db)
    projects = await adapter.list_projects()
    states = await service.project_states(db, [p.entity_id for p in projects])
    return {"projects": [_project_out(p, states.get(p.entity_id)) for p in projects]}


async def _sources(db, entity_id: uuid.UUID) -> dict[str, Any]:
    adapter = LibraryAdapter(db)
    project = await adapter.get_project(entity_id)
    if project is None:
        raise ShareError(404, "project_not_found", "라이브러리에 그런 프로젝트가 없습니다.")
    return {"entity_id": str(entity_id), "name": project.name, "scope": project.scope,
            "sources": [_source_out(g) for g in await adapter.list_documents(entity_id)]}


# ---- 소유자

@router.get("/status")
async def status() -> dict[str, Any]:
    """Gateway control 연결 설정 상태. 토큰·전체 주소는 내려가지 않는다."""
    return {"gateway": gateway_status()}


@router.get("/site")
async def site_status(gateway: Gateway) -> Any:
    """Read the live Gateway setting rather than the local connection configuration."""
    if error := _site_configuration_error(gateway):
        return error
    try:
        enabled = await gateway.site_status()
    except (GatewayNotConfigured, GatewayUnavailable, GatewayRejected, GatewayBadResponse) as exc:
        return _gateway_site_error(exc)
    return {"enabled": enabled, "public_url": gateway.public_url}


@router.patch("/site")
async def set_site_status(body: SiteEnabledIn, gateway: Gateway) -> Any:
    if error := _site_configuration_error(gateway):
        return error
    try:
        enabled = await gateway.set_site_enabled(body.enabled)
    except (GatewayNotConfigured, GatewayUnavailable, GatewayRejected, GatewayBadResponse) as exc:
        return _gateway_site_error(exc)
    return {"enabled": enabled, "public_url": gateway.public_url}


@router.get("/projects")
async def projects(db: Db) -> dict[str, Any]:
    return {"gateway": gateway_status(), **await _projects(db)}


@router.get("/projects/{entity_id}/sources")
async def sources(entity_id: uuid.UUID, db: Db) -> dict[str, Any]:
    return await _sources(db, entity_id)


@router.post("/drafts", status_code=201)
async def create_draft(body: DraftCreate, request: Request, db: Db) -> dict[str, Any]:
    return await service.create_draft(db, LibraryAdapter(db), entity_id=body.entity_id, document_ids=body.document_ids,
                                      title=body.title, summary=body.summary, actor="owner",
                                      confirm_work_scope=body.confirm_work_scope, **_meta(request))


@router.get("/drafts")
async def list_drafts(db: Db, entity_id: uuid.UUID | None = None, status: Literal["draft", "published"] | None = None,
                      limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    return await service.list_drafts(db, entity_id=entity_id, status=status, limit=limit, offset=offset)


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: uuid.UUID, db: Db) -> dict[str, Any]:
    return await service.get_draft(db, draft_id)


@router.patch("/drafts/{draft_id}")
async def update_draft(draft_id: uuid.UUID, body: DraftPatch, request: Request, db: Db) -> dict[str, Any]:
    return await service.update_draft(db, draft_id, body.model_dump(exclude_unset=True), **_meta(request))


@router.post("/drafts/{draft_id}/publish")
async def publish(draft_id: uuid.UUID, body: PublishIn, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    return await service.publish_draft(db, gateway, draft_id, body.approved_digest, **_meta(request))


@router.post("/keys", status_code=201)
async def issue_key(body: KeyIn, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    """원문 키는 이 응답에 한 번만 담긴다. Private DB·감사 로그·목록 응답에는 없다."""
    return await service.issue_key(db, gateway, body.project_slug, label=body.label,
                                   scopes=sorted(set(body.scopes)), credential_type=body.credential_type,
                                   ttl_days=body.ttl_days, snapshot_policy=body.snapshot_policy, **_meta(request))


@router.get("/keys")
async def list_keys(db: Db, gateway: Gateway, project_slug: Annotated[str | None, Query(pattern=SLUG)] = None) -> dict[str, Any]:
    return await service.list_keys(db, project_slug, gateway)


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: uuid.UUID, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    return await service.revoke_key(db, gateway, key_id, **_meta(request))


@router.post("/links", status_code=201)
async def issue_link(body: LinkIn, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    return await service.issue_link(db, gateway, body.project_slug, label=body.label, ttl_days=body.ttl_days,
                                    snapshot_policy=body.snapshot_policy, allow_export=body.allow_export, **_meta(request))


@router.get("/links")
async def list_links(db: Db, gateway: Gateway, project_slug: Annotated[str | None, Query(pattern=SLUG)] = None) -> dict[str, Any]:
    return await service.list_links(db, project_slug, gateway)


@router.post("/links/{link_id}/revoke")
async def revoke_link(link_id: uuid.UUID, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    return await service.revoke_link(db, gateway, link_id, **_meta(request))


@router.post("/projects/{slug}/revoke")
async def revoke_project(slug: str, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    return await service.revoke_project(db, gateway, slug, **_meta(request))


# ---- 에이전트: 세션 범위 프로젝트 게시·키·사이트 관리, 초안 편집/프로젝트 철회는 제외

@agent_router.get("/projects")
async def agent_projects(db: Db) -> dict[str, Any]:
    return await _projects(db)


@agent_router.get("/projects/{entity_id}/sources")
async def agent_sources(entity_id: uuid.UUID, db: Db) -> dict[str, Any]:
    return await _sources(db, entity_id)


@agent_router.post("/drafts", status_code=201)
async def agent_create_draft(body: DraftCreate, request: Request, db: Db) -> dict[str, Any]:
    # work 범위는 사용자가 직접 요청해 에이전트가 confirm_work_scope=true 를 보낼 때만 통과한다. 서비스가 거부 여부를 정한다.
    return await service.create_draft(db, LibraryAdapter(db), entity_id=body.entity_id, document_ids=body.document_ids,
                                      title=body.title, summary=body.summary, actor="agent",
                                      confirm_work_scope=body.confirm_work_scope, **_meta(request))


@agent_router.get("/drafts")
async def agent_list_drafts(db: Db, entity_id: uuid.UUID | None = None,
                            limit: Annotated[int, Query(ge=1, le=100)] = 50) -> dict[str, Any]:
    return await service.list_drafts(db, entity_id=entity_id, status=None, limit=limit, offset=0)


@agent_router.get("/drafts/{draft_id}")
async def agent_get_draft(draft_id: uuid.UUID, db: Db) -> dict[str, Any]:
    return await service.get_draft(db, draft_id)


@agent_router.post("/publish")
async def agent_publish(body: AgentPublishIn, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    if error := _site_configuration_error(gateway):
        return error
    return await service.collect_and_publish(db, LibraryAdapter(db), gateway, entity_id=body.entity_id,
                                          confirm_work_scope=body.confirm_work_scope, **_meta(request))


@agent_router.get("/keys")
async def agent_list_keys(db: Db, gateway: Gateway, project_slug: Annotated[str | None, Query(pattern=SLUG)] = None) -> dict[str, Any]:
    return await service.list_keys(db, project_slug, gateway)


@agent_router.post("/keys", status_code=201)
async def agent_issue_key(body: KeyIn, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    if error := _site_configuration_error(gateway):
        return error
    return await service.issue_key(db, gateway, body.project_slug, label=body.label,
                                   scopes=sorted(set(body.scopes)), credential_type=body.credential_type,
                                   ttl_days=body.ttl_days, snapshot_policy=body.snapshot_policy,
                                   actor="agent", **_meta(request))


@agent_router.post("/keys/{key_id}/revoke")
async def agent_revoke_key(key_id: uuid.UUID, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    if error := _site_configuration_error(gateway):
        return error
    return await service.revoke_key(db, gateway, key_id, actor="agent", **_meta(request))


@agent_router.get("/links")
async def agent_list_links(db: Db, gateway: Gateway, project_slug: Annotated[str | None, Query(pattern=SLUG)] = None) -> dict[str, Any]:
    return await service.list_links(db, project_slug, gateway)


@agent_router.post("/links", status_code=201)
async def agent_issue_link(body: LinkIn, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    if error := _site_configuration_error(gateway):
        return error
    return await service.issue_link(db, gateway, body.project_slug, label=body.label, ttl_days=body.ttl_days,
                                    snapshot_policy=body.snapshot_policy, allow_export=body.allow_export,
                                    actor="agent", **_meta(request))


@agent_router.post("/links/{link_id}/revoke")
async def agent_revoke_link(link_id: uuid.UUID, request: Request, db: Db, gateway: Gateway) -> dict[str, Any]:
    if error := _site_configuration_error(gateway):
        return error
    return await service.revoke_link(db, gateway, link_id, actor="agent", **_meta(request))


@agent_router.get("/site")
async def agent_site_status(gateway: Gateway) -> Any:
    if error := _site_configuration_error(gateway):
        return error
    try:
        enabled = await gateway.site_status()
    except (GatewayNotConfigured, GatewayUnavailable, GatewayRejected, GatewayBadResponse) as exc:
        return _gateway_site_error(exc)
    return {"enabled": enabled, "public_url": gateway.public_url}


@agent_router.patch("/site")
async def agent_set_site_status(body: SiteEnabledIn, gateway: Gateway) -> Any:
    if error := _site_configuration_error(gateway):
        return error
    try:
        enabled = await gateway.set_site_enabled(body.enabled)
    except (GatewayNotConfigured, GatewayUnavailable, GatewayRejected, GatewayBadResponse) as exc:
        return _gateway_site_error(exc)
    return {"enabled": enabled, "public_url": gateway.public_url}
