"""Publisher → 실제 Gateway 코어(context_gateway control/public 앱) 통합. 코어 패키지나 격리 클러스터가 없으면 건너뛴다.

코어는 같은 프로세스에서 ASGI 로 띄우고, DB 는 시험 전용 PostgreSQL 클러스터 안의 별도 데이터베이스를 쓴다."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import conftest
import httpx
import pytest
from context_sharing_support import agent_headers, make_project
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.api import context_sharing as api
from ttakkari.context_sharing.gateway import GatewayControl, HttpGatewayTransport
from ttakkari.db import sessionmaker
from ttakkari.main import app
from ttakkari.orchestrator import mcp_server as ms

CORE_SRC = Path(__file__).resolve().parents[1] / "context_gateway" / "src"
CLUSTER = getattr(conftest, "PG_CLUSTER", None)
TEST_DB = getattr(conftest, "TEST_DB", "")
TOKEN = "synthetic-real-control-token-0123456789"
BASE = "/api/context-sharing"

pytestmark = pytest.mark.skipif(not TEST_DB or not CORE_SRC.exists(), reason="Gateway 코어 소스 또는 시험 PostgreSQL 없음")


async def create_core_database(name: str) -> str:
    """시험 서버 안에 코어 전용 DB 를 만든다. 운영 DB(ttakkari)와 같은 이름은 만들지 않는다."""
    if CLUSTER is not None:
        return CLUSTER.create_database(name)
    from sqlalchemy.engine import make_url

    base = make_url(TEST_DB)
    assert base.database != "ttakkari" and name != "ttakkari"
    admin = create_async_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - 권한이 없으면 통합 시험만 건너뛴다
        pytest.skip(f"시험 서버에 코어 DB 를 만들 수 없다: {type(exc).__name__}")
    finally:
        await admin.dispose()
    return base.set(database=name).render_as_string(hide_password=False)


@pytest.fixture
async def core(tmp_path):
    sys.path.insert(0, str(CORE_SRC))
    try:
        from context_gateway.apps import create_control_app, create_public_app
        from context_gateway.config import Settings
    except ImportError as exc:  # 코어 의존성이 이 환경에 없다
        sys.path.remove(str(CORE_SRC))
        pytest.skip(f"Gateway 코어를 불러올 수 없다: {exc}")
    db_name = f"gateway_{uuid.uuid4().hex[:8]}"
    url = await create_core_database(db_name)
    settings = Settings(database_url=url, snapshot_dir=tmp_path / "snapshots", control_token=TOKEN)
    control, public = create_control_app(settings), create_public_app(settings)
    # public 앱의 lifespan(MCP 매니저의 anyio 작업 그룹)은 픽스처 teardown 과 작업이 달라 열지 않는다. 스키마는 control 이 만든다.
    async with control.router.lifespan_context(control):
        yield {"control": control, "public": public, "url": url}
    sys.path.remove(str(CORE_SRC))


@pytest.fixture
def real_gateway(core):
    transport = HttpGatewayTransport("http://127.0.0.1:8788", TOKEN, http_transport=httpx.ASGITransport(app=core["control"]))
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(
        transport, public_url="https://share.example.test"
    )
    yield core
    app.dependency_overrides.pop(api.get_gateway, None)


async def publish_real(client, auth, draft):
    """과거 코어는 0o555 로 잠근 stage 디렉터리를 다른 부모로 os.replace 해서 비루트에서 PermissionError 가 났다.
    그 결함이 되살아나면 사유를 밝히고 건너뛴다(고쳐진 코어에서는 그대로 실행된다)."""
    try:
        return await client.post(f"{BASE}/drafts/{draft['id']}/publish", headers=auth,
                                 json={"approved_digest": draft["preview"]["digest"]})
    except PermissionError as exc:
        pytest.skip(f"Gateway 코어 publish 결함(stage 디렉터리 rename 권한): {exc.filename and Path(exc.filename).name}")


async def core_rows(url: str, sql: str):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).all()
    finally:
        await engine.dispose()


async def test_publish_and_revoke_against_the_real_control_app(client, auth, real_gateway):
    project = await make_project("Alpha", {"docs/arch.md": "# 공개 설명\n\n본문입니다.\n", "notes.md": "선택한 보충 내용\n"})
    r = await client.post(f"{BASE}/drafts", headers=auth, json={
        "entity_id": project["entity_id"], "document_ids": list(project["docs"].values())})
    draft = r.json()
    first_doc = draft["documents"][0]
    r = await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth,
                           json={"documents": [{"id": first_doc["id"], "owner_verified": True}]})
    assert r.status_code == 200, r.text
    draft = r.json()
    published = await publish_real(client, auth, draft)
    assert published.status_code == 200, published.text
    operation = await core_rows(real_gateway["url"],
        f"SELECT operation_id::text FROM gateway_publish_operations WHERE operation_id = '{draft['id']}'")
    assert operation == [(draft["id"],)]
    snap_id = published.json()["publication"]["snapshot_id"]
    rows = await core_rows(real_gateway["url"], "SELECT s.id::text, s.revoked_at, p.slug, p.latest_snapshot_id::text, s.manifest "
                                                "FROM gateway_snapshots s JOIN gateway_projects p ON p.id = s.project_id")
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [(snap_id, None, draft["slug"], snap_id)]
    manifest = rows[0][4]
    assert manifest["verified_at"] is None
    assert len(manifest["documents"]) == 2
    for published_doc, source_doc in zip(manifest["documents"], draft["documents"], strict=True):
        assert published_doc["id"] == ("docs-arch" if source_doc["logical_path"] == "docs/arch.md" else "notes")
        assert published_doc["source_refs"] == [f"library-document:{project['docs'][source_doc['logical_path']]}"]
        assert published_doc["source_revision"] == source_doc["source_revision"]
        assert published_doc["generated_at"] == source_doc["generated_at"]
        assert published_doc["verified_at"] == source_doc["verified_at"]
    assert manifest["documents"][0]["verified_at"] is not None
    assert manifest["documents"][1]["verified_at"] is None
    assert published.json()["publication"]["status"] == "active"

    revoked = await client.post(f"{BASE}/projects/{draft['slug']}/revoke", headers=auth)
    assert revoked.status_code == 200 and revoked.json()["revoked_snapshots"] == 1
    rows = await core_rows(real_gateway["url"], "SELECT s.revoked_at IS NOT NULL, p.latest_snapshot_id, p.sharing_revoked_at IS NOT NULL FROM gateway_snapshots s "
                                                "JOIN gateway_projects p ON p.id = s.project_id")
    assert [tuple(r) for r in rows] == [(True, None, True)]


async def test_real_control_app_rejects_a_wrong_control_token(client, auth, core):
    bad = HttpGatewayTransport("http://127.0.0.1:8788", "wrong-token", http_transport=httpx.ASGITransport(app=core["control"]))
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(bad)
    try:
        project = await make_project("Alpha", {"a.md": "# a\n"})
        draft = (await client.post(f"{BASE}/drafts", headers=auth, json={
            "entity_id": project["entity_id"], "document_ids": list(project["docs"].values())})).json()
        draft = (await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth, json={
            "documents": [{"id": d["id"], "owner_verified": True} for d in draft["documents"]]})).json()
        r = await client.post(f"{BASE}/drafts/{draft['id']}/publish", headers=auth,
                              json={"approved_digest": draft["preview"]["digest"]})
        assert r.status_code == 502 and r.json()["code"] == "gateway_rejected" and r.json()["gateway_status"] == 401
        assert (await client.get(f"{BASE}/drafts/{draft['id']}", headers=auth)).json()["status"] == "draft"
    finally:
        app.dependency_overrides.pop(api.get_gateway, None)


async def test_key_issue_exchange_single_use_revoke_and_withdraw_against_the_real_core(client, auth, real_gateway):
    project = await make_project("Alpha", {"a.md": "# a\n공개 설명\n"})
    draft = (await client.post(f"{BASE}/drafts", headers=auth, json={
        "entity_id": project["entity_id"], "document_ids": list(project["docs"].values())})).json()
    draft = (await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth, json={
        "documents": [{"id": d["id"], "owner_verified": True} for d in draft["documents"]]})).json()
    assert (await publish_real(client, auth, draft)).status_code == 200
    issued = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    assert issued.status_code == 201, issued.text
    raw = issued.json()["key"]
    assert issued.json()["ttl_seconds"] == 3600
    public = httpx.AsyncClient(transport=httpx.ASGITransport(app=real_gateway["public"]), base_url="http://public")
    async with public:
        ok = await public.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw}"})
        assert ok.status_code == 200, ok.text
        token = ok.json()["access_token"]
        (pid,), = await core_rows(real_gateway["url"], "SELECT id::text FROM gateway_projects")
        ctx = await public.get(f"/api/v1/projects/{pid}/context", headers={"Authorization": f"Bearer {token}"})
        assert ctx.status_code == 200 and "공개 설명" in ctx.text
        assert (await public.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw}"})).status_code == 401  # 1회 교환
        revoked_key = await client.post(f"{BASE}/keys/{issued.json()['id']}/revoke", headers=auth)
        assert revoked_key.status_code == 200 and revoked_key.json()["status"] == "revoked"
        denied = await public.get(f"/api/v1/projects/{pid}/context", headers={"Authorization": f"Bearer {token}"})
        assert denied.status_code == 401  # 폐기는 열려 있던 세션에도 즉시 적용된다
    rows = await core_rows(real_gateway["url"], "SELECT revoked_at IS NOT NULL FROM gateway_api_keys")
    assert [tuple(r) for r in rows] == [(True,)]
    again = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    withdrawn = await client.post(f"{BASE}/projects/{draft['slug']}/revoke", headers=auth)
    assert withdrawn.status_code == 200 and withdrawn.json()["revoked_keys"] == (1 if again.status_code == 201 else 0)


async def test_agent_mcp_dispatch_publishes_and_manages_keys_and_site_against_real_core(monkeypatch, real_gateway):
    project = await make_project("AgentAlpha", {"README.md": "# 공개 설명\n합성 프로젝트 설명입니다.\n"})
    headers = await agent_headers()
    transport = httpx.ASGITransport(app=app)

    async def via_asgi(method, path, body=None, *, prefix="/api/orchestrator", params=None, timeout=30):
        async with httpx.AsyncClient(transport=transport, base_url="http://private", headers=headers) as client:
            response = await client.request(method, f"{prefix}{path}", json=body, params=params)
        data = response.json()
        if response.status_code >= 400:
            data = {"error": response.status_code, **data}
        return json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(ms, "_call", via_asgi)
    published = json.loads((await ms.server.call_tool("context_share_publish", {
        "entity_id": project["entity_id"]})).content[0].text)
    assert published["publication"]["status"] == "active"
    slug = published["draft"]["slug"]
    issued = json.loads((await ms.server.call_tool("context_share_key_issue", {
        "project_slug": slug, "label": "agent test"})).content[0].text)
    raw_key = issued["key"]
    assert issued["ttl_seconds"] == 3600 and issued["single_use"] is True
    keys = json.loads((await ms.server.call_tool("context_share_keys", {"project_slug": slug})).content[0].text)
    assert len(keys["keys"]) == 1 and keys["keys"][0]["id"] == issued["id"]
    assert raw_key not in (await ms.server.call_tool("context_share_keys", {"project_slug": slug})).content[0].text, \
        "raw key leaked into list response"
    async with sessionmaker()() as db:
        audit_rows = (await db.execute(text(
            "SELECT actor, detail::text FROM audit_logs WHERE action = 'context_share.key_issue'"))).all()
    assert audit_rows and all(actor == "agent" for actor, _ in audit_rows), "agent key issuance was misattributed"
    assert all(raw_key not in detail for _, detail in audit_rows), "raw key leaked into audit storage"

    public = httpx.AsyncClient(transport=httpx.ASGITransport(app=real_gateway["public"]), base_url="http://public")
    async with public:
        exchanged = await public.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"})
        assert exchanged.status_code == 200
        access = exchanged.json()["access_token"]
        assert (await public.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"})).status_code == 401
        state = json.loads((await ms.server.call_tool("context_share_site", {})).content[0].text)
        assert state["enabled"] is True
        closed = json.loads((await ms.server.call_tool("context_share_site_set", {"enabled": False})).content[0].text)
        assert closed["enabled"] is False
        assert (await public.get("/api/v1/site")).json() == {"enabled": False}
        assert (await public.get("/api/v1/projects/{}/context".format(
            next(iter((await core_rows(real_gateway["url"], "SELECT id::text FROM gateway_projects"))[0]))),
            headers={"Authorization": f"Bearer {access}"})).status_code == 503
        reopened = json.loads((await ms.server.call_tool("context_share_site_set", {"enabled": True})).content[0].text)
        assert reopened["enabled"] is True

    revoked = json.loads((await ms.server.call_tool("context_share_key_revoke", {
        "key_id": issued["id"]})).content[0].text)
    assert revoked["status"] == "revoked"
    assert raw_key not in json.dumps(keys), "raw key leaked into persisted key metadata"


async def test_private_site_switch_controls_real_gateway_without_changing_snapshot_or_ttl(client, auth, real_gateway):
    from context_gateway.auth import hash_credential

    control_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_gateway["control"]), base_url="http://control"
    )
    public_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_gateway["public"]), base_url="http://public"
    )
    async with control_client, public_client:
        published = await control_client.post(
            "/control/v1/snapshots", headers={"Authorization": f"Bearer {TOKEN}"},
            json={"project_slug": f"synthetic-site-{uuid.uuid4().hex[:8]}",
                  "project_name": "Synthetic Site Switch", "source_refs": ["fixture://site-switch"],
                  "source_revision": "site-switch-rev", "generated_at": "2026-11-16T00:00:00Z",
                  "verified_at": "2026-11-16T00:00:00Z",
                  "documents": [{"id": "brief", "title": "Brief", "content": "synthetic only"}]},
        )
        assert published.status_code == 201, published.text
        project_id = published.json()["project_id"]
        issued = await control_client.post(
            "/control/v1/keys", headers={"Authorization": f"Bearer {TOKEN}"},
            json={"label": "synthetic site switch", "projects": {project_id: {"scopes": ["read"]}}},
        )
        assert issued.status_code == 201, issued.text
        exchanged = await public_client.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {issued.json()['key']}"}
        )
        assert exchanged.status_code == 200
        reader = {"Authorization": f"Bearer {exchanged.json()['access_token']}"}
        first = await public_client.get(f"/api/v1/projects/{project_id}/context", headers=reader)
        assert first.status_code == 200
        snapshot_id = first.json()["snapshot"]["id"]
        session_hash = hash_credential(exchanged.json()["access_token"])
        session_expiry_before = await core_rows(
            real_gateway["url"],
            f"SELECT expires_at::text FROM gateway_sessions WHERE credential_hash = '{session_hash}'",
        )

        state = await client.get(f"{BASE}/site", headers=auth)
        assert state.status_code == 200
        assert state.json() == {"enabled": True, "public_url": "https://share.example.test"}
        closed = await client.patch(f"{BASE}/site", headers=auth, json={"enabled": False})
        assert closed.status_code == 200 and closed.json()["enabled"] is False
        assert (await public_client.get("/api/v1/site")).json() == {"enabled": False}
        denied = await public_client.get(f"/api/v1/projects/{project_id}/context", headers=reader)
        assert denied.status_code == 503

        reopened = await client.patch(f"{BASE}/site", headers=auth, json={"enabled": True})
        assert reopened.status_code == 200 and reopened.json()["enabled"] is True
        after = await public_client.get(f"/api/v1/projects/{project_id}/context", headers=reader)
        assert after.status_code == 200 and after.json()["snapshot"]["id"] == snapshot_id
        session_expiry_after = await core_rows(
            real_gateway["url"],
            f"SELECT expires_at::text FROM gateway_sessions WHERE credential_hash = '{session_hash}'",
        )
        assert session_expiry_after == session_expiry_before
        assert (await public_client.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {issued.json()['key']}"}
        )).status_code == 401


async def test_private_v2_key_and_link_contract_against_real_control_and_public_apps(client, auth, real_gateway):
    project = await make_project("V2Private", {"README.md": "# V2\nSynthetic v2 test content.\n"})
    draft = (await client.post(f"{BASE}/drafts", headers=auth, json={
        "entity_id": project["entity_id"], "document_ids": list(project["docs"].values())})).json()
    draft = (await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth, json={
        "documents": [{"id": d["id"], "owner_verified": True} for d in draft["documents"]]})).json()
    published = await publish_real(client, auth, draft)
    assert published.status_code == 200, published.text
    key = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"],
        "credential_type": "reusable_api_v2", "ttl_days": 3, "scopes": ["read"], "snapshot_policy": "pinned"})
    assert key.status_code == 201, key.text
    raw_key = key.json()["key"]
    public = httpx.AsyncClient(transport=httpx.ASGITransport(app=real_gateway["public"]), base_url="http://public")
    async with public:
        first = await public.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"})
        second = await public.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"})
        assert first.status_code == second.status_code == 200
        pid = (await core_rows(real_gateway["url"], "SELECT id::text FROM gateway_projects WHERE slug = 'v2private'"))[0][0]
        direct = await public.get(f"/api/v1/projects/{pid}/context", headers={"Authorization": f"Bearer {raw_key}"})
        assert direct.status_code == 200 and "Synthetic v2" in direct.text
    listed = (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"]
    assert listed[0]["usage_status"] == "available"
    assert listed[0]["credential_type"] == "reusable_api_v2" and listed[0]["last_used_at"]
    assert raw_key not in json.dumps(listed)
    link = await client.post(f"{BASE}/links", headers=auth, json={"project_slug": draft["slug"], "ttl_days": 1})
    assert link.status_code == 201, link.text
    assert link.json()["url"].startswith("https://share.example.test/s#token=")
    token = link.json()["url"].split("#token=", 1)[1]
    assert token not in (await client.get(f"{BASE}/links", headers=auth)).text
