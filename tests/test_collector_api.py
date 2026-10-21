"""CP-S2-02: collector intake HTTP API. 인증 주체에서 scope 를 산출하고 403/409/422/404 가 계약대로 나가는지 확인한다.

사용자 API(/api/assistant/collect, owner JWT)와 에이전트 API(/api/agent/collect, 세션 토큰)는 같은 핸들러를 쓴다.
DB 는 tests/conftest.py 가 쓰는 테스트 DB(운영 DB 거부)다. 설계 기대값은 contracts/fixtures/intake/ 10건을 HTTP 로 다시 돌린다.
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from ttakkari.collector import jobs
from ttakkari.collector_models import CollectorJob, CollectorOutbox
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Workspace
from ttakkari.orchestrator.runtime import issue_token

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "docs/design/collector-platform/contracts/fixtures"
INTAKE_EXPECTATIONS = json.loads((FIXTURES_DIR / "intake" / "expectations.json").read_text(encoding="utf-8"))
OWNER = "/api/assistant/collect"
AGENT = "/api/agent/collect"


@pytest.fixture
def envelope() -> dict:
    return bound(json.loads((FIXTURES_DIR / "valid-accept.json").read_text(encoding="utf-8")), "personal")


def bound(envelope: dict, scope: str) -> dict:
    """API 가 인증 주체에서 산출하는 tenant/principal 에 맞춘 envelope. tenant 는 owner 다."""
    env = copy.deepcopy(envelope)
    env["access"].update({"tenant_id": "owner", "scope": scope, "principal_ids": ["owner"]})
    return env


def with_id(envelope: dict) -> dict:
    env = copy.deepcopy(envelope)
    env["envelope_id"] = str(uuid.uuid4())
    return env


@pytest.fixture
async def agent_headers(make_dir):
    root = make_dir()
    async with sessionmaker()() as db:
        ws = Workspace(id=uuid.uuid4(), name=root.name, root_path=str(root))
        db.add(ws)
        await db.flush()
        sessions = {}
        for pool in ("personal", "work"):
            row = ChatSession(id=uuid.uuid4(), workspace_id=ws.id, title=pool, engine="codex", pool=pool,
                              created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            db.add(row)
            sessions[pool] = row
        await db.commit()
    return {pool: {"Authorization": f"Bearer {issue_token(row.id)}"} for pool, row in sessions.items()}


async def post(client, headers, env, *, key="req-1", base=OWNER, scope=None, content=None):
    params = {"scope": scope} if scope else None
    all_headers = {**headers, "Content-Type": "application/json"}
    if key is not None:
        all_headers["Idempotency-Key"] = key
    body = content if content is not None else json.dumps(env, allow_nan=True)
    return await client.post(f"{base}/jobs", content=body.encode("utf-8", "surrogatepass"), headers=all_headers,
                             params=params)


async def counts() -> tuple[int, int]:
    async with sessionmaker()() as db:
        return (await db.scalar(select(func.count(CollectorJob.id))),
                await db.scalar(select(func.count(CollectorOutbox.id))))


def err(response) -> tuple[int, str]:
    body = response.json()
    assert set(body) == {"code", "message"}, body
    return response.status_code, body["code"]


async def test_create_replay_and_conflict(client, auth, envelope):
    created = await post(client, auth, envelope)
    assert created.status_code == 202
    assert created.json()["result"] == "created"

    replay = await post(client, auth, envelope)
    assert replay.status_code == 202
    assert replay.json()["result"] == "replay"
    assert replay.json()["job_id"] == created.json()["job_id"]

    altered = copy.deepcopy(envelope)
    altered["decision"]["state"] = "hold"
    altered["decision"]["reason_codes"] = ["unmeasured_quality"]
    conflict = await post(client, auth, altered)
    assert err(conflict) == (409, "idempotency_conflict")
    assert created.json()["payload_hash"] not in conflict.json()["message"]
    assert await counts() == (1, 1)


async def test_same_envelope_id_with_other_key_is_409(client, auth, envelope):
    await post(client, auth, envelope, key="a")
    response = await post(client, auth, envelope, key="b")
    assert err(response) == (409, "envelope_id_conflict")
    assert await counts() == (1, 1)


@pytest.mark.parametrize("fixture_name", sorted(INTAKE_EXPECTATIONS))
async def test_intake_fixtures_give_same_result_over_http(client, auth, fixture_name):
    """계약 fixture 10건이 HTTP 경로에서도 같은 결과다. 변조 fixture 는 tenant/principal 값만 owner 로 치환해 변조를 유지한다."""
    case = json.loads((FIXTURES_DIR / "intake" / fixture_name).read_text(encoding="utf-8"))
    scope = case["auth"]["session_pool"]
    want_sequence = INTAKE_EXPECTATIONS[fixture_name]

    def rebind(sub: dict) -> dict:
        sub = copy.deepcopy(sub)
        if sub["access"]["tenant_id"] == "fixture-tenant":
            sub["access"]["tenant_id"] = "owner"
        sub["access"]["principal_ids"] = ["owner" if p == "fixture-owner" else p for p in sub["access"]["principal_ids"]]
        return sub

    for index, (want, submission) in enumerate(zip(want_sequence, case["submissions"], strict=True)):
        response = await post(client, auth, rebind(submission), key=case["request_key"], scope=scope)
        label = f"{fixture_name}[{index}] -> {want}"
        if want == "reject:scope_binding_mismatch":
            assert err(response) == (403, "scope_binding_mismatch"), label
        elif want == "conflict:409":
            assert err(response)[0] == 409, label
        else:
            assert response.status_code == 202 and response.json()["result"] == want, label
    created = sum(1 for want in want_sequence if want == "created")
    assert await counts() == (created, created)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e["access"].update(tenant_id="someone-else"), id="tenant"),
        pytest.param(lambda e: e["access"].update(principal_ids=["other"]), id="principal"),
        pytest.param(lambda e: e["access"].update(principal_ids=["owner", "admin"]), id="principal-escalation"),
        pytest.param(lambda e: e["access"].update(scope="work"), id="scope"),
    ],
)
async def test_tampered_access_is_403_without_side_effects(client, auth, envelope, mutate):
    tampered = copy.deepcopy(envelope)
    mutate(tampered)
    response = await post(client, auth, tampered)
    assert err(response) == (403, "scope_binding_mismatch")
    assert response.json()["message"] == "envelope.access does not match the authenticated principal"
    assert await counts() == (0, 0)


async def test_cross_scope_replay_never_reveals_the_existing_job(client, auth, envelope):
    first = await post(client, auth, envelope)
    assert first.status_code == 202
    # 같은 키·같은 payload 를 work scope 요청으로 보내면 기존 job 을 돌려주지 않고 403 이다.
    response = await post(client, auth, envelope, scope="work")
    assert err(response) == (403, "scope_binding_mismatch")
    assert first.json()["job_id"] not in response.text


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not json", id="invalid-json"),
        pytest.param("[1, 2]", id="not-object"),
        pytest.param("{}", id="empty-object"),
        pytest.param('{"a": 1, "a": 2}', id="duplicate-key"),
        pytest.param("NaN", id="nan-literal"),
    ],
)
async def test_malformed_body_is_422(client, auth, content):
    response = await post(client, auth, None, content=content)
    assert err(response) == (422, "invalid_envelope")
    assert await counts() == (0, 0)


async def test_missing_idempotency_key_is_422(client, auth, envelope):
    assert err(await post(client, auth, envelope, key=None)) == (422, "invalid_envelope")
    assert err(await post(client, auth, envelope, key="")) == (422, "invalid_envelope")
    assert err(await post(client, auth, envelope, key="k" * 257)) == (422, "invalid_envelope")
    assert await counts() == (0, 0)


@pytest.mark.parametrize(
    "value_json",
    [
        pytest.param('"bad \\ud800 text"', id="lone-surrogate"),
        pytest.param("9007199254740993", id="int-above-2**53"),
        pytest.param("Infinity", id="infinity"),
        pytest.param('"nul\\u0000byte"', id="nul"),
    ],
)
async def test_unencodable_values_are_422_not_500(client, auth, envelope, value_json):
    env = copy.deepcopy(envelope)
    env["extensions"]["x-test:value"] = "__PLACEHOLDER__"
    raw = json.dumps(env).replace('"__PLACEHOLDER__"', value_json)
    response = await post(client, auth, None, content=raw)
    assert err(response) == (422, "invalid_envelope")
    assert await counts() == (0, 0)


async def test_oversized_body_is_422(client, auth, envelope):
    env = copy.deepcopy(envelope)
    env["extensions"]["x-test:value"] = "x" * (8 * 1024 * 1024 + 1)
    assert err(await post(client, auth, env)) == (422, "invalid_envelope")
    assert await counts() == (0, 0)


async def test_authentication_required(client, envelope):
    assert (await client.post(f"{OWNER}/jobs", json=envelope, headers={"Idempotency-Key": "k"})).status_code == 401
    assert (await client.post(f"{AGENT}/jobs", json=envelope, headers={"Idempotency-Key": "k"})).status_code == 401
    assert (await client.get(f"{OWNER}/jobs")).status_code == 401
    assert await counts() == (0, 0)


async def test_agent_scope_comes_from_session_pool_not_request(client, envelope, agent_headers):
    ok = await post(client, agent_headers["personal"], envelope, base=AGENT)
    assert ok.status_code == 202 and ok.json()["result"] == "created"

    work_env = bound(envelope, "work")
    work_env["envelope_id"] = str(uuid.uuid4())
    assert (await post(client, agent_headers["work"], work_env, base=AGENT)).status_code == 202

    # work 세션이 personal envelope 를 보내면 403. 쿼리로 scope 를 바꿔도 소용없다.
    response = await post(client, agent_headers["work"], with_id(envelope), base=AGENT, scope="personal", key="x")
    assert err(response) == (403, "scope_binding_mismatch")
    # personal 세션이 work envelope 를 보내도 403
    assert err(await post(client, agent_headers["personal"], work_env, base=AGENT, key="y"))[0] == 403
    assert await counts() == (2, 2)


async def test_other_scope_jobs_are_invisible_and_uncancellable(client, auth, envelope, agent_headers):
    job_id = (await post(client, auth, envelope)).json()["job_id"]

    for headers, base, params in (
        (auth, OWNER, {"scope": "work"}),
        (agent_headers["work"], AGENT, None),
    ):
        got = await client.get(f"{base}/jobs/{job_id}", headers=headers, params=params)
        assert err(got) == (404, "job_not_found")
        cancel = await client.post(f"{base}/jobs/{job_id}/cancel", headers=headers, params=params)
        assert err(cancel) == (404, "job_not_found")
        listed = await client.get(f"{base}/jobs", headers=headers, params=params)
        assert listed.json()["jobs"] == []
    # 같은 scope 의 agent 는 owner 가 만든 personal job 을 본다
    assert (await client.get(f"{AGENT}/jobs/{job_id}", headers=agent_headers["personal"])).status_code == 200
    async with sessionmaker()() as db:
        assert (await db.get(CollectorJob, uuid.UUID(job_id))).status == "queued"


async def test_unknown_job_id_is_404(client, auth):
    assert err(await client.get(f"{OWNER}/jobs/{uuid.uuid4()}", headers=auth)) == (404, "job_not_found")


async def test_job_view_hides_envelope_and_attempt_token(client, auth, envelope):
    job_id = (await post(client, auth, envelope)).json()["job_id"]
    body = (await client.get(f"{OWNER}/jobs/{job_id}", headers=auth)).json()
    assert body["status"] == "queued" and body["stage"] == "intake" and body["attempts"] == 0
    assert body["request_key"] == "req-1" and body["cursor"] is None
    assert not {"envelope", "attempt_token"} & set(body)


async def test_list_paginates_newest_first_without_duplicates(client, auth, envelope):
    ids = []
    for i in range(5):
        ids.append((await post(client, auth, with_id(envelope), key=f"k-{i}")).json()["job_id"])
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (await client.get(f"{OWNER}/jobs", headers=auth, params=params)).json()
        seen += [j["job_id"] for j in body["jobs"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == list(reversed(ids))

    assert err(await client.get(f"{OWNER}/jobs", headers=auth, params={"cursor": "garbage"})) == (422, "invalid_envelope")
    assert err(await client.get(f"{OWNER}/jobs", headers=auth, params={"limit": 0}))[0] == 422


async def test_cancel_queued_running_and_finished(client, auth, envelope):
    queued_id = (await post(client, auth, with_id(envelope), key="q")).json()["job_id"]
    running_id = (await post(client, auth, with_id(envelope), key="r")).json()["job_id"]

    cancelled = await client.post(f"{OWNER}/jobs/{queued_id}/cancel", headers=auth)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (await client.post(f"{OWNER}/jobs/{queued_id}/cancel", headers=auth)).json()["status"] == "cancelled"
    assert (await client.get(f"{OWNER}/jobs/{queued_id}", headers=auth)).json()["status"] == "cancelled"

    async with sessionmaker()() as db:
        claimed = await jobs.claim_batch(db, owner_generation="test-owner", limit=5)
        await db.commit()
    assert [c.id for c in claimed] == [uuid.UUID(running_id)]
    flagged = await client.post(f"{OWNER}/jobs/{running_id}/cancel", headers=auth)
    assert flagged.json()["status"] == "cancelling"
    view = (await client.get(f"{OWNER}/jobs/{running_id}", headers=auth)).json()
    assert view["status"] == "running" and view["cancel_requested"] is True and view["attempts"] == 1

    async with sessionmaker()() as db:
        await jobs.finish(db, claimed[0].id, claimed[0].attempt_token, status="succeeded")
        await db.commit()
    assert err(await client.post(f"{OWNER}/jobs/{running_id}/cancel", headers=auth)) == (409, "invalid_job_state")
