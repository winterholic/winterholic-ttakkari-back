from __future__ import annotations

import hmac
import io
import json
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
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
    authenticate_session,
    exchange_key,
    validate_session,
)
from .config import Settings
from .database import make_session_factory
from .models import Base, Project, Session, SiteControlAudit, SiteControlState, Snapshot
from .publishing import (
    IntegrityError,
    ProjectSharingRevokedError,
    PublishError,
    issue_api_key,
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


class SnapshotPublishRequest(BaseModel):
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
    except AuthenticationError as exc:
        if exc.status_code == 401:
            raise HTTPException(
                401, "unauthorized", headers={"WWW-Authenticate": "Bearer"}
            ) from exc
        if exc.status_code == 429:
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"}) from exc
        raise HTTPException(404, "not found") from exc


def _project_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        slug=project.slug,
        name=project.name,
        description=project.description,
        latest_snapshot_id=project.latest_snapshot_id,
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
        if path.startswith(("/api/v1/", "/mcp")) and request.headers.get("authorization"):
            async with factory() as db:
                enabled = await _site_enabled(db)
            if not enabled:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "site_temporarily_unavailable"},
                    headers={"Cache-Control": "no-store", "Retry-After": "60"},
                )
        return await call_next(request)

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
                if not await validate_session(db, raw):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "unauthorized"},
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
                401, "unauthorized", headers={"WWW-Authenticate": "Bearer"}
            ) from exc
        return ExchangeResponse(access_token=token, expires_at=expiry)

    @app.get("/api/v1/projects", response_model=list[ProjectOut])
    async def list_projects(
        authorization: str | None = Header(default=None), db: AsyncSession = Depends(db_dep)
    ):
        _, ids = await _authorize(db, authorization, config, project_id=None)
        if not ids:
            return []
        rows = await db.scalars(select(Project).where(Project.id.in_(ids)).order_by(Project.name))
        return [_project_out(item) for item in rows]

    @app.get("/api/v1/projects/{project_id}")
    async def get_project(
        project_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        _, ids = await _authorize(db, authorization, config, project_id=project_id)
        project = await _project_by_id(db, project_id, ids)
        return _negotiate(
            request.headers.get("accept"),
            f"# {project.name}\n\n{project.description}\n",
            _project_out(project).model_dump(mode="json"),
        )

    @app.get("/api/v1/projects/{project_id}/snapshots", response_model=list[SnapshotOut])
    async def list_snapshots(
        project_id: UUID,
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(db_dep),
    ):
        _, ids = await _authorize(db, authorization, config, project_id=project_id)
        await _project_by_id(db, project_id, ids)
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
        _, ids = await _authorize(db, authorization, config, project_id=project_id)
        project = await _project_by_id(db, project_id, ids)
        snapshot = await get_snapshot(db, project, snapshot_id)
        if snapshot is None:
            raise HTTPException(404, "not found")
        docs = read_all_documents(snapshot)
        body = "\n\n".join(f"## {doc['title']}\n\n{doc['content']}" for doc in docs)
        md = f"# {project.name}\n\n{body}\n"
        data = {
            "project": _project_out(project).model_dump(mode="json"),
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
        _, ids = await _authorize(db, authorization, config, project_id=project_id)
        project = await _project_by_id(db, project_id, ids)
        snapshot = await get_snapshot(db, project, snapshot_id)
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
        _, ids = await _authorize(db, authorization, config, project_id=project_id, scope="search")
        project = await _project_by_id(db, project_id, ids)
        snapshot = await get_snapshot(db, project, snapshot_id)
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
        _, ids = await _authorize(db, authorization, config, project_id=project_id, scope="export")
        project = await _project_by_id(db, project_id, ids)
        snapshot = await get_snapshot(db, project, snapshot_id)
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
        async with authorized(ctx, None, "read") as (db, _, ids):
            rows = (
                await db.scalars(select(Project).where(Project.id.in_(ids)).order_by(Project.name))
                if ids
                else []
            )
            return [_project_out(row).model_dump(mode="json") for row in rows]

    @server.tool(annotations=read_only)
    async def get_project_context(
        project_id: str, ctx: Context, snapshot_id: str | None = None
    ) -> dict[str, Any]:
        target = UUID(project_id)
        selected = UUID(snapshot_id) if snapshot_id else None
        async with authorized(ctx, target, "read") as (db, _, ids):
            project = await _project_by_id(db, target, ids)
            snapshot = await get_snapshot(db, project, selected)
            if snapshot is None:
                raise ValueError("not found")
            return {
                "project": _project_out(project).model_dump(mode="json"),
                "snapshot": _snapshot_out(snapshot).model_dump(mode="json"),
                "documents": read_all_documents(snapshot),
            }

    @server.tool(annotations=read_only)
    async def search_project_context(
        project_id: str, query: str, ctx: Context, snapshot_id: str | None = None
    ) -> dict[str, Any]:
        target = UUID(project_id)
        selected = UUID(snapshot_id) if snapshot_id else None
        async with authorized(ctx, target, "search") as (db, _, ids):
            project = await _project_by_id(db, target, ids)
            snapshot = await get_snapshot(db, project, selected)
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
        async with authorized(ctx, target, "read") as (db, _, ids):
            project = await _project_by_id(db, target, ids)
            snapshot = await get_snapshot(db, project, selected)
            document = read_document(snapshot, document_id) if snapshot else None
            if document is None:
                raise ValueError("not found")
            return document

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
        except ProjectSharingRevokedError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (PublishError, IntegrityError) as exc:
            raise HTTPException(
                422
                if isinstance(exc, PublishError) and not isinstance(exc, IntegrityError)
                else 409,
                str(exc),
            ) from exc
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
