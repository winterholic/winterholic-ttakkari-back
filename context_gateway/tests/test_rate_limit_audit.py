from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from context_gateway.apps import create_control_app, create_public_app
from context_gateway.auth import (
    AuthenticationError,
    authenticate_api_key,
    authenticate_session,
    hash_credential,
)
from context_gateway.config import Settings
from context_gateway.models import APIKey, AuditEvent, KeyACL, Project, Session


@pytest.mark.asyncio
async def test_audit_insert_with_insert_only_role_does_not_require_select(postgres_url):
    """Match the public container's INSERT-only audit grant, not an admin DB role."""
    role = "gateway_audit_test_" + uuid4().hex
    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
                await connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
                await connection.execute(text(f'GRANT INSERT ON gateway_audit_events TO "{role}"'))
                await connection.execute(text(f'SET LOCAL ROLE "{role}"'))
                async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                    event = AuditEvent(event="synthetic_insert_only")
                    db.add(event)
                    await db.flush()
                    event_id = event.id
                    await db.commit()
                async with connection.begin_nested():
                    with pytest.raises(DBAPIError):
                        await connection.execute(
                            text("SELECT created_at FROM gateway_audit_events")
                        )
                    # Roll back the deliberately rejected statement before leaving
                    # the savepoint; the outer transaction still owns the test role.
                    await connection.get_nested_transaction().rollback()
                await connection.execute(text("RESET ROLE"))
                value = await connection.scalar(
                    select(AuditEvent.created_at).where(AuditEvent.id == event_id)
                )
                assert value is not None
            finally:
                await transaction.rollback()  # Removes the test role and event too.
    finally:
        await engine.dispose()


@pytest.fixture
def rate_settings(postgres_url, tmp_path) -> Settings:
    return Settings(postgres_url, tmp_path / "snapshots", "synthetic-rate-token", 2)


@pytest.mark.asyncio
async def test_rate_limit_boundary_and_window_refresh_are_database_backed(
    db_sessions,
) -> None:
    now = datetime(2026, 11, 16, tzinfo=UTC)
    async with db_sessions() as db:
        project = Project(slug="rate-window", name="Rate Window", description="")
        key = APIKey(
            label="synthetic-window",
            credential_hash="a" * 64,
            expires_at=now + timedelta(hours=1),
        )
        db.add_all([project, key])
        await db.flush()
        db.add(KeyACL(key_id=key.id, project_id=project.id, scopes=["read"]))
        session_record = Session(
            key_id=key.id,
            credential_hash=hash_credential("synthetic-session"),
            expires_at=now + timedelta(hours=1),
            window_started_at=now,
            window_count=0,
        )
        db.add(session_record)
        await db.commit()

        for timestamp in (now, now + timedelta(seconds=59)):
            await authenticate_session(
                db,
                "synthetic-session",
                project_id=project.id,
                required_scope="read",
                rate_limit_per_minute=2,
                now=timestamp,
            )
            await db.commit()

        with pytest.raises(AuthenticationError) as rejected:
            await authenticate_session(
                db,
                "synthetic-session",
                project_id=project.id,
                required_scope="read",
                rate_limit_per_minute=2,
                now=now + timedelta(seconds=59),
            )
        assert rejected.value.status_code == 429

        await authenticate_session(
            db,
            "synthetic-session",
            project_id=project.id,
            required_scope="read",
            rate_limit_per_minute=2,
            now=now + timedelta(minutes=1),
        )
        await db.commit()
        await db.refresh(key)
        assert key.window_started_at == now + timedelta(minutes=1)
        assert key.window_count == 1
        await db.refresh(session_record)
        assert session_record.window_count == 0


@pytest.mark.asyncio
async def test_rest_and_mcp_share_counter_and_persist_nonsecret_audit_events(
    rate_settings: Settings,
    db_sessions,
) -> None:
    control = create_control_app(rate_settings)
    public = create_public_app(rate_settings)
    raw_key = ""
    session_token = ""
    async with (
        control.router.lifespan_context(control),
        public.router.lifespan_context(public),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="http://control"
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public), base_url="http://public"
        ) as rest,
    ):
        published = await admin.post(
            "/control/v1/snapshots",
            headers={"Authorization": "Bearer synthetic-rate-token"},
            json={
                "project_slug": f"rate-audit-{uuid4().hex[:8]}",
                "project_name": "Synthetic Rate Audit",
                "source_refs": ["fixture://rate-audit"],
                "source_revision": "rate-audit-rev-1",
                "generated_at": "2026-11-16T00:00:00Z",
                "documents": [{"id": "brief", "title": "Brief", "content": "synthetic"}],
            },
        )
        assert published.status_code == 201, published.text
        project_id = published.json()["project_id"]
        issued = await admin.post(
            "/control/v1/keys",
            headers={"Authorization": "Bearer synthetic-rate-token"},
            json={
                "label": "synthetic rate audit",
                "projects": {project_id: {"scopes": ["read"]}},
            },
        )
        assert issued.status_code == 201, issued.text
        raw_key = issued.json()["key"]
        exchanged = await rest.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert exchanged.status_code == 200, exchanged.text
        session_token = exchanged.json()["access_token"]
        headers = {"Authorization": f"Bearer {session_token}"}

        first_read = await rest.get("/api/v1/projects", headers=headers)
        assert first_read.status_code == 200, first_read.text

        sdk_http = httpx.AsyncClient(
            headers=headers,
            transport=httpx.ASGITransport(app=public),
        )
        async with sdk_http:
            transport = streamable_http_client("http://localhost/mcp", http_client=sdk_http)
            async with Client(transport) as sdk:
                mcp_read = await sdk.call_tool("list_projects", {})
        assert not mcp_read.is_error

        third_read = await rest.get("/api/v1/projects", headers=headers)
        assert third_read.status_code == 429
        assert third_read.headers["retry-after"] == "60"

    async with db_sessions() as db:
        events = list((await db.scalars(select(AuditEvent).order_by(AuditEvent.created_at))).all())
        event_names = [event.event for event in events]
        assert event_names.count("key_issued") == 1
        assert event_names.count("key_exchanged") == 1
        assert event_names.count("api_read") == 2
        assert all(
            raw_key
            not in repr(
                (event.key_id, event.session_id, event.event, event.project_id, event.detail)
            )
            and session_token
            not in repr(
                (event.key_id, event.session_id, event.event, event.project_id, event.detail)
            )
            for event in events
        )


@pytest.mark.asyncio
async def test_reusable_key_and_its_sessions_share_one_rate_limit(db_sessions) -> None:
    now = datetime(2026, 11, 19, tzinfo=UTC)
    async with db_sessions() as db:
        project = Project(slug=f"shared-rate-{uuid4().hex}", name="Shared Rate")
        key = APIKey(
            label="synthetic reusable rate",
            credential_hash=hash_credential("synthetic reusable bearer"),
            expires_at=now + timedelta(days=1),
            credential_type="reusable_api_v2",
        )
        db.add_all([project, key])
        await db.flush()
        db.add(KeyACL(key_id=key.id, project_id=project.id, scopes=["read"]))
        db.add(
            Session(
                key_id=key.id,
                credential_hash=hash_credential("synthetic reusable session"),
                expires_at=now + timedelta(hours=1),
                window_started_at=now,
                window_count=0,
            )
        )
        await db.commit()

        await authenticate_api_key(
            db,
            "synthetic reusable bearer",
            project_id=project.id,
            required_scope="read",
            rate_limit_per_minute=2,
            now=now,
        )
        await db.commit()
        await authenticate_session(
            db,
            "synthetic reusable session",
            project_id=project.id,
            required_scope="read",
            rate_limit_per_minute=2,
            now=now + timedelta(seconds=1),
        )
        await db.commit()
        with pytest.raises(AuthenticationError) as rejected:
            await authenticate_api_key(
                db,
                "synthetic reusable bearer",
                project_id=project.id,
                required_scope="read",
                rate_limit_per_minute=2,
                now=now + timedelta(seconds=2),
            )

    assert rejected.value.status_code == 429
