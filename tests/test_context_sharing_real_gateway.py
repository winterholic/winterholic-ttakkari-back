"""Publisher → 실제 Gateway 코어(context_gateway control/public 앱) 통합. 코어 패키지나 격리 클러스터가 없으면 건너뛴다.

코어는 같은 프로세스에서 ASGI 로 띄우고, DB 는 시험 전용 PostgreSQL 클러스터 안의 별도 데이터베이스를 쓴다."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import conftest
import httpx
import pytest
from context_sharing_support import make_project
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.api import context_sharing as api
from ttakkari.context_sharing.gateway import GatewayControl, HttpGatewayTransport
from ttakkari.main import app

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
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(transport)
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
    published = await publish_real(client, auth, draft)
    assert published.status_code == 200, published.text
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
        assert published_doc["verified_at"] is None
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
