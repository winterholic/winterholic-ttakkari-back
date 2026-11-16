"""맥락 공유 Private API 통합 시험(격리 PostgreSQL, 합성 데이터, 인메모리 Gateway control 가짜)."""
from __future__ import annotations

import json

import pytest
from context_sharing_support import (
    RAW_KEY_PREFIX,
    FakeGateway,
    agent_headers,
    make_project,
    sha,
)
from sqlalchemy import text

from ttakkari.api import context_sharing as api
from ttakkari.config import get_settings
from ttakkari.context_sharing.gateway import GatewayControl, GatewayUnavailable
from ttakkari.db import sessionmaker
from ttakkari.main import app

pytestmark = pytest.mark.security

BASE = "/api/context-sharing"
DOC = "# 아키텍처\n\n공개 가능한 설명입니다.\n"
RISKY = "# 설정\n\n서버는 http://localhost:8787 에서 돌고 /Users/winterholic/dev/app 에 있다.\n"


@pytest.fixture
def gateway():
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(fake, public_url="https://ctx.example.test")
    yield fake
    app.dependency_overrides.pop(api.get_gateway, None)


async def new_draft(client, auth, project, rels, *, verified=True, **extra):
    """초안을 만든다. 기본은 소유자가 모든 문서를 확인한 상태(Gateway 코어가 verified_at 을 요구한다)."""
    r = await client.post(f"{BASE}/drafts", headers=auth, json={
        "entity_id": project["entity_id"], "document_ids": [project["docs"][x] for x in rels], **extra})
    assert r.status_code == 201, r.text
    draft = r.json()
    if verified:
        r = await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth, json={
            "documents": [{"id": d["id"], "owner_verified": True} for d in draft["documents"]]})
        assert r.status_code == 200, r.text
        draft = r.json()
    return draft


async def publish(client, auth, draft, digest=None):
    return await client.post(f"{BASE}/drafts/{draft['id']}/publish", headers=auth,
                             json={"approved_digest": digest or draft["preview"]["digest"]})


async def published_project(client, auth, gateway, *, name="Alpha", body=DOC):
    project = await make_project(name, {"docs/arch.md": body, "README.md": "# 소개\n안녕\n"})
    draft = await new_draft(client, auth, project, ["docs/arch.md"])
    r = await publish(client, auth, draft)
    assert r.status_code == 200, r.text
    return project, draft, r.json()


async def table_dump() -> str:
    async with sessionmaker()() as db:
        out = []
        for t in ("ctx_share_projects", "ctx_share_drafts", "ctx_share_draft_documents", "ctx_share_publications",
                  "ctx_share_keys", "audit_logs"):
            rows = (await db.execute(text(f'SELECT row_to_json(t)::text FROM "{t}" t'))).scalars().all()
            out.extend(rows)
        return "\n".join(out)


# ---- 인증 경계

OWNER_ROUTES = [
    ("GET", "/status"), ("GET", "/site"), ("PATCH", "/site"), ("GET", "/projects"), ("GET", "/projects/00000000-0000-0000-0000-000000000000/sources"),
    ("GET", "/drafts"), ("POST", "/drafts"), ("GET", "/drafts/00000000-0000-0000-0000-000000000000"),
    ("PATCH", "/drafts/00000000-0000-0000-0000-000000000000"),
    ("POST", "/drafts/00000000-0000-0000-0000-000000000000/publish"),
    ("POST", "/keys"), ("GET", "/keys"), ("POST", "/keys/00000000-0000-0000-0000-000000000000/revoke"),
    ("POST", "/projects/alpha/revoke"),
]


@pytest.mark.parametrize("method,path", OWNER_ROUTES)
async def test_owner_routes_reject_missing_and_agent_credentials(client, method, path):
    anonymous = await client.request(method, BASE + path, json={})
    assert anonymous.status_code == 401
    agent = await client.request(method, BASE + path, json={}, headers=await agent_headers())
    assert agent.status_code == 401


async def test_agent_cannot_publish_or_issue_keys_even_for_existing_drafts(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"])
    agent = await agent_headers()
    r = await client.post(f"{BASE}/drafts/{draft['id']}/publish", headers=agent,
                          json={"approved_digest": draft["preview"]["digest"]})
    assert r.status_code == 401
    assert gateway.calls == []
    # agent 전용 경로에는 게시·키 경로 자체가 없다.
    for method, path in [("POST", f"/drafts/{draft['id']}/publish"), ("POST", "/keys"), ("PATCH", f"/drafts/{draft['id']}"),
                         ("POST", "/projects/alpha/revoke")]:
        r = await client.request(method, f"/api/agent/context-sharing{path}", headers=agent, json={})
        assert r.status_code in (404, 405), (method, path, r.status_code)


async def test_owner_site_control_reads_and_changes_gateway_state(client, auth, gateway):
    status = await client.get(f"{BASE}/site", headers=auth)
    assert status.status_code == 200
    assert status.json() == {"enabled": True, "public_url": "https://ctx.example.test"}
    changed = await client.patch(f"{BASE}/site", headers=auth, json={"enabled": False})
    assert changed.status_code == 200
    assert changed.json() == {"enabled": False, "public_url": "https://ctx.example.test"}
    assert [(method, path) for method, path, _ in gateway.calls] == [
        ("GET", "/control/v1/site"), ("PATCH", "/control/v1/site")]


async def test_owner_site_control_reports_gateway_failure_separately_from_config(client, auth, gateway):
    gateway.fail_with = GatewayUnavailable("ConnectError")
    response = await client.get(f"{BASE}/site", headers=auth)
    assert response.status_code == 503
    assert response.json()["code"] == "gateway_unavailable"

    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(
        None, status={"configured": False, "missing": ["context_gateway_control_url"]}
    )
    unconfigured = await client.get(f"{BASE}/site", headers=auth)
    assert unconfigured.status_code == 503
    assert unconfigured.json()["code"] == "gateway_not_configured"

    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(gateway)
    missing_public_url = await client.get(f"{BASE}/site", headers=auth)
    assert missing_public_url.status_code == 503
    assert missing_public_url.json()["code"] == "gateway_not_configured"
    assert "context_gateway_public_url" in missing_public_url.json()["gateway_status"]["missing"]


# ---- 선택한 문서만

async def test_projects_and_sources_list_only_logical_names(client, auth, gateway):
    project = await make_project("Alpha", {"docs/arch.md": DOC, "README.md": "# r\n"})
    await make_project("Beta", {"b.md": "# b\n"}, type_="tool")
    r = await client.get(f"{BASE}/projects", headers=auth)
    assert r.status_code == 200
    names = [p["name"] for p in r.json()["projects"]]
    assert names == ["Alpha"]  # type=project 만
    r = await client.get(f"{BASE}/projects/{project['entity_id']}/sources", headers=auth)
    body = r.json()
    docs = {d["logical_path"]: d for s in body["sources"] for d in s["documents"]}
    assert set(docs) == {"docs/arch.md", "README.md"}
    assert "/synthetic-root" not in r.text


async def test_draft_reads_only_selected_documents_of_that_project(client, auth, gateway):
    alpha = await make_project("Alpha", {"docs/arch.md": DOC, "docs/other.md": "# 선택 안 함\n선택안함-본문\n"})
    beta = await make_project("Beta", {"secret.md": "# 다른 프로젝트\n다른프로젝트-본문\n"})
    draft = await new_draft(client, auth, alpha, ["docs/arch.md"])
    assert [d["logical_path"] for d in draft["documents"]] == ["docs/arch.md"]
    assert "선택안함-본문" not in json.dumps(draft, ensure_ascii=False)

    r = await client.post(f"{BASE}/drafts", headers=auth, json={
        "entity_id": alpha["entity_id"], "document_ids": [alpha["docs"]["docs/arch.md"], beta["docs"]["secret.md"]]})
    assert r.status_code == 422
    assert r.json()["code"] == "document_not_in_project"
    assert "다른프로젝트-본문" not in r.text
    async with sessionmaker()() as db:
        assert (await db.execute(text("SELECT count(*) FROM ctx_share_drafts"))).scalar() == 1


@pytest.mark.parametrize("payload", [
    {"document_ids": []},
    {"document_ids": ["not-a-uuid"]},
    {"document_ids": ["00000000-0000-0000-0000-000000000001"]},
])
async def test_draft_rejects_empty_malformed_or_unknown_selection(client, auth, gateway, payload):
    project = await make_project("Alpha", {"a.md": DOC})
    r = await client.post(f"{BASE}/drafts", headers=auth, json={"entity_id": project["entity_id"], **payload})
    assert r.status_code == 422
    assert r.json()["code"] in {"validation", "document_not_in_project"}


async def test_disabled_source_or_non_project_entity_is_not_selectable(client, auth, gateway):
    off = await make_project("Off", {"a.md": DOC}, enabled=False)
    tool = await make_project("Tool", {"a.md": DOC}, type_="tool")
    for p in (off, tool):
        r = await client.post(f"{BASE}/drafts", headers=auth,
                              json={"entity_id": p["entity_id"], "document_ids": [p["docs"]["a.md"]]})
        assert r.status_code in (404, 422)
        assert r.json()["code"] in {"project_not_found", "document_not_in_project"}


async def test_memories_and_conversations_are_not_part_of_the_draft(client, auth, gateway):
    from ttakkari.context import store
    project = await make_project("Alpha", {"a.md": DOC})
    async with sessionmaker()() as db:
        await store.remember(db, "대화에서 나온 개인 기억-비공개", entities=["Alpha"], source="chat")
        await db.commit()
    draft = await new_draft(client, auth, project, ["a.md"])
    assert "개인 기억-비공개" not in json.dumps(draft, ensure_ascii=False)


# ---- 영속·출처

async def test_draft_is_persisted_with_provenance_and_logical_refs(client, auth, gateway):
    project = await make_project("Alpha", {"docs/arch.md": DOC})
    draft = await new_draft(client, auth, project, ["docs/arch.md"], title="Alpha 맥락", verified=False)
    doc = draft["documents"][0]
    assert doc["sha256"] == sha(doc["content"])
    assert doc["source_revision"] == sha(DOC)  # 색인 시점 원본 sha256
    assert doc["source_refs"] == ["library:projects/docs/arch.md"]
    assert doc["generated_at"] and doc["verified_at"] is None and doc["verified_by"] is None
    assert doc["content_origin"] == "indexed_chunks"
    assert "/synthetic-root" not in json.dumps(draft)
    again = await client.get(f"{BASE}/drafts/{draft['id']}", headers=auth)
    assert again.json()["documents"][0]["sha256"] == doc["sha256"]
    assert again.json()["preview"]["digest"] == draft["preview"]["digest"]
    listing = await client.get(f"{BASE}/drafts", headers=auth, params={"entity_id": project["entity_id"]})
    assert [d["id"] for d in listing.json()["drafts"]] == [draft["id"]]
    assert "documents" not in listing.json()["drafts"][0]


async def test_verified_at_is_never_filled_without_an_owner_attestation(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"], verified=False)
    assert draft["documents"][0]["verified_at"] is None
    doc_id = draft["documents"][0]["id"]
    r = await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth,
                           json={"documents": [{"id": doc_id, "owner_verified": True}]})
    assert r.status_code == 200
    doc = r.json()["documents"][0]
    assert doc["verified_at"] and doc["verified_by"] == "owner"
    # 내용을 고치면 검증은 풀린다.
    r = await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth,
                           json={"documents": [{"id": doc_id, "content": "# 바뀐 본문\n"}]})
    doc = r.json()["documents"][0]
    assert doc["verified_at"] is None and doc["verified_by"] is None


# ---- 편집·digest·게시

async def test_edit_recomputes_hashes_and_digest(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"])
    r = await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth, json={
        "title": "새 제목", "documents": [{"id": draft["documents"][0]["id"], "content": "# 고침\n본문\n"}]})
    assert r.status_code == 200
    edited = r.json()
    assert edited["documents"][0]["sha256"] == sha("# 고침\n본문\n")
    assert edited["preview"]["digest"] != draft["preview"]["digest"]
    assert edited["revision"] == draft["revision"] + 1
    assert edited["documents"][0]["edited"] is True


async def test_publish_requires_the_current_approved_digest(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"])
    stale = draft["preview"]["digest"]
    await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth,
                       json={"documents": [{"id": draft["documents"][0]["id"], "content": "# 수정 후\n",
                                                  "owner_verified": True}]})
    r = await publish(client, auth, draft, stale)
    assert r.status_code == 409 and r.json()["code"] == "digest_mismatch"
    assert gateway.calls == []
    missing = await client.post(f"{BASE}/drafts/{draft['id']}/publish", headers=auth, json={})
    assert missing.status_code == 422
    current = (await client.get(f"{BASE}/drafts/{draft['id']}", headers=auth)).json()["preview"]["digest"]
    ok = await publish(client, auth, draft, current)
    assert ok.status_code == 200


async def test_publish_pushes_the_approved_snapshot_to_gateway_control_only(client, auth, gateway):
    project, draft, result = await published_project(client, auth, gateway)
    (method, path, body), = gateway.calls
    assert (method, path) == ("POST", "/control/v1/snapshots")
    assert body["project_slug"] == draft["slug"] and body["project_name"] == "Alpha"
    assert [d["id"] for d in body["documents"]] == ["docs-arch"]  # 코어 문서 id 규칙에 맞춘 논리 경로
    assert body["documents"][0]["sha256"] == sha(draft["documents"][0]["content"])
    assert body["documents"][0]["source_refs"] == [f"library-document:{project['docs']['docs/arch.md']}"]
    assert body["documents"][0]["source_revision"] == draft["documents"][0]["source_revision"]
    assert body["documents"][0]["generated_at"] == draft["documents"][0]["generated_at"]
    assert body["documents"][0]["verified_at"] == draft["documents"][0]["verified_at"]
    assert body["verified_at"] and body["source_refs"] == [f"library-document:{project['docs']['docs/arch.md']}"]
    assert "docs/arch.md" not in json.dumps(body)
    assert "/synthetic-root" not in json.dumps(body)
    assert result["draft"]["status"] == "published"
    assert result["publication"]["status"] == "active"
    assert result["publication"]["digest"] == result["draft"]["digest"]
    # 같은 초안의 재게시·게시 후 편집은 막힌다.
    again = await publish(client, auth, result["draft"])
    assert again.status_code == 409 and again.json()["code"] == "draft_not_editable"
    edit = await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth, json={"title": "x"})
    assert edit.status_code == 409 and edit.json()["code"] == "draft_not_editable"


async def test_publish_preserves_null_per_document_verification(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC, "b.md": "# b\n"})
    draft = await new_draft(client, auth, project, ["a.md", "b.md"], verified=False)
    r = await publish(client, auth, draft)
    assert r.status_code == 200, r.text
    body = gateway.calls[0][2]
    assert body["verified_at"] is None
    assert [d["verified_at"] for d in body["documents"]] == [None, None]
    assert all(d["source_revision"] and d["generated_at"] and d["source_refs"] for d in body["documents"])
    assert len({d["source_revision"] for d in body["documents"]}) == 2
    assert r.json()["draft"]["status"] == "published"


async def test_mixed_source_verification_does_not_block_approved_publish_or_infer_snapshot_verification(client, auth,
                                                                                                         gateway):
    project = await make_project("Alpha", {"a.md": DOC, "b.md": "# b\n"})
    draft = await new_draft(client, auth, project, ["a.md", "b.md"], verified=False)
    first = draft["documents"][0]
    edited = await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth,
                                json={"documents": [{"id": first["id"], "owner_verified": True}]})
    assert edited.status_code == 200
    draft = edited.json()

    result = await publish(client, auth, draft)

    assert result.status_code == 200, result.text
    assert result.json()["draft"]["status"] == "published"
    body = gateway.calls[-1][2]
    assert body["verified_at"] is None
    assert [d["verified_at"] for d in body["documents"]] == [draft["documents"][0]["verified_at"], None]
    assert body["documents"][0]["verified_at"] is not None
    for sent, source in zip(body["documents"], draft["documents"], strict=True):
        expected_ref = f"library-document:{project['docs'][source['logical_path']]}"
        assert sent["source_refs"] == [expected_ref]
        assert sent["source_revision"] == source["source_revision"]
        assert sent["generated_at"] == source["generated_at"]
        assert sent["verified_at"] == source["verified_at"]


async def test_risky_body_is_previewed_blocks_publish_and_can_be_edited_away(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": RISKY})
    draft = await new_draft(client, auth, project, ["a.md"])
    preview = draft["preview"]
    assert preview["blocked"] is True
    assert {f["rule"] for f in preview["findings"]} >= {"internal_url", "local_path"}
    assert all({"rule", "severity", "line", "excerpt", "document_id", "field"} <= set(f) for f in preview["findings"])
    assert "회사 기밀" in " ".join(preview["limitations"])
    r = await publish(client, auth, draft)
    assert r.status_code == 422 and r.json()["code"] == "sensitive_content_blocked"
    assert r.json()["findings"], "차단 응답이 위험 항목을 알려야 한다"
    assert gateway.calls == []
    fixed = await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth, json={
        "documents": [{"id": draft["documents"][0]["id"], "content": "# 설정\n\n서버 위치는 비공개다.\n",
                       "owner_verified": True}]})
    assert fixed.json()["preview"]["blocked"] is False
    ok = await publish(client, auth, fixed.json())
    assert ok.status_code == 200


async def test_credential_in_title_or_summary_also_blocks(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"], summary="password = hunter2-synthetic")
    assert draft["preview"]["blocked"] is True
    finding = draft["preview"]["findings"][0]
    assert finding["field"] == "summary" and "hunter2-synthetic" not in json.dumps(draft["preview"])


async def test_medium_findings_warn_but_do_not_block(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": "문의: owner@example.com\n"})
    draft = await new_draft(client, auth, project, ["a.md"])
    assert draft["preview"]["blocked"] is False
    assert draft["preview"]["counts"] == {"high": 0, "medium": 1}
    assert (await publish(client, auth, draft)).status_code == 200


# ---- Gateway 상태와 오류

async def test_unconfigured_gateway_returns_actionable_503_and_records_nothing(client, auth):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"])
    status = await client.get(f"{BASE}/status", headers=auth)
    assert status.json()["gateway"]["configured"] is False
    assert set(status.json()["gateway"]["missing"]) == {"context_gateway_control_url", "context_gateway_control_token"}
    r = await publish(client, auth, draft)
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "gateway_not_configured"
    assert body["gateway"]["configured"] is False and "message" in body
    detail = (await client.get(f"{BASE}/drafts/{draft['id']}", headers=auth)).json()
    assert detail["status"] == "draft" and detail["publication"] is None


@pytest.mark.parametrize("failure,reply,status,code", [
    (GatewayUnavailable("connect"), None, 503, "gateway_unavailable"),
    (None, 500, 503, "gateway_unavailable"),
    (None, 422, 502, "gateway_rejected"),
    (None, 401, 502, "gateway_rejected"),
])
async def test_gateway_failure_does_not_mark_the_draft_published(client, auth, gateway, failure, reply, status, code):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"])
    gateway.fail_with, gateway.reply_status = failure, reply
    r = await publish(client, auth, draft)
    assert r.status_code == status and r.json()["code"] == code
    detail = (await client.get(f"{BASE}/drafts/{draft['id']}", headers=auth)).json()
    assert detail["status"] == "draft" and detail["publication"] is None
    async with sessionmaker()() as db:
        assert (await db.execute(text("SELECT count(*) FROM ctx_share_publications"))).scalar() == 0
        outcome = (await db.execute(text(
            "SELECT outcome FROM audit_logs WHERE action = 'context_share.publish' ORDER BY id DESC LIMIT 1"))).scalar()
        assert outcome == "error"
    gateway.fail_with, gateway.reply_status = None, None
    assert (await publish(client, auth, draft)).status_code == 200  # 재시도 가능


async def test_gateway_receipt_with_different_document_hashes_is_a_failure(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"])
    gateway.echo_wrong_hash = True
    r = await publish(client, auth, draft)
    assert r.status_code == 502 and r.json()["code"] == "gateway_bad_response"
    assert (await client.get(f"{BASE}/drafts/{draft['id']}", headers=auth)).json()["status"] == "draft"


# ---- 키

async def test_key_is_returned_once_and_never_stored(client, auth, gateway):
    _, draft, _ = await published_project(client, auth, gateway)
    r = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"], "label": "외부 AI"})
    assert r.status_code == 201, r.text
    assert "no-store" in r.headers["cache-control"]
    issued = r.json()
    raw = issued["key"]
    assert raw.startswith(RAW_KEY_PREFIX)
    assert issued["ttl_seconds"] == 3600 and issued["single_use"] is True
    assert issued["connect"]["url"] == "https://ctx.example.test"
    sent = gateway.calls[-1][2]
    assert "ttl_seconds" not in sent and sent["label"] == "외부 AI"
    (pid, grant), = sent["projects"].items()
    assert grant["scopes"] == ["read", "search"] and pid == gateway.projects[draft["slug"]]["id"]
    listing = await client.get(f"{BASE}/keys", headers=auth)
    assert raw not in listing.text
    entry = listing.json()["keys"][0]
    assert entry["id"] == issued["id"] and entry["status"] == "active" and "key" not in entry
    assert raw not in await table_dump()
    async with sessionmaker()() as db:
        assert (await db.execute(text("SELECT count(*) FROM ctx_share_keys"))).scalar() == 1


async def test_key_requires_an_active_publication_and_accepts_only_fixed_one_hour_ttl(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC})
    draft = await new_draft(client, auth, project, ["a.md"])
    r = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    assert r.status_code == 409 and r.json()["code"] == "project_not_published"
    _, published, _ = await published_project(client, auth, gateway, name="Beta")
    for ttl in (0, 59, 600, 3601):
        bad = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": published["slug"], "ttl_seconds": ttl})
        assert bad.status_code == 422
    assert not any(c[1] == "/control/v1/keys" for c in gateway.calls)
    fixed = await client.post(f"{BASE}/keys", headers=auth,
                              json={"project_slug": published["slug"], "ttl_seconds": 3600})
    assert fixed.status_code == 201 and fixed.json()["ttl_seconds"] == 3600


async def test_key_issue_failure_leaves_no_local_key(client, auth, gateway):
    _, draft, _ = await published_project(client, auth, gateway)
    gateway.fail_with = GatewayUnavailable("down")
    r = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    assert r.status_code == 503 and r.json()["code"] == "gateway_unavailable"
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"] == []


async def test_key_revoke_is_recorded_only_after_gateway_confirms(client, auth, gateway):
    _, draft, _ = await published_project(client, auth, gateway)
    issued = (await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})).json()
    gateway.fail_with = GatewayUnavailable("down")
    r = await client.post(f"{BASE}/keys/{issued['id']}/revoke", headers=auth)
    assert r.status_code == 503
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"][0]["status"] == "active"
    gateway.fail_with = None
    ok = await client.post(f"{BASE}/keys/{issued['id']}/revoke", headers=auth)
    assert ok.status_code == 200 and ok.json()["status"] == "revoked" and ok.json()["revoked_at"]
    assert any(k["revoked"] for k in gateway.keys.values())
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"][0]["status"] == "revoked"
    missing = await client.post(f"{BASE}/keys/00000000-0000-0000-0000-000000000000/revoke", headers=auth)
    assert missing.status_code == 404


async def test_project_revoke_requires_gateway_confirmation(client, auth, gateway):
    _, draft, _ = await published_project(client, auth, gateway)
    await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    gateway.reply_status = 409
    r = await client.post(f"{BASE}/projects/{draft['slug']}/revoke", headers=auth)
    assert r.status_code == 502
    detail = (await client.get(f"{BASE}/drafts/{draft['id']}", headers=auth)).json()
    assert detail["publication"]["status"] == "active"
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"][0]["status"] == "active"
    gateway.reply_status = None
    ok = await client.post(f"{BASE}/projects/{draft['slug']}/revoke", headers=auth)
    assert ok.status_code == 200 and ok.json()["publication"]["status"] == "revoked"
    assert ok.json()["revoked_snapshots"] == 1 and ok.json()["revoked_keys"] == 1
    assert gateway.latest(draft["slug"]) is None
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"][0]["status"] == "revoked"
    again = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    assert again.status_code == 409 and again.json()["code"] == "project_not_published"
    repeat = await client.post(f"{BASE}/projects/{draft['slug']}/revoke", headers=auth)
    assert repeat.status_code == 200 and repeat.json()["already_revoked"] is True
    unknown = await client.post(f"{BASE}/projects/nope/revoke", headers=auth)
    assert unknown.status_code == 404


async def test_project_revoke_uses_gateway_project_endpoint_for_all_snapshots(client, auth, gateway):
    project, first, _ = await published_project(client, auth, gateway)
    second = await new_draft(client, auth, project, ["README.md"])
    assert (await publish(client, auth, second)).status_code == 200
    gateway.calls.clear()
    ok = await client.post(f"{BASE}/projects/{first['slug']}/revoke", headers=auth)
    assert ok.status_code == 200 and ok.json()["revoked_snapshots"] == 2
    order = [c[1] for c in gateway.calls]
    project_id = gateway.projects[first["slug"]]["id"]
    assert order == [f"/control/v1/projects/{project_id}/revoke"]
    assert gateway.latest(first["slug"]) is None


async def test_project_revoke_partial_failure_records_only_what_gateway_confirmed(client, auth, gateway):
    project, first, _ = await published_project(client, auth, gateway)
    second = await new_draft(client, auth, project, ["README.md"])
    assert (await publish(client, auth, second)).status_code == 200
    project_id = gateway.projects[first["slug"]]["id"]
    gateway.fail_paths[f"/projects/{project_id}/revoke"] = 500
    r = await client.post(f"{BASE}/projects/{first['slug']}/revoke", headers=auth)
    assert r.status_code == 503
    assert r.json()["partial"] == {"revoked_snapshots": 0, "remaining_snapshots": 2, "revoked_keys": 0, "remaining_keys": 0}
    state = (await client.get(f"{BASE}/projects", headers=auth)).json()["projects"][0]["publication"]
    assert state["status"] == "active" and state["draft_id"] == second["id"]
    gateway.fail_paths.clear()
    ok = await client.post(f"{BASE}/projects/{first['slug']}/revoke", headers=auth)
    assert ok.status_code == 200 and ok.json()["revoked_snapshots"] == 2


async def test_republish_supersedes_previous_publication(client, auth, gateway):
    project, first, _ = await published_project(client, auth, gateway)
    second = await new_draft(client, auth, project, ["README.md"])
    assert second["slug"] == first["slug"]
    assert (await publish(client, auth, second)).status_code == 200
    projects = (await client.get(f"{BASE}/projects", headers=auth)).json()["projects"]
    assert projects[0]["publication"]["draft_id"] == second["id"]
    assert projects[0]["publication"]["status"] == "active"
    async with sessionmaker()() as db:
        statuses = (await db.execute(text("SELECT status FROM ctx_share_publications ORDER BY published_at"))).scalars().all()
        assert statuses == ["superseded", "active"]


# ---- work 범위

async def test_work_scope_requires_owner_confirmation_and_agents_cannot_confirm(client, auth, gateway):
    project = await make_project("Corp", {"a.md": DOC}, scope="work")
    payload = {"entity_id": project["entity_id"], "document_ids": [project["docs"]["a.md"]]}
    r = await client.post(f"{BASE}/drafts", headers=auth, json=payload)
    assert r.status_code == 403 and r.json()["code"] == "work_scope_confirmation_required"
    ok = await client.post(f"{BASE}/drafts", headers=auth, json={**payload, "confirm_work_scope": True})
    assert ok.status_code == 201 and ok.json()["scope"] == "work"
    agent = await client.post("/api/agent/context-sharing/drafts", headers=await agent_headers(),
                              json={**payload, "confirm_work_scope": True})
    assert agent.status_code == 403 and agent.json()["code"] == "work_scope_owner_only"


# ---- agent 경로

async def test_agent_can_inspect_and_draft_but_drafts_are_marked_and_unpublished(client, auth, gateway):
    project = await make_project("Alpha", {"a.md": DOC})
    agent = await agent_headers()
    listed = await client.get("/api/agent/context-sharing/projects", headers=agent)
    assert [p["name"] for p in listed.json()["projects"]] == ["Alpha"]
    sources = await client.get(f"/api/agent/context-sharing/projects/{project['entity_id']}/sources", headers=agent)
    assert sources.status_code == 200 and "/synthetic-root" not in sources.text
    r = await client.post("/api/agent/context-sharing/drafts", headers=agent,
                          json={"entity_id": project["entity_id"], "document_ids": [project["docs"]["a.md"]]})
    assert r.status_code == 201 and r.json()["created_by"] == "agent" and r.json()["status"] == "draft"
    detail = await client.get(f"/api/agent/context-sharing/drafts/{r.json()['id']}", headers=agent)
    assert detail.status_code == 200 and detail.json()["documents"]
    assert gateway.calls == []
    owner_view = await client.get(f"{BASE}/drafts", headers=auth)
    assert owner_view.json()["drafts"][0]["created_by"] == "agent"


async def test_settings_expose_status_without_secrets(client, auth, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "context_gateway_control_url", "https://control.example.test", raising=False)
    monkeypatch.setattr(s, "context_gateway_control_token", "synthetic-control-token-value", raising=False)
    r = await client.get(f"{BASE}/status", headers=auth)
    assert r.json()["gateway"]["configured"] is True
    assert "synthetic-control-token-value" not in r.text
    assert r.json()["gateway"]["control_host"] == "control.example.test"


async def test_key_cleanup_revoke_failure_is_audited_without_key_material(client, auth, gateway):
    _, draft, _ = await published_project(client, auth, gateway)
    gateway.key_lifetime = 3665  # 코어가 고정 3600초보다 오래 사는 키를 반환했다고 가정한다.
    gateway.fail_paths["/control/v1/keys/"] = 503
    r = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    assert r.status_code == 502 and r.json()["code"] == "gateway_bad_response"
    assert RAW_KEY_PREFIX not in r.text
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"] == []
    async with sessionmaker()() as db:
        audit = (await db.execute(text("SELECT detail::text FROM audit_logs WHERE action = 'context_share.key_cleanup_failed'"))).scalar_one()
    assert "gateway_key_id" in audit and "gateway_unavailable" in audit
    assert RAW_KEY_PREFIX not in audit


async def test_key_that_outlives_the_fixed_ttl_is_revoked_and_not_returned(client, auth, gateway):
    _, draft, _ = await published_project(client, auth, gateway)
    gateway.key_lifetime = 3665  # 코어가 고정 3600초보다 오래 사는 키를 반환했다고 가정한다.
    r = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    assert r.status_code == 502 and r.json()["code"] == "gateway_bad_response"
    assert RAW_KEY_PREFIX not in r.text
    assert all(k["revoked"] for k in gateway.keys.values())
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"] == []
    gateway.key_lifetime = None
    ok = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"], "scopes": ["read"]})
    assert ok.status_code == 201 and ok.json()["scopes"] == ["read"]


async def test_key_that_expires_too_early_is_revoked_and_not_returned(client, auth, gateway):
    _, draft, _ = await published_project(client, auth, gateway)
    gateway.key_lifetime = 60
    r = await client.post(f"{BASE}/keys", headers=auth, json={"project_slug": draft["slug"]})
    assert r.status_code == 502 and r.json()["code"] == "gateway_bad_response"
    assert RAW_KEY_PREFIX not in r.text
    assert all(k["revoked"] for k in gateway.keys.values())
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"] == []


async def test_publish_is_refused_when_gateway_receipt_has_no_project_id(client, auth, gateway):
    gateway.omit_project_id = True
    project = await make_project("Alpha", {"docs/arch.md": DOC})
    draft = await new_draft(client, auth, project, ["docs/arch.md"])
    r = await publish(client, auth, draft)
    assert r.status_code == 502 and r.json()["code"] == "gateway_bad_response"
    detail = (await client.get(f"{BASE}/drafts/{draft['id']}", headers=auth)).json()
    assert detail["status"] == "draft" and detail["publication"] is None
    assert not any(c[1] == "/control/v1/keys" for c in gateway.calls)
    assert (await client.get(f"{BASE}/keys", headers=auth)).json()["keys"] == []
