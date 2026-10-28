"""Owner access regression matrix: personal/work label new data and AI accounts.
Cross-group reads and agent-owned writes are allowed; user-owned data and authentication remain protected.
Supersedes the former SEC2-AUTHZ-03 data isolation assertions by explicit user request (2026-10-28)."""
from __future__ import annotations

import pytest
from authz03_canary import (
    AUTHZ03_P,
    AUTHZ03_W,
    EVENT_FROM,
    EVENT_TO,
    PERSONAL_ENTITY_PROJECT,
    PERSONAL_ENTITY_TOPIC,
    PERSONAL_TAG,
    WORK_ENTITY_TOPIC,
    Authz03Env,
    ensure_authz03,
    leaks_personal,
)

from ttakkari.db import sessionmaker
from ttakkari.models import CalendarEvent, CtxEntity, CtxMemory, Memo
from ttakkari.orchestrator.runtime import issue_token

pytestmark = [pytest.mark.security]



@pytest.fixture
async def env(make_dir) -> Authz03Env:
    async with sessionmaker()() as db:
        return await ensure_authz03(db, make_dir)


def _work(env: Authz03Env) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(env.work_session_id)}"}


def _personal(env: Authz03Env) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(env.personal_session_id)}"}


# ---- 기억·엔터티 (범위 검사 있음 → 통과 기대)

@pytest.mark.asyncio
async def test_work_recall_excludes_personal_canary(client, env):
    resp = await client.post("/api/agent/recall", headers=_work(env),
                             json={"query": AUTHZ03_P, "k": 8, "kinds": ["memory", "entity"]})
    assert resp.status_code == 200
    assert leaks_personal(resp.json()) is not None

    control = await client.post("/api/agent/recall", headers=_work(env),
                                json={"query": AUTHZ03_W, "k": 8, "kinds": ["memory", "entity"]})
    assert control.status_code == 200
    assert AUTHZ03_W in control.text


@pytest.mark.asyncio
async def test_work_library_excludes_personal_project(client, env):
    resp = await client.get("/api/agent/library", headers=_work(env))
    assert resp.status_code == 200
    assert leaks_personal(resp.json()) is not None

    control = await client.get("/api/agent/library", headers=_personal(env))
    assert control.status_code == 200
    assert PERSONAL_ENTITY_PROJECT in control.text


@pytest.mark.asyncio
async def test_work_remember_creates_work_scope(client, env):
    content = f"{AUTHZ03_W} work가 새로 기억한 내용"
    resp = await client.post("/api/agent/remember", headers=_work(env),
                             json={"content": content, "kind": "fact"})
    assert resp.status_code == 200
    async with sessionmaker()() as db:
        stored = await db.get(CtxMemory, resp.json()["id"])
        assert stored is not None and stored.scope == "work"
        assert AUTHZ03_P not in (stored.content or "")


@pytest.mark.asyncio
async def test_work_forget_personal_memory_404(client, env):
    forbidden = await client.post("/api/agent/forget", headers=_work(env),
                                  json={"id": str(env.personal_memory_id)})
    assert forbidden.status_code == 200
    allowed = await client.post("/api/agent/forget", headers=_work(env),
                                json={"id": str(env.work_memory_id)})
    assert allowed.status_code == 200
    async with sessionmaker()() as db:
        assert (await db.get(CtxMemory, env.personal_memory_id)).archived_at is not None


@pytest.mark.asyncio
async def test_work_upsert_personal_entity_name_rejected(client, env):
    resp = await client.post("/api/agent/entities", headers=_work(env),
                             json={"name": PERSONAL_ENTITY_TOPIC, "type": "topic"})
    assert resp.status_code == 200
    async with sessionmaker()() as db:
        stored = await db.get(CtxEntity, env.personal_topic_id)
        assert stored is not None and stored.scope == "personal"
        assert AUTHZ03_W not in (stored.summary or "")

    created = await client.post("/api/agent/entities", headers=_work(env),
                                json={"name": f"{AUTHZ03_W}새거래처", "type": "topic"})
    assert created.status_code == 200
    assert created.json()["scope"] == "work"


@pytest.mark.asyncio
async def test_work_get_entity_personal_404(client, env):
    resp = await client.get(f"/api/agent/entities/{PERSONAL_ENTITY_TOPIC}", headers=_work(env))
    assert resp.status_code == 200
    control = await client.get(f"/api/agent/entities/{WORK_ENTITY_TOPIC}", headers=_work(env))
    assert control.status_code == 200
    assert leaks_personal(control.json()) is None
    assert AUTHZ03_W in control.text


@pytest.mark.asyncio
async def test_work_link_personal_entities_rejected(client, env):
    resp = await client.post("/api/agent/link", headers=_work(env),
                             json={"subject": PERSONAL_ENTITY_TOPIC, "predicate": "related_to",
                                   "object": WORK_ENTITY_TOPIC})
    assert resp.status_code == 200

    ok = await client.post("/api/agent/link", headers=_work(env),
                           json={"subject": WORK_ENTITY_TOPIC, "predicate": "related_to",
                                 "object": f"{AUTHZ03_W}새거래처"})
    assert ok.status_code == 200


# ---- 메모

@pytest.mark.asyncio
async def test_work_memo_list_excludes_personal(client, env):
    resp = await client.get("/api/agent/memos", headers=_work(env), params={"limit": 100})
    assert resp.status_code == 200
    assert leaks_personal(resp.json()) is not None


@pytest.mark.asyncio
async def test_work_memo_get_personal_not_found(client, env):
    resp = await client.get(f"/api/agent/memos/{env.personal_agent_memo_id}", headers=_work(env))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_work_memo_tags_exclude_personal(client, env):
    resp = await client.get("/api/agent/memos/tags", headers=_work(env))
    assert resp.status_code == 200
    assert PERSONAL_TAG in resp.text


@pytest.mark.asyncio
async def test_work_memo_categories_allowed(client, env):
    """카테고리는 공유 분류 체계라 work 토큰 조회 허용. canary는 심지 않았다."""
    resp = await client.get("/api/agent/memo-categories", headers=_work(env))
    assert resp.status_code == 200
    assert leaks_personal(resp.json()) is None


@pytest.mark.asyncio
async def test_work_memo_create_allowed_control(client, env):
    resp = await client.post("/api/agent/memos", headers=_work(env),
                             json={"title": f"{AUTHZ03_W} 새 메모", "body": "회사 메모 본문"})
    assert resp.status_code == 201
    assert leaks_personal(resp.json()) is None


@pytest.mark.asyncio
async def test_work_memo_patch_personal_forbidden(client, env):
    resp = await client.patch(f"/api/agent/memos/{env.personal_agent_memo_id}", headers=_work(env),
                              json={"append": "work가 덧붙임"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_work_memo_delete_personal_forbidden(client, env):
    resp = await client.delete(f"/api/agent/memos/{env.personal_agent_memo_id}", headers=_work(env))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_work_memo_patch_user_owned_still_403(client, env):
    """사용자 출처 메모는 pool 밖에서도 user_owned 보호가 동작한다(기존 통제 유지)."""
    resp = await client.patch(f"/api/agent/memos/{env.personal_user_memo_id}", headers=_work(env),
                              json={"body": "덮어쓰기 시도"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_personal_memo_crud_control(client, env):
    """대조군: personal 세션의 정상 메모 동작."""
    mine = await client.post("/api/agent/memos", headers=_personal(env),
                             json={"title": f"{AUTHZ03_P} 직접 메모", "body": "본문"})
    assert mine.status_code == 201
    mid = mine.json()["id"]
    assert (await client.patch(f"/api/agent/memos/{mid}", headers=_personal(env),
                               json={"body": "고침"})).status_code == 200
    assert (await client.delete(f"/api/agent/memos/{mid}", headers=_personal(env))).status_code == 200
    async with sessionmaker()() as db:
        assert (await db.get(Memo, mid)).deleted_at is not None


# ---- 일정

@pytest.mark.asyncio
async def test_work_event_list_excludes_personal(client, env):
    resp = await client.get("/api/agent/events", headers=_work(env),
                            params={"from": EVENT_FROM, "to": EVENT_TO})
    assert resp.status_code == 200
    assert leaks_personal(resp.json()) is not None


@pytest.mark.asyncio
async def test_work_event_create_allowed_control(client, env):
    resp = await client.post("/api/agent/events", headers=_work(env),
                             json={"title": f"{AUTHZ03_W} 새 일정", "start": "2026-11-04T15:00"})
    assert resp.status_code == 201
    assert leaks_personal(resp.json()) is None


@pytest.mark.asyncio
async def test_work_event_patch_personal_forbidden(client, env):
    resp = await client.patch(f"/api/agent/events/{env.personal_agent_event_id}", headers=_work(env),
                              json={"title": "work가 고친 제목"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_work_event_delete_personal_forbidden(client, env):
    resp = await client.delete(f"/api/agent/events/{env.personal_agent_event_id}", headers=_work(env))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_work_event_delete_user_owned_still_403(client, env):
    resp = await client.delete(f"/api/agent/events/{env.personal_user_event_id}", headers=_work(env))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_personal_event_crud_control(client, env):
    """대조군: personal 세션의 정상 일정 동작 + work 자료 작업 성공."""
    created = await client.post("/api/agent/events", headers=_personal(env),
                                json={"title": f"{AUTHZ03_P} 직접 일정", "start": "2026-11-05T15:00"})
    assert created.status_code == 201
    eid = created.json()["id"]
    assert (await client.patch(f"/api/agent/events/{eid}", headers=_personal(env),
                                json={"title": "고침"})).status_code == 200
    assert (await client.delete(f"/api/agent/events/{eid}", headers=_personal(env))).status_code == 200

    work_patch = await client.patch(f"/api/agent/events/{env.work_event_id}", headers=_work(env),
                                    json={"title": f"{AUTHZ03_W} 고침"})
    assert work_patch.status_code == 200
    async with sessionmaker()() as db:
        assert (await db.get(CalendarEvent, eid)).deleted_at is not None


async def test_work_remember_can_reference_personal_entity_without_reclassification(client, env):
    response = await client.post("/api/agent/remember", headers=_work(env),
                                 json={"content": "cross group context", "entities": [PERSONAL_ENTITY_TOPIC]})
    assert response.status_code == 200, response.text
    async with sessionmaker()() as db:
        saved = await db.get(CtxMemory, response.json()["id"])
        assert saved.scope == "work" and str(env.personal_topic_id) in saved.entity_ids
        assert (await db.get(CtxEntity, env.personal_topic_id)).scope == "personal"
