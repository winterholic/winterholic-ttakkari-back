from __future__ import annotations

import hmac
import io
import json
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth import (
    AuthenticationError,
    authenticate_api_key,
    authenticate_session,
    exchange_key,
    exchange_share_link,
    hash_credential,
    session_validation_error,
)
from .config import Settings
from .database import make_session_factory
from .models import (
    APIKey,
    Base,
    KeyACL,
    Project,
    PublishOperation,
    Session,
    SiteControlAudit,
    SiteControlState,
    Snapshot,
)
from .publishing import (
    IdempotencyConflictError,
    IntegrityError,
    ProjectSharingRevokedError,
    PublishError,
    issue_api_key,
    issue_v2_credential,
    publish_snapshot,
    revoke_api_key,
    revoke_project_sharing,
    revoke_snapshot,
)
from .snapshot_service import get_snapshot, read_all_documents, read_document


class ExchangeResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_at: datetime


class SiteEnabledRequest(BaseModel):
    enabled: bool


class ProjectKeyGrant(BaseModel):
    scopes: list[Literal["read", "search", "export"]] = Field(min_length=1)


class IssueKeyRequest(BaseModel):
    label: str = Field(min_length=1, max_length=160)
    projects: dict[UUID, ProjectKeyGrant] = Field(min_length=1)


class V2ProjectGrant(BaseModel):
    scopes: list[Literal["read", "search", "export"]] = Field(min_length=1)
    snapshot_policy: Literal["pinned", "latest"] = "pinned"
    snapshot_id: UUID | None = None

    @model_validator(mode="after")
    def validate_snapshot(self):
        if (self.snapshot_policy == "pinned") != (self.snapshot_id is not None):
            raise ValueError("pinned policy requires snapshot_id; latest forbids it")
        return self


class IssueCredentialV2Request(BaseModel):
    credential_type: Literal["reusable_api_v2", "share_link_v2"]
    label: str = Field(min_length=1, max_length=160)
    ttl_days: Literal[1, 3, 7, 30] = 3
    projects: dict[UUID, V2ProjectGrant] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def validate_type_scopes(self):
        grant = next(iter(self.projects.values()))
        if self.credential_type == "share_link_v2" and (
            "read" not in grant.scopes or "search" in grant.scopes
        ):
            raise ValueError("share links require read and do not support search")
        return self


class CredentialStatusRequest(BaseModel):
    key_ids: list[UUID] = Field(min_length=1, max_length=200)


class SnapshotPublishRequest(BaseModel):
    operation_id: UUID | None = None
    project_slug: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    project_name: str = Field(min_length=1, max_length=240)
    project_description: str = Field(default="", max_length=4000)
    source_refs: list[str] = Field(min_length=1, max_length=100)
    source_revision: str = Field(min_length=1, max_length=255)
    generated_at: datetime
    verified_at: datetime | None = None
    documents: list[SnapshotDocumentInput] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_timestamps_and_size(self):
        if self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        if self.verified_at is not None and self.verified_at.utcoffset() is None:
            raise ValueError("verified_at must include a timezone when supplied")
        if self.verified_at is not None and self.verified_at < self.generated_at:
            raise ValueError("verified_at cannot precede generated_at")
        if sum(len(document.content.encode("utf-8")) for document in self.documents) > 20_000_000:
            raise ValueError("snapshot content exceeds 20 MB")
        return self


class SnapshotDocumentInput(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=2_000_000)
    content_type: Literal["text/markdown", "text/plain", "application/json"] = "text/markdown"
    source_refs: list[str] = Field(default_factory=list, max_length=100)
    source_revision: str | None = Field(default=None, min_length=1, max_length=255)
    generated_at: datetime | None = None
    verified_at: datetime | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_provenance_timestamps(self):
        for name in ("generated_at", "verified_at"):
            value = getattr(self, name)
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{name} must include a timezone when supplied")
        if (
            self.verified_at is not None
            and self.generated_at is not None
            and self.verified_at < self.generated_at
        ):
            raise ValueError("verified_at cannot precede generated_at")
        return self


class ProjectOut(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str
    latest_snapshot_id: UUID | None


class SnapshotOut(BaseModel):
    id: UUID
    project_id: UUID
    schema_version: int
    version: int
    created_at: datetime
    status: Literal["active", "revoked"]
    source_refs: list[str]
    source_revision: str
    generated_at: datetime
    verified_at: datetime | None
    published_at: datetime
    manifest_sha256: str
    documents: list[dict[str, Any]]


def _settings(settings: Settings | None) -> Settings:
    result = settings or Settings.from_env()
    if not result.database_url:
        raise RuntimeError("CONTEXT_GATEWAY_DATABASE_URL is required")
    return result


async def _initialize_schema(factory: async_sessionmaker[AsyncSession]) -> None:
    engine = factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.exec_driver_sql("SELECT pg_advisory_xact_lock(915712611)")
        site_control_was_initialized = await connection.scalar(
            text("SELECT to_regclass('public.gateway_site_control_audit') IS NOT NULL")
        )
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_api_keys ADD COLUMN IF NOT EXISTS credential_type VARCHAR(32) NOT NULL DEFAULT 'single_use_v1'"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_api_keys ADD COLUMN IF NOT EXISTS window_started_at TIMESTAMPTZ"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_api_keys ADD COLUMN IF NOT EXISTS window_count INTEGER NOT NULL DEFAULT 0"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_key_acls ADD COLUMN IF NOT EXISTS snapshot_policy VARCHAR(16) NOT NULL DEFAULT 'latest'"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_key_acls ADD COLUMN IF NOT EXISTS snapshot_id UUID"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_key_acls ADD COLUMN IF NOT EXISTS project_name VARCHAR(240)"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_key_acls ADD COLUMN IF NOT EXISTS project_description TEXT"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_publish_operations ADD COLUMN IF NOT EXISTS receipt JSONB"
        )
        if not site_control_was_initialized:
            await connection.exec_driver_sql(
                "INSERT INTO gateway_site_control (id, enabled) VALUES (1, TRUE) ON CONFLICT (id) DO NOTHING"
            )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_projects ADD COLUMN IF NOT EXISTS sharing_revoked_at TIMESTAMPTZ"
        )
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_snapshots ALTER COLUMN verified_at DROP NOT NULL"
        )
        await connection.exec_driver_sql(
            """CREATE OR REPLACE FUNCTION context_gateway_snapshot_immutable() RETURNS trigger AS $$
                BEGIN
                  IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'published snapshots are immutable';
                  END IF;
                  IF (to_jsonb(NEW) - 'revoked_at') IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at') THEN
                    RAISE EXCEPTION 'published snapshots are immutable';
                  END IF;
                  IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
                    RAISE EXCEPTION 'snapshot revocation is irreversible';
                  END IF;
                  RETURN NEW;
                END;
                $$ LANGUAGE plpgsql"""
        )
        trigger_exists = await connection.exec_driver_sql(
            "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_gateway_snapshots_immutable' AND tgrelid = 'gateway_snapshots'::regclass)"
        )
        if not trigger_exists.scalar_one():
            await connection.exec_driver_sql(
                """CREATE TRIGGER trg_gateway_snapshots_immutable
                BEFORE UPDATE OR DELETE ON gateway_snapshots
                FOR EACH ROW EXECUTE FUNCTION context_gateway_snapshot_immutable()"""
            )
        role_exists = await connection.exec_driver_sql(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'context_gateway_public')"
        )
        if role_exists.scalar_one():
            await connection.exec_driver_sql(
                "GRANT SELECT ON gateway_projects, gateway_snapshots, gateway_api_keys, gateway_key_acls, gateway_sessions TO context_gateway_public"
            )
            await connection.exec_driver_sql(
                "GRANT UPDATE (consumed_at) ON gateway_api_keys TO context_gateway_public"
            )
            await connection.exec_driver_sql(
                "GRANT UPDATE (last_used_at, window_started_at, window_count) ON gateway_api_keys TO context_gateway_public"
            )
            await connection.exec_driver_sql(
                "GRANT INSERT, UPDATE ON gateway_sessions TO context_gateway_public"
            )
            await connection.exec_driver_sql(
                "GRANT INSERT ON gateway_audit_events TO context_gateway_public"
            )
            await connection.exec_driver_sql(
                "GRANT SELECT ON gateway_site_control TO context_gateway_public"
            )
            await connection.exec_driver_sql(
                "REVOKE INSERT, UPDATE, DELETE ON gateway_site_control FROM context_gateway_public"
            )
            await connection.exec_driver_sql(
                "REVOKE ALL ON gateway_site_control_audit FROM context_gateway_public"
            )


async def _site_enabled(db: AsyncSession) -> bool:
    enabled = await db.scalar(select(SiteControlState.enabled).where(SiteControlState.id == 1))
    return bool(enabled) if enabled is not None else False


def _session_dependency(factory: async_sessionmaker[AsyncSession]):
    async def dependency() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return dependency


def _bearer(header: str | None) -> str:
    if not header:
        raise HTTPException(
            status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    scheme, separator, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not separator or not credential or " " in credential:
        raise HTTPException(
            status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    return credential


async def _authorize(
    session: AsyncSession,
    authorization: str | None,
    settings: Settings,
    *,
    project_id: UUID | None,
    scope: str = "read",
) -> tuple[Session, list[UUID]]:
    raw = _bearer(authorization)
    try:
        return await authenticate_session(
            session,
            raw,
            project_id=project_id,
            required_scope=scope,
            rate_limit_per_minute=settings.rate_limit_per_minute,
        )
    except AuthenticationError as session_error:
        try:
            return await authenticate_api_key(
                session,
                raw,
                project_id=project_id,
                required_scope=scope,
                rate_limit_per_minute=settings.rate_limit_per_minute,
            )
        except AuthenticationError as key_error:
            failure = (
                session_error
                if (key_error.status_code == 401 and session_error.status_code != 401)
                or (
                    key_error.error_code == "unauthorized"
                    and session_error.error_code != "unauthorized"
                )
                else key_error
            )
        if failure.status_code == 401:
            raise HTTPException(
                401, failure.error_code, headers={"WWW-Authenticate": "Bearer"}
            ) from failure
        if failure.status_code == 429:
            raise HTTPException(
                429, "rate limit exceeded", headers={"Retry-After": "60"}
            ) from failure
        raise HTTPException(404, "not found") from failure


def _project_out(
    project: Project,
    *,
    name: str | None = None,
    description: str | None = None,
    latest_snapshot_id: UUID | None = None,
) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        slug=project.slug,
        name=name if name is not None else project.name,
        description=description if description is not None else project.description,
        latest_snapshot_id=(
            latest_snapshot_id if latest_snapshot_id is not None else project.latest_snapshot_id
        ),
    )


async def _credential_project_out(
    db: AsyncSession, principal: Session | APIKey, project: Project
) -> ProjectOut:
    key = principal if isinstance(principal, APIKey) else await db.get(APIKey, principal.key_id)
    if key is None or key.credential_type == "single_use_v1":
        return _project_out(project)
    acl = await db.get(KeyACL, (key.id, project.id))
    if acl is None or acl.snapshot_policy != "pinned":
        return _project_out(project)
    snapshot = await db.get(Snapshot, acl.snapshot_id) if acl.snapshot_id else None
    manifest = snapshot.manifest if snapshot is not None else {}
    # Existing manifests have no immutable metadata. Old grants without grant-time values
    # fall back to non-sensitive stable values instead of current Project metadata.
    name = acl.project_name or manifest.get("project_name") or project.slug
    description = (
        acl.project_description
        if acl.project_description is not None
        else manifest.get("project_description", "")
    )
    return _project_out(
        project,
        name=name,
        description=description,
        latest_snapshot_id=acl.snapshot_id,
    )


def _snapshot_out(snapshot: Snapshot) -> SnapshotOut:
    return SnapshotOut(
        id=snapshot.id,
        project_id=snapshot.project_id,
        schema_version=snapshot.manifest.get("schema_version", 0),
        version=snapshot.manifest.get("version", 0),
        created_at=snapshot.published_at,
        status="revoked" if snapshot.revoked_at is not None else "active",
        source_refs=snapshot.source_refs,
        source_revision=snapshot.source_revision,
        generated_at=snapshot.generated_at,
        verified_at=snapshot.verified_at,
        published_at=snapshot.published_at,
        manifest_sha256=snapshot.manifest_sha256,
        documents=snapshot.manifest["documents"],
    )


def _negotiate(accept: str | None, markdown: str, data: Any) -> Response:
    accepted = [
        part.split(";", 1)[0].strip().lower() for part in (accept or "application/json").split(",")
    ]
    if "text/markdown" in accepted and "application/json" not in accepted:
        return Response(markdown, media_type="text/markdown; charset=utf-8")
    if any(item in {"application/json", "*/*", "application/*"} for item in accepted):
        return JSONResponse(data)
    if "text/markdown" in accepted:
        return Response(markdown, media_type="text/markdown; charset=utf-8")
    raise HTTPException(406, "supported Accept types: text/markdown, application/json")


async def _project_by_id(
    session: AsyncSession, project_id: UUID, allowed_ids: list[UUID]
) -> Project:
    if project_id not in allowed_ids:
        raise HTTPException(404, "not found")
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "not found")
    return project


async def _credential_snapshot(
    db: AsyncSession,
    principal: Session | APIKey,
    project: Project,
    requested_id: UUID | None,
) -> Snapshot | None:
    key = principal if isinstance(principal, APIKey) else await db.get(APIKey, principal.key_id)
    if key is None or key.credential_type == "single_use_v1":
        return await get_snapshot(db, project, requested_id)
    acl = await db.get(KeyACL, (key.id, project.id))
    if acl is None:
        return None
    if acl.snapshot_policy == "pinned":
        if requested_id is not None and requested_id != acl.snapshot_id:
            return None
        return await get_snapshot(db, project, acl.snapshot_id)
    if requested_id is not None and requested_id != project.latest_snapshot_id:
        return None
    return await get_snapshot(db, project, project.latest_snapshot_id)


def create_public_app(settings: Settings | None = None) -> FastAPI:
    config = _settings(settings)
    factory = make_session_factory(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.initialize_schema:
            await _initialize_schema(factory)
        async with app.state.mcp_server.session_manager.run():
            yield
        await factory.kw["bind"].dispose()

    app = FastAPI(title="Context Gateway", version="1.0.0", lifespan=lifespan)
    app.state.settings = config
    app.state.session_factory = factory
    db_dep = _session_dependency(factory)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.middleware("http")
    async def enforce_site_enabled(request: Request, call_next):
        path = request.url.path
        if path == "/api/v1/site":
            return await call_next(request)
        if path.startswith(("/api/v1/", "/mcp")) and (
            request.headers.get("authorization") or path.startswith("/api/v1/share")
        ):
            async with factory() as db:
                enabled = await _site_enabled(db)
            if not enabled:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "site_temporarily_unavailable"},
                    headers={"Cache-Control": "no-store", "Retry-After": "60"},
                )
        response = await call_next(request)
        if path.startswith("/api/v1/share"):
            response.headers.update(
                {
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Robots-Tag": "noindex, nofollow, noarchive",
                    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                }
            )
        return response

    @app.get("/api/v1/site", include_in_schema=True)
    async def public_site_status(response: Response, db: AsyncSession = Depends(db_dep)):
        # Deliberately exposes only the global switch, never project or document metadata.
        response.headers["Cache-Control"] = "no-store"
        return {"enabled": await _site_enabled(db)}

    @app.middleware("http")
    async def require_session_for_mcp(request: Request, call_next):
        if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
            try:
                raw = _bearer(request.headers.get("authorization"))
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": "unauthorized"},
                    headers=exc.headers,
                )
            async with factory() as db:
                auth_error = await session_validation_error(db, raw)
                if auth_error is not None:
                    return JSONResponse(
                        status_code=401,
                        content={"detail": auth_error},
                        headers={"WWW-Authenticate": "Bearer"},
                    )
        return await call_next(request)

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        return JSONResponse(status_code=409, content={"detail": "snapshot integrity check failed"})

    @app.post("/api/v1/auth/exchange", response_model=ExchangeResponse)
    async def auth_exchange(
        authorization: str | None = Header(default=None), db: AsyncSession = Depends(db_dep)
    ):
        raw = _bearer(authorization)
        try:
            token, expiry = await exchange_key(db, raw)
        except AuthenticationError as exc:
            raise HTTPException(
                401, exc.error_code, headers={"WWW-Authenticate": "Bearer"}
            ) from exc
        return ExchangeResponse(access_token=token, expires_at=expiry)

    async def _share_session(db: AsyncSession, request: Request, *, scope: str = "read"):
        raw = request.cookies.get("context_gateway_share")
        if not raw:
            raise HTTPException(401, "share_session_required")
        try:
            principal, ids = await authenticate_session(
                db,
                raw,
                project_id=None,
                required_scope=scope,
                rate_limit_per_minute=config.rate_limit_per_minute,
                allow_share=True,
            )
        except AuthenticationError as exc:
            raise HTTPException(401, exc.error_code) from exc
        key = await db.get(APIKey, principal.key_id)
        if key is None or key.credential_type != "share_link_v2":
            raise HTTPException(401, "audience_mismatch")
        if len(ids) != 1:
            raise HTTPException(404, "not found")
        return principal, key, ids[0]

    @app.post("/api/v1/share/exchange")
    async def share_exchange(
        request: Request, response: Response, db: AsyncSession = Depends(db_dep)
    ):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise HTTPException(415, "unsupported_media_type")
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(403, "cross_site_request")
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        forwarded_proto = (
            request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
        )
        if not origin or origin.rstrip("/").lower() != f"{forwarded_proto}://{host}".lower():
            raise HTTPException(403, "cross_site_request")
        try:
            body = await request.json()
            raw = body.get("token") if isinstance(body, dict) else None
        except (ValueError, UnicodeDecodeError):
            raw = None
        if not isinstance(raw, str) or not raw or len(raw) > 256:
            raise HTTPException(401, "unauthorized")
        try:
            token, expiry = await exchange_share_link(db, raw)
        except AuthenticationError as exc:
            raise HTTPException(401, exc.error_code) from exc
        response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            "context_gateway_share",
            token,
            expires=expiry,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/api/v1/share",
        )
        return {"expires_at": expiry.isoformat()}

    @app.get("/api/v1/share/context")
    async def share_context(request: Request, db: AsyncSession = Depends(db_dep)):
        principal, key, project_id = await _share_session(db, request)
        project = await _project_by_id(db, project_id, [project_id])
        snapshot = await _credential_snapshot(db, principal, project, None)
        if snapshot is None:
            raise HTTPException(404, "not found")
        acl = await db.get(KeyACL, (key.id, project_id))
        project_out = await _credential_project_out(db, principal, project)
        return {
            "project": project_out.model_dump(mode="json"),
            "snapshot": _snapshot_out(snapshot).model_dump(mode="json"),
            "documents": read_all_documents(snapshot),
            "link": {
                "label": key.label,
                "expires_at": key.expires_at.isoformat(),
                "snapshot_policy": acl.snapshot_policy,
                "scopes": acl.scopes,
            },
            "session_expires_at": principal.expires_at.isoformat(),
        }

    @app.get("/api/v1/share/export")
    async def share_export(
        request: Request,
        format: Literal["markdown"] = "markdown",
        db: AsyncSession = Depends(db_dep),
    ):
        principal, _, project_id = await _share_session(db, request, scope="export")
        project = await _project_by_id(db, project_id, [project_id])
        snapshot = await _credential_snapshot(db, principal, project, None)
        if snapshot is None:
            raise HTTPException(404, "not found")
        body = "\n\n".join(
            f"# {doc['title']}\n\n{doc['content']}" for doc in read_all_documents(snapshot)
        )
        return Response(body, media_type="text/markdown; charset=utf-8")

    @app.post("/api/v1/share/logout")
    async def share_logout(
        request: Request, response: Response, db: AsyncSession = Depends(db_dep)
    ):
        raw = request.cookies.get("context_gateway_share")
        if raw:
            token_hash = hash_credential(raw)
            record = await db.scalar(select(Session).where(Session.credential_hash == token_hash))
            if record is not None:
                record.revoked_at = datetime.now(UTC)
        response.delete_cookie(
            "context_gateway_share",
            path="/api/v1/share",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return {"logged_out": True}

    @app.get("/api/v1/projects", response_model=list[ProjectOut])
    async def list_projects(
        authorization: str | None = Header(default=None), db: AsyncSession = Depends(db_dep)
    ):
        principal, ids = await _authorize(db, authorization, config, project_id=None)
        if not ids:
            return []
        rows = await db.scalars(select(Project).where(Project.id.in_(ids)))
        projects = [await _credential_project_out(db, principal, item) for item in rows]
        return sorted(projects, key=lambda item: item.name)

    @app.get("/api/v1/projects/{project_id}")
    async def get_project(
        project_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        principal, ids = await _authorize(db, authorization, config, project_id=project_id)
        project = await _project_by_id(db, project_id, ids)
        output = await _credential_project_out(db, principal, project)
        return _negotiate(
            request.headers.get("accept"),
            f"# {output.name}\n\n{output.description}\n",
            output.model_dump(mode="json"),
        )

    @app.get("/api/v1/projects/{project_id}/snapshots", response_model=list[SnapshotOut])
    async def list_snapshots(
        project_id: UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        principal, ids = await _authorize(db, authorization, config, project_id=project_id)
        project = await _project_by_id(db, project_id, ids)
        key = principal if isinstance(principal, APIKey) else await db.get(APIKey, principal.key_id)
        if key is not None and key.credential_type != "single_use_v1":
            snapshot = await _credential_snapshot(db, principal, project, None)
            if snapshot is None:
                return []
            return [_snapshot_out(snapshot)]
        rows = await db.scalars(
            select(Snapshot)
            .where(Snapshot.project_id == project_id, Snapshot.revoked_at.is_(None))
            .order_by(Snapshot.published_at.desc())
        )
        return [_snapshot_out(item) for item in rows]

    @app.get("/api/v1/projects/{project_id}/context")
    async def get_context(
        project_id: UUID,
        request: Request,
        snapshot_id: UUID | None = None,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        principal, ids = await _authorize(db, authorization, config, project_id=project_id)
        project = await _project_by_id(db, project_id, ids)
        snapshot = await _credential_snapshot(db, principal, project, snapshot_id)
        if snapshot is None:
            raise HTTPException(404, "not found")
        docs = read_all_documents(snapshot)
        project_out = await _credential_project_out(db, principal, project)
        body = "\n\n".join(f"## {doc['title']}\n\n{doc['content']}" for doc in docs)
        md = f"# {project_out.name}\n\n{body}\n"
        data = {
            "project": project_out.model_dump(mode="json"),
            "snapshot": _snapshot_out(snapshot).model_dump(mode="json"),
            "documents": docs,
        }
        return _negotiate(request.headers.get("accept"), md, data)

    @app.get("/api/v1/projects/{project_id}/documents/{doc_id}")
    async def get_document(
        project_id: UUID,
        doc_id: str,
        request: Request,
        snapshot_id: UUID | None = None,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        principal, ids = await _authorize(db, authorization, config, project_id=project_id)
        project = await _project_by_id(db, project_id, ids)
        snapshot = await _credential_snapshot(db, principal, project, snapshot_id)
        document = read_document(snapshot, doc_id) if snapshot else None
        if document is None:
            raise HTTPException(404, "not found")
        return _negotiate(
            request.headers.get("accept"),
            f"# {document['title']}\n\n{document['content']}",
            document,
        )

    @app.get("/api/v1/projects/{project_id}/search")
    async def search_project(
        project_id: UUID,
        q: str = Query(min_length=1, max_length=300),
        snapshot_id: UUID | None = None,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        principal, ids = await _authorize(
            db, authorization, config, project_id=project_id, scope="search"
        )
        project = await _project_by_id(db, project_id, ids)
        snapshot = await _credential_snapshot(db, principal, project, snapshot_id)
        if snapshot is None:
            raise HTTPException(404, "not found")
        needle = q.casefold()
        results = []
        for doc in read_all_documents(snapshot):
            count = doc["content"].casefold().count(needle)
            if count:
                results.append(
                    {
                        "id": doc["id"],
                        "title": doc["title"],
                        "snippet": doc["content"][:500],
                        "matches": count,
                    }
                )
        results.sort(key=lambda item: (-item["matches"], item["id"]))
        return {
            "project_id": project.id,
            "snapshot_id": snapshot.id,
            "query": q,
            "results": results,
        }

    @app.get("/api/v1/projects/{project_id}/export")
    async def export_project(
        project_id: UUID,
        format: Literal["markdown", "json", "zip"],
        snapshot_id: UUID | None = None,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        principal, ids = await _authorize(
            db, authorization, config, project_id=project_id, scope="export"
        )
        project = await _project_by_id(db, project_id, ids)
        snapshot = await _credential_snapshot(db, principal, project, snapshot_id)
        if snapshot is None:
            raise HTTPException(404, "not found")
        docs = read_all_documents(snapshot)
        if format == "json":
            return JSONResponse(
                {
                    "project": _project_out(project).model_dump(mode="json"),
                    "snapshot": _snapshot_out(snapshot).model_dump(mode="json"),
                    "documents": docs,
                }
            )
        if format == "markdown":
            return Response(
                "\n\n".join(f"# {doc['title']}\n\n{doc['content']}" for doc in docs),
                media_type="text/markdown; charset=utf-8",
            )
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json", json.dumps(snapshot.manifest, ensure_ascii=False, indent=2)
            )
            for doc in docs:
                archive.writestr(f"documents/{doc['id']}.{doc['extension']}", doc["content"])
        return Response(
            stream.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{project.slug}-{snapshot.id}.zip"'
            },
        )

    mcp_server = _build_mcp_server(factory, config)
    from mcp.server.transport_security import TransportSecuritySettings

    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        session_idle_timeout=600,
        max_sessions=1000,
        transport_security=TransportSecuritySettings(allowed_hosts=list(config.mcp_allowed_hosts)),
    )
    app.state.mcp_server = mcp_server
    app.mount("/", mcp_app)
    return app


def _build_mcp_server(factory: async_sessionmaker[AsyncSession], settings: Settings):
    from sqlalchemy import select

    server = MCPServer("Context Gateway", version="1.0.0")
    read_only = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @asynccontextmanager
    async def authorized(ctx: Context, project_id: UUID | None, scope: str):
        headers = ctx.headers or {}
        auth_header = next(
            (value for key, value in headers.items() if key.lower() == "authorization"), None
        )
        async with factory() as db:
            principal, project_ids = await _authorize(
                db, auth_header, settings, project_id=project_id, scope=scope
            )
            try:
                yield db, principal, project_ids
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    @server.tool(annotations=read_only)
    async def list_projects(ctx: Context) -> list[dict[str, Any]]:
        async with authorized(ctx, None, "read") as (db, principal, ids):
            rows = await db.scalars(select(Project).where(Project.id.in_(ids))) if ids else []
            projects = [await _credential_project_out(db, principal, row) for row in rows]
            return [
                item.model_dump(mode="json")
                for item in sorted(projects, key=lambda item: item.name)
            ]

    @server.tool(annotations=read_only)
    async def get_project_context(
        project_id: str, ctx: Context, snapshot_id: str | None = None
    ) -> dict[str, Any]:
        target = UUID(project_id)
        selected = UUID(snapshot_id) if snapshot_id else None
        async with authorized(ctx, target, "read") as (db, principal, ids):
            project = await _project_by_id(db, target, ids)
            snapshot = await _credential_snapshot(db, principal, project, selected)
            if snapshot is None:
                raise ValueError("not found")
            return {
                "project": (await _credential_project_out(db, principal, project)).model_dump(
                    mode="json"
                ),
                "snapshot": _snapshot_out(snapshot).model_dump(mode="json"),
                "documents": read_all_documents(snapshot),
            }

    @server.tool(annotations=read_only)
    async def search_project_context(
        project_id: str, query: str, ctx: Context, snapshot_id: str | None = None
    ) -> dict[str, Any]:
        target = UUID(project_id)
        selected = UUID(snapshot_id) if snapshot_id else None
        async with authorized(ctx, target, "search") as (db, principal, ids):
            project = await _project_by_id(db, target, ids)
            snapshot = await _credential_snapshot(db, principal, project, selected)
            if snapshot is None:
                raise ValueError("not found")
            needle = query.casefold()
            matches = [
                {
                    "id": d["id"],
                    "title": d["title"],
                    "snippet": d["content"][:500],
                    "matches": d["content"].casefold().count(needle),
                }
                for d in read_all_documents(snapshot)
            ]
            return {
                "project_id": str(project.id),
                "snapshot_id": str(snapshot.id),
                "query": query,
                "results": [m for m in matches if m["matches"]],
            }

    @server.tool(annotations=read_only)
    async def get_document(
        project_id: str, document_id: str, ctx: Context, snapshot_id: str | None = None
    ) -> dict[str, Any]:
        target = UUID(project_id)
        selected = UUID(snapshot_id) if snapshot_id else None
        async with authorized(ctx, target, "read") as (db, principal, ids):
            project = await _project_by_id(db, target, ids)
            snapshot = await _credential_snapshot(db, principal, project, selected)
            document = read_document(snapshot, document_id) if snapshot else None
            if document is None:
                raise ValueError("not found")
            return document

    @server.tool(annotations=read_only)
    async def export_project_context(
        project_id: str, ctx: Context, snapshot_id: str | None = None
    ) -> str:
        target = UUID(project_id)
        selected = UUID(snapshot_id) if snapshot_id else None
        async with authorized(ctx, target, "export") as (db, principal, ids):
            project = await _project_by_id(db, target, ids)
            snapshot = await _credential_snapshot(db, principal, project, selected)
            if snapshot is None:
                raise ValueError("not found")
            return "\n\n".join(
                f"# {doc['title']}\n\n{doc['content']}" for doc in read_all_documents(snapshot)
            )

    return server


def create_control_app(settings: Settings | None = None) -> FastAPI:
    config = _settings(settings)
    factory = make_session_factory(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await _initialize_schema(factory)
        yield
        await factory.kw["bind"].dispose()

    app = FastAPI(
        title="Context Gateway Control",
        version="1.0.0",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.session_factory = factory
    db_dep = _session_dependency(factory)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    def control_auth(authorization: str | None):
        supplied = _bearer(authorization)
        if not config.control_token or not hmac.compare_digest(supplied, config.control_token):
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/control/v1/site")
    async def site_status(
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        enabled = await _site_enabled(db)
        db.add(SiteControlAudit(event="site_status_read", enabled=enabled))
        return {"enabled": enabled}

    @app.patch("/control/v1/site")
    async def set_site_status(
        body: SiteEnabledRequest,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        state = await db.get(SiteControlState, 1)
        if state is None:
            state = SiteControlState(id=1, enabled=body.enabled)
            db.add(state)
        else:
            state.enabled = body.enabled
            state.updated_at = func.now()
        db.add(SiteControlAudit(event="site_status_changed", enabled=body.enabled))
        await db.flush()
        return {"enabled": state.enabled}

    @app.post("/control/v1/snapshots", status_code=201)
    async def publish(
        body: SnapshotPublishRequest,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        payload = body.model_dump(mode="python")
        try:
            snapshot = await publish_snapshot(db, config.snapshot_dir, payload)
        except IdempotencyConflictError as exc:
            raise HTTPException(409, "operation_id payload conflict") from exc
        except ProjectSharingRevokedError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (PublishError, IntegrityError) as exc:
            raise HTTPException(
                422
                if isinstance(exc, PublishError) and not isinstance(exc, IntegrityError)
                else 409,
                str(exc),
            ) from exc
        if body.operation_id is not None:
            operation = await db.get(PublishOperation, body.operation_id)
            if operation is not None and operation.receipt is not None:
                return operation.receipt
        return _snapshot_out(snapshot).model_dump(mode="json")

    @app.post("/control/v1/snapshots/{snapshot_id}/revoke")
    async def revoke_snapshot_endpoint(
        snapshot_id: UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        if not await revoke_snapshot(db, snapshot_id):
            raise HTTPException(404, "not found")
        return {"revoked": True, "snapshot_id": snapshot_id}

    @app.post("/control/v1/projects/{project_id}/revoke")
    async def revoke_project_endpoint(
        project_id: UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        result = await revoke_project_sharing(db, project_id)
        if result is None:
            raise HTTPException(404, "not found")
        revoked_at, snapshot_count = result
        return {
            "project_id": str(project_id),
            "revoked": True,
            "revoked_at": revoked_at.isoformat(),
            "snapshots_revoked": snapshot_count,
        }

    @app.post("/control/v1/keys", status_code=201)
    async def issue_key_endpoint(
        body: IssueKeyRequest,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        scopes = {project_id: grant.scopes for project_id, grant in body.projects.items()}
        try:
            key, raw = await issue_api_key(db, label=body.label, project_scopes=scopes)
        except ProjectSharingRevokedError as exc:
            raise HTTPException(409, str(exc)) from exc
        except PublishError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"key_id": str(key.id), "key": raw, "expires_at": key.expires_at.isoformat()}

    @app.post("/control/v2/credentials", status_code=201)
    async def issue_v2_credential_endpoint(
        body: IssueCredentialV2Request,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        grants = {
            project_id: {
                "scopes": grant.scopes,
                "snapshot_policy": grant.snapshot_policy,
                "snapshot_id": grant.snapshot_id,
            }
            for project_id, grant in body.projects.items()
        }
        try:
            _key, raw, receipt = await issue_v2_credential(
                db,
                label=body.label,
                credential_type=body.credential_type,
                ttl_days=body.ttl_days,
                project_grants=grants,
            )
        except ProjectSharingRevokedError as exc:
            raise HTTPException(409, "project sharing has been revoked") from exc
        except PublishError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**receipt, "key": raw}

    @app.post("/control/v2/credentials/status")
    async def credential_status_endpoint(
        body: CredentialStatusRequest,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        keys = list(await db.scalars(select(APIKey).where(APIKey.id.in_(body.key_ids))))
        acls = list(await db.scalars(select(KeyACL).where(KeyACL.key_id.in_(body.key_ids))))
        by_key: dict[UUID, list[KeyACL]] = {}
        for acl in acls:
            by_key.setdefault(acl.key_id, []).append(acl)
        return {
            "credentials": [
                {
                    "key_id": str(key.id),
                    "credential_type": key.credential_type or "single_use_v1",
                    "label": key.label,
                    "created_at": key.created_at.isoformat() if key.created_at else None,
                    "expires_at": key.expires_at.isoformat(),
                    "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
                    "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
                    "projects": {
                        str(acl.project_id): {
                            "scopes": acl.scopes,
                            "snapshot_policy": acl.snapshot_policy or "latest",
                            "snapshot_id": str(acl.snapshot_id) if acl.snapshot_id else None,
                        }
                        for acl in by_key.get(key.id, [])
                    },
                }
                for key in keys
            ]
        }

    @app.post("/control/v1/keys/{key_id}/revoke")
    async def revoke_key_endpoint(
        key_id: UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        control_auth(authorization)
        if not await revoke_api_key(db, key_id):
            raise HTTPException(404, "not found")
        return {"revoked": True, "key_id": key_id}

    return app
