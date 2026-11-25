"""Private → 실제 Gateway 합성 수락 여정. conftest 시험 DB와 격리 Gateway DB만 쓴다."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import conftest
import httpx
import pytest
from context_sharing_support import make_project
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_context_sharing_real_gateway import create_core_database

from ttakkari.api import context_sharing as private_api
from ttakkari.context_sharing.gateway import GatewayControl, HttpGatewayTransport
from ttakkari.context_sharing.models import ShareDraft, ShareKey, ShareProject
from ttakkari.db import sessionmaker
from ttakkari.generation_service import maintenance_jobs
from ttakkari.main import app

pytestmark = pytest.mark.skipif(
    not getattr(conftest, "TEST_DB", "") or not (Path(__file__).resolve().parents[1] / "context_gateway/src").exists(),
    reason="명시적 Private test DB 또는 Gateway 코어 소스 없음",
)

BASE = "/api/context-sharing"
GATEWAY_TOKEN = "synthetic-private-acceptance-control-token"
CORE_SRC = Path(__file__).resolve().parents[1] / "context_gateway" / "src"


@pytest.fixture
async def gateway_apps(tmp_path):
    """conftest Private DB와 create_core_database가 만든 Gateway DB를 분리한다."""
    from context_gateway.apps import create_control_app, create_public_app
    from context_gateway.config import Settings

    private_url = make_url(conftest.TEST_DB)
    conftest.assert_test_db_isolated(conftest.TEST_DB)
    gateway_url = await create_core_database(f"gateway_{uuid.uuid4().hex[:8]}")
    assert make_url(gateway_url).database != private_url.database
    conftest.assert_test_db_isolated(gateway_url)
    settings = Settings(gateway_url, tmp_path / "snapshots", GATEWAY_TOKEN)
    control, public = create_control_app(settings), create_public_app(settings)
    # Gateway SDK의 MCP lifespan은 자체 task group을 시작하며 test fixture teardown task와 공유하지 않는다.
    async with control.router.lifespan_context(control):
        yield {"control": control, "public": public, "url": gateway_url}


@pytest.fixture
def private_gateway(gateway_apps):
    sys.path.insert(0, str(CORE_SRC))
    transport = HttpGatewayTransport(
        "http://127.0.0.1:8788", GATEWAY_TOKEN,
        http_transport=httpx.ASGITransport(app=gateway_apps["control"]),
    )
    app.dependency_overrides[private_api.get_gateway] = lambda: GatewayControl(
        transport, public_url="https://share.synthetic.test"
    )
    yield gateway_apps
    app.dependency_overrides.pop(private_api.get_gateway, None)
    sys.path.remove(str(CORE_SRC))


async def rows(url: str, sql: str, params: dict | None = None):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params or {})).all()
    finally:
        await engine.dispose()


async def publish(client, auth, entity_id: str, doc_ids: list[str]):
    draft_response = await client.post(
        f"{BASE}/drafts", headers=auth, json={"entity_id": entity_id, "document_ids": doc_ids}
    )
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()
    verified = await client.patch(
        f"{BASE}/drafts/{draft['id']}", headers=auth,
        json={"documents": [{"id": item["id"], "owner_verified": True} for item in draft["documents"]]},
    )
    assert verified.status_code == 200, verified.text
    draft = verified.json()
    result = await client.post(
        f"{BASE}/drafts/{draft['id']}/publish", headers=auth,
        json={"approved_digest": draft["preview"]["digest"]},
    )
    assert result.status_code == 200, result.text
    return draft, result.json()


async def public_client(app_):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app_), base_url="https://gateway.synthetic.test")


@pytest.mark.asyncio
async def test_private_gateway_abc_journey_cookie_bearer_mcp_isolation_and_republish(
    client, auth, private_gateway
):
    gateway = private_gateway
    alpha = await make_project("Acceptance Alpha", {"brief.md": "Alpha snapshot one synthetic"})
    beta = await make_project("Acceptance Beta", {"brief.md": "Beta must stay isolated"})
    gamma = await make_project("Acceptance Gamma", {"brief.md": "Gamma latest journey one"})
    alpha_draft, alpha_pub = await publish(client, auth, alpha["entity_id"], list(alpha["docs"].values()))
    beta_draft, _ = await publish(client, auth, beta["entity_id"], list(beta["docs"].values()))
    gamma_draft, gamma_pub_1 = await publish(client, auth, gamma["entity_id"], list(gamma["docs"].values()))

    # 생략한 v2 TTL은 Private 응답 영수증에서 3일이어야 한다.
    key_response = await client.post(f"{BASE}/keys", headers=auth, json={
        "project_slug": alpha_draft["slug"], "credential_type": "reusable_api_v2", "scopes": ["read"]})
    assert key_response.status_code == 201, key_response.text
    key = key_response.json()
    assert timedelta(days=2, hours=23) < datetime.fromisoformat(key["expires_at"]) - datetime.fromisoformat(key["issued_at"]) < timedelta(days=3, seconds=1)
    alpha_gateway_id = (await rows(gateway["url"], "SELECT id::text FROM gateway_projects WHERE slug = :slug",
                                   {"slug": alpha_draft["slug"]}))[0][0]
    assert key["connect"]["project_id"] == alpha_gateway_id

    search_only = await client.post(f"{BASE}/keys", headers=auth, json={
        "project_slug": alpha_draft["slug"], "credential_type": "reusable_api_v2", "scopes": ["search"]})
    assert search_only.status_code == 201, search_only.text
    assert search_only.json()["connect"]["project_id"] == alpha_gateway_id

    latest_response = await client.post(f"{BASE}/keys", headers=auth, json={
        "project_slug": gamma_draft["slug"], "credential_type": "reusable_api_v2", "scopes": ["read"],
        "snapshot_policy": "latest"})
    assert latest_response.status_code == 201, latest_response.text
    latest = latest_response.json()
    assert latest["snapshot_policy"] == "latest"
    assert timedelta(days=2, hours=23) < datetime.fromisoformat(latest["expires_at"]) - datetime.fromisoformat(latest["issued_at"]) < timedelta(days=3, seconds=1)

    link_response = await client.post(f"{BASE}/links", headers=auth, json={"project_slug": alpha_draft["slug"]})
    assert link_response.status_code == 201, link_response.text
    link = link_response.json()
    assert link["snapshot_policy"] == "pinned"
    assert timedelta(days=2, hours=23) < datetime.fromisoformat(link["expires_at"]) - datetime.fromisoformat(link["issued_at"]) < timedelta(days=3, seconds=1)
    assert link["url"].startswith("https://share.synthetic.test/s#token=")
    link_secret = link["url"].split("#token=", 1)[1]
    assert link_secret not in (await client.get(f"{BASE}/links", headers=auth)).text
    assert key["key"] not in (await client.get(f"{BASE}/keys", headers=auth)).text

    async with await public_client(gateway["public"]) as public:
        # 링크는 재교환·브라우저 세션 반복 열람이 되고 raw token은 응답/cookie에만 필요한 경계로 남는다.
        exchange_headers = {"Origin": "https://gateway.synthetic.test", "Content-Type": "application/json"}
        first = await public.post("/api/v1/share/exchange", json={"token": link_secret}, headers=exchange_headers)
        assert first.status_code == 200, first.text
        first_ctx = await public.get("/api/v1/share/context")
        assert first_ctx.status_code == 200 and "Alpha snapshot one" in first_ctx.text
        second = await public.post("/api/v1/share/exchange", json={"token": link_secret}, headers=exchange_headers)
        assert second.status_code == 200, second.text
        assert (await public.get("/api/v1/share/context")).status_code == 200
        assert link_secret not in first.text and link_secret not in first_ctx.text

        # API Key는 직접 Bearer와 교환 세션 모두 반복 REST 사용 가능.
        raw_key = key["key"]
        rest_headers = {"Authorization": f"Bearer {raw_key}"}
        for _ in range(2):
            direct = await public.get(f"/api/v1/projects/{key['connect']['project_id']}/context", headers=rest_headers)
            assert direct.status_code == 200 and "Alpha snapshot one" in direct.text
        exchange_1 = await public.post("/api/v1/auth/exchange", headers=rest_headers)
        exchange_2 = await public.post("/api/v1/auth/exchange", headers=rest_headers)
        assert exchange_1.status_code == exchange_2.status_code == 200
        session_headers = {"Authorization": f"Bearer {exchange_1.json()['access_token']}"}
        assert (await public.get(f"/api/v1/projects/{key['connect']['project_id']}/context", headers=session_headers)).status_code == 200
        beta_id = (await rows(gateway["url"], "SELECT id::text FROM gateway_projects WHERE slug = :slug",
                              {"slug": beta_draft["slug"]}))[0][0]
        assert (await public.get(f"/api/v1/projects/{beta_id}", headers=rest_headers)).status_code == 404

        # 동일 자격의 MCP 경로는 실제 Gateway Streamable HTTP 앱에 연결한다.
        async with gateway["public"].state.mcp_server.session_manager.run():
            mcp_http = httpx.AsyncClient(headers=rest_headers, transport=httpx.ASGITransport(app=gateway["public"]))
            async with mcp_http:
                transport = streamable_http_client("http://localhost/mcp", http_client=mcp_http)
                async with Client(transport) as sdk:
                    mcp_context = await sdk.call_tool("get_project_context", {"project_id": key["connect"]["project_id"]})
                    assert not mcp_context.is_error
                    assert "Alpha snapshot one" in json.dumps(mcp_context.structured_content)
                    mcp_context_again = await sdk.call_tool("get_project_context", {"project_id": key["connect"]["project_id"]})
                    assert not mcp_context_again.is_error
                    assert mcp_context_again.structured_content["snapshot"] == mcp_context.structured_content["snapshot"]
                    mcp_other = await sdk.call_tool("get_project_context", {"project_id": beta_id})
                    assert mcp_other.is_error

        # 재게시 후 pinned는 첫 snapshot, latest만 두 번째 snapshot으로 이동한다.
        async with sessionmaker()() as db:
            from ttakkari.models import CtxChunk, CtxDocument
            doc = await db.scalar(select(CtxDocument).where(CtxDocument.id == uuid.UUID(gamma["docs"]["brief.md"])))
            chunk = await db.scalar(select(CtxChunk).where(CtxChunk.document_id == doc.id))
            chunk.content = "Gamma latest journey two"
            await db.commit()
        await publish(client, auth, gamma["entity_id"], list(gamma["docs"].values()))
        gamma_id = latest["connect"]["project_id"]
        latest_direct = await public.get(f"/api/v1/projects/{gamma_id}/context", headers={"Authorization": f"Bearer {latest['key']}"})
        assert latest_direct.status_code == 200 and "Gamma latest journey two" in latest_direct.text
        assert (await public.get(f"/api/v1/projects/{gamma_id}/context", params={
            "snapshot_id": gamma_pub_1["publication"]["snapshot_id"]},
            headers={"Authorization": f"Bearer {latest['key']}"})).status_code == 404

        # Alpha를 재게시해도 pinned 자격은 발급 당시 snapshot에 머문다.
        async with sessionmaker()() as db:
            from ttakkari.models import CtxChunk, CtxDocument
            doc = await db.scalar(select(CtxDocument).where(CtxDocument.id == uuid.UUID(alpha["docs"]["brief.md"])))
            chunk = await db.scalar(select(CtxChunk).where(CtxChunk.document_id == doc.id))
            chunk.content = "Alpha snapshot two synthetic"
            await db.commit()
        await publish(client, auth, alpha["entity_id"], list(alpha["docs"].values()))
        pinned_read = await public.get(f"/api/v1/projects/{key['connect']['project_id']}/context", headers=rest_headers)
        assert pinned_read.status_code == 200 and "Alpha snapshot one" in pinned_read.text
        assert "Alpha snapshot two" not in pinned_read.text
        assert pinned_read.json()["snapshot"]["id"] == alpha_pub["publication"]["snapshot_id"]
        assert (await public.get(f"/api/v1/projects/{key['connect']['project_id']}/context", params={
            "snapshot_id": str(uuid.uuid4())}, headers=rest_headers)).status_code == 404

        # 링크 폐기 뒤 이미 발급된 브라우저 cookie도 즉시 차단한다.
        revoke_link = await client.post(f"{BASE}/links/{link['id']}/revoke", headers=auth)
        assert revoke_link.status_code == 200 and revoke_link.json()["status"] == "revoked"
        denied_cookie = await public.get("/api/v1/share/context")
        assert denied_cookie.status_code == 401

    private_rows = await rows(conftest.TEST_DB, "SELECT credential_type, status FROM ctx_share_keys ORDER BY credential_type")
    assert {row[0] for row in private_rows} >= {"reusable_api_v2", "share_link_v2"}


@pytest.mark.asyncio
async def test_private_revoke_intents_retry_after_transport_and_local_commit_failures(
    client, auth, private_gateway, monkeypatch
):
    from ttakkari.context_sharing.gateway import GatewayError

    gateway = private_gateway
    case_id = uuid.uuid4().hex[:10]
    project = await make_project(f"Revoke Retry {case_id}", {"brief.md": "synthetic revoke fixture"})
    draft, _ = await publish(client, auth, project["entity_id"], list(project["docs"].values()))
    issued = await client.post(f"{BASE}/keys", headers=auth, json={
        "project_slug": draft["slug"], "credential_type": "reusable_api_v2", "scopes": ["read"]})
    assert issued.status_code == 201, issued.text
    raw_key, private_key_id = issued.json()["key"], uuid.UUID(issued.json()["id"])
    public = await public_client(gateway["public"])
    async with public:
        exchange = await public.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"})
        assert exchange.status_code == 200
        session_headers = {"Authorization": f"Bearer {exchange.json()['access_token']}"}
        assert (await public.get(f"/api/v1/projects/{issued.json()['connect']['project_id']}/context", headers=session_headers)).status_code == 200

        # Link/key/project intent는 Gateway 통신 장애 동안 pending으로 남고, 아직 Gateway에 전달되지 않은 parent는 열린다.
        link_resp = await client.post(f"{BASE}/links", headers=auth, json={"project_slug": draft["slug"]})
        assert link_resp.status_code == 201
        link = link_resp.json()
        original = app.dependency_overrides[private_api.get_gateway]()
        real_transport = original.require()

        class BrokenTransport:
            async def request(self, method, path, json=None):
                if method == "POST" and (path.endswith("/revoke") or "/revoke" in path):
                    raise GatewayError("transport_unavailable", "synthetic transport unavailable")
                return await real_transport.request(method, path, json)

        app.dependency_overrides[private_api.get_gateway] = lambda: GatewayControl(
            BrokenTransport(), public_url="https://share.synthetic.test")
        key_revoke = await client.post(f"{BASE}/keys/{private_key_id}/revoke", headers=auth)
        assert key_revoke.status_code == 502 and key_revoke.json()["status"] == "revoking"
        link_revoke = await client.post(f"{BASE}/links/{link['id']}/revoke", headers=auth)
        assert link_revoke.status_code == 502 and link_revoke.json()["status"] == "revoking"
        project_revoke = await client.post(f"{BASE}/projects/{draft['slug']}/revoke", headers=auth)
        assert project_revoke.status_code == 502 and project_revoke.json()["status"] == "revoking"
        async with sessionmaker()() as db:
            key_row = await db.get(ShareKey, private_key_id)
            link_row = await db.get(ShareKey, uuid.UUID(link["id"]))
            project_row = await db.scalar(select(ShareProject).where(ShareProject.slug == draft["slug"]))
            assert key_row.status == link_row.status == "revoking"
            assert project_row.revoke_state == "revoking"
        assert (await public.get(f"/api/v1/projects/{issued.json()['connect']['project_id']}/context", headers=session_headers)).status_code == 200
        assert (await public.get(f"/api/v1/projects/{issued.json()['connect']['project_id']}/context", headers={"Authorization": f"Bearer {raw_key}"})).status_code == 200
        blocked_new_issue = await client.post(f"{BASE}/keys", headers=auth, json={
            "project_slug": draft["slug"], "credential_type": "reusable_api_v2", "scopes": ["read"]})
        assert blocked_new_issue.status_code == 409 and blocked_new_issue.json()["code"] == "project_revocation_pending"

        # 재시도 callback은 런타임 maintenance_jobs에 등록된 실제 operation을 호출하고 세 intent를 대사한다.
        app.dependency_overrides[private_api.get_gateway] = lambda: original
        from ttakkari.config import get_settings
        registered = {name: (operation, interval) for name, operation, interval in maintenance_jobs(get_settings())}
        retry_operation, retry_interval = registered["context-sharing-revocations"]
        assert callable(retry_operation) and 10 <= retry_interval <= 60
        from ttakkari import generation_service
        monkeypatch.setattr(generation_service, "sessionmaker", sessionmaker)
        monkeypatch.setattr(generation_service, "get_settings", get_settings, raising=False)
        monkeypatch.setattr("ttakkari.context_sharing.gateway.build_control", lambda: original)
        await retry_operation()
        async with sessionmaker()() as db:
            key_row = await db.get(ShareKey, private_key_id)
            link_row = await db.get(ShareKey, uuid.UUID(link["id"]))
            project_row = await db.scalar(select(ShareProject).where(ShareProject.slug == draft["slug"]))
            assert key_row.status == link_row.status == "revoked"
            assert project_row.revoke_state == "active"
        assert (await public.get(f"/api/v1/projects/{issued.json()['connect']['project_id']}/context", headers=session_headers)).status_code == 401

        # 다음 합성 프로젝트에서는 Gateway 폐기는 성공하지만 Private 완료 commit만 실패한다.
        retry_project = await make_project(f"Local Commit Retry {case_id}", {"brief.md": "local failure fixture"})
        retry_draft, _ = await publish(client, auth, retry_project["entity_id"], list(retry_project["docs"].values()))
        retry_key = await client.post(f"{BASE}/keys", headers=auth, json={
            "project_slug": retry_draft["slug"], "credential_type": "reusable_api_v2", "scopes": ["read"]})
        assert retry_key.status_code == 201
        raw_retry_key = retry_key.json()["key"]
        exchanged = await public.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_retry_key}"})
        assert exchanged.status_code == 200
        retry_id = uuid.UUID(retry_key.json()["id"])
        async with sessionmaker()() as db:
            retry_row = await db.get(ShareKey, retry_id)
            retry_gateway_id = retry_row.gateway_key_id
        original_commit = AsyncSession.commit

        async def fail_revoked_commit(self):
            if any(isinstance(item, ShareKey) and item.status == "revoked" for item in self.dirty):
                raise RuntimeError("synthetic local commit failure after Gateway success")
            return await original_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", fail_revoked_commit)
        failure_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
        response = await failure_client.post(f"{BASE}/keys/{retry_key.json()['id']}/revoke", headers=auth)
        await failure_client.aclose()
        assert response.status_code == 500
        monkeypatch.setattr(AsyncSession, "commit", original_commit)
        async with sessionmaker()() as db:
            pending = await db.get(ShareKey, retry_id)
            assert pending.status == "revoking"
        remote = await rows(gateway["url"], "SELECT revoked_at IS NOT NULL FROM gateway_api_keys WHERE id = :id", {"id": retry_gateway_id})
        assert remote == [(True,)]
        denied = await public.get(f"/api/v1/projects/{retry_key.json()['connect']['project_id']}/context", headers={"Authorization": f"Bearer {exchanged.json()['access_token']}"})
        assert denied.status_code == 401
        await retry_operation()
        async with sessionmaker()() as db:
            reconciled = await db.get(ShareKey, retry_id)
            assert reconciled.status == "revoked"


@pytest.mark.asyncio
async def test_work_v1_v2_issue_and_publish_and_existing_v1_gateway_contract(client, auth, private_gateway):
    gateway = private_gateway
    personal = await make_project("Acceptance Work", {"internal.md": "synthetic internal only"})
    legacy_draft, _ = await publish(client, auth, personal["entity_id"], list(personal["docs"].values()))
    legacy = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": legacy_draft["slug"]})
    assert legacy.status_code == 201
    # A published project's active draft enters the work scope: keep its existing v1 row intact,
    # then verify every new key type is still issued.
    async with sessionmaker()() as db:
        row = await db.get(ShareDraft, uuid.UUID(legacy_draft["id"]))
        row.scope = "work"
        await db.commit()
    for body in (
        {"project_slug": legacy_draft["slug"], "credential_type": "single_use_v1"},
        {"project_slug": legacy_draft["slug"], "credential_type": "reusable_api_v2"},
    ):
        issued = await client.post(f"{BASE}/keys", headers=auth, json=body)
        assert issued.status_code == 201, issued.text

    work = await make_project("Acceptance New Work", {"internal.md": "new internal synthetic only"}, scope="work")
    work_draft_response = await client.post(f"{BASE}/drafts", headers=auth, json={
        "entity_id": work["entity_id"], "document_ids": list(work["docs"].values()), "confirm_work_scope": True})
    assert work_draft_response.status_code == 201, work_draft_response.text
    work_draft = work_draft_response.json()
    work_publish = await client.post(f"{BASE}/drafts/{work_draft['id']}/publish", headers=auth,
                                     json={"approved_digest": work_draft["preview"]["digest"]})
    assert work_publish.status_code == 200, work_publish.text
    listed = (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"]
    preserved = next(item for item in listed if item["id"] == legacy.json()["id"])
    assert preserved["status"] == "active" and preserved["credential_type"] == "single_use_v1"
    assert legacy.json()["ttl_seconds"] == 3600 and legacy.json()["single_use"] is True
    async with await public_client(gateway["public"]) as public:
        headers = {"Authorization": f"Bearer {legacy.json()['key']}"}
        first = await public.post("/api/v1/auth/exchange", headers=headers)
        assert first.status_code == 200
        assert (await public.post("/api/v1/auth/exchange", headers=headers)).status_code == 401
        session = {"Authorization": f"Bearer {first.json()['access_token']}"}
        assert (await public.get(f"/api/v1/projects/{legacy.json()['connect']['project_id']}/context", headers=session)).status_code == 200
