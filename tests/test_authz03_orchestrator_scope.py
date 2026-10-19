"""SEC2-AUTHZ-03 3/3: orchestrator.py 15개 라우트 범위 매트릭스.

1/3의 공용 fixture(authz03_canary.ensure_authz03)를 재사용하고, 여기에
orchestrator 종류 세션·작업 세션·완료된 Run·PMS 프로젝트·엔진 계정 시드를
덧댄다. 2/3 범위(host_env·deploy·notebooklm·stocks·reverse·oplogs·design)는
다루지 않는다. 운영 코드는 고치지 않는다.

회사 오케스트레이터 토큰(work pool)으로 개인 자산에 접근 시도한다.
오케스트레이터 토큰은 세션 id 자체이므로, 남의 오케스트레이터가 아닌
자기 트리 밖 개인 자산(PMS·luna 잡·타인 작업 세션)이 보였다면 누출이다.

라우트별 기대 결과(work 오케스트레이터 토큰 기준):
| 라우트 | 기대 | 근거 |
| POST /api/orchestrator/workers | 201, pool=work 상속 (대조군) | pool=orch.pool로 생성 |
| POST /workers/{id}/messages | 타인 404 / 자기 202 | _worker parent 검사 |
| POST /workers/{id}/verification | 타인 404 / 자기 200 | _worker parent 검사 |
| GET /workers/metrics | pool=work 집계만 | pool 필터 |
| GET /context | 200, 자기 것만 | sid 바인딩 |
| POST /context/reset | 200 (대조군) | sid 바인딩 |
| GET /workers | 자기 트리만 | parent_id==sid |
| GET /workers/{id} | 타인 404 / 자기 200 | _worker parent 검사 |
| POST /workers/{id}/stop | 타인 404 / 자기 200 | _worker parent 검사 |
| GET /accounts/usage | 회사 계정만 | effective_order pool=work |
| POST /luna-common30-v2 | work 403 | pool!=personal 거부 |
| GET /luna-common30-v2/{job} | 404 | owner=sid 스코핑 |
| DELETE /luna-common30-v2/{job} | 404 | owner=sid 스코핑 |
| GET /pms/projects | RED(xfail) | personal 고정 필터라 work에도 개인이 보인다 |
| POST /pms/projects/{pid}/tasks | RED(xfail, SEC2-AUTHZ-02) | scope 검사 없음 |

개인 luna 시작(POST, personal 토큰)은 실제 벤치 잡을 띄우므로 일부러
다루지 않는다. work 403이 풀 거부를 증명한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from authz03_canary import (
    AUTHZ03_P,
    AUTHZ03_W,
    Authz03Env,
    ensure_authz03,
    leaks_personal,
)
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    EngineAccount,
    PmsProject,
    Run,
    RunStatus,
    SessionKind,
)
from ttakkari.orchestrator.runtime import issue_token

pytestmark = [pytest.mark.security]


PROBE_MODEL = f"{AUTHZ03_P}-probe"


@dataclass
class OrchEnv:
    base: Authz03Env
    personal_orch_id: uuid.UUID
    work_orch_id: uuid.UUID
    personal_worker_id: uuid.UUID
    work_worker_id: uuid.UUID
    personal_pms_id: uuid.UUID
    work_pms_id: uuid.UUID
    personal_acct: str
    work_acct: str


@pytest.fixture
async def oenv(make_dir) -> OrchEnv:
    async with sessionmaker()() as db:
        base = await ensure_authz03(db, make_dir)
        suffix = uuid.uuid4().hex[:8]
        p_orch = ChatSession(workspace_id=base.workspace_id, title="AUTHZ03 personal orch",
                            engine="auto", kind=SessionKind.orchestrator, pool="personal")
        w_orch = ChatSession(workspace_id=base.workspace_id, title="AUTHZ03 work orch",
                            engine="auto", kind=SessionKind.orchestrator, pool="work")
        db.add_all([p_orch, w_orch])
        await db.flush()
        p_worker = ChatSession(workspace_id=base.workspace_id, title=f"{AUTHZ03_P} 개인 작업",
                              engine="codex", model=PROBE_MODEL, kind=SessionKind.worker,
                              parent_id=p_orch.id, pool="personal", worker_task_type="general")
        w_worker = ChatSession(workspace_id=base.workspace_id, title=f"{AUTHZ03_W} 회사 작업",
                              engine="codex", kind=SessionKind.worker,
                              parent_id=w_orch.id, pool="work", worker_task_type="general")
        db.add_all([p_worker, w_worker])
        await db.flush()
        p_run = Run(session_id=p_worker.id, workspace_id=base.workspace_id, prompt="개인 작업 지시",
                    engine="codex", model=PROBE_MODEL, status=RunStatus.succeeded,
                    result_text=f"{AUTHZ03_P} 개인 작업 결과")
        w_run = Run(session_id=w_worker.id, workspace_id=base.workspace_id, prompt="회사 작업 지시",
                    engine="codex", status=RunStatus.succeeded,
                    result_text=f"{AUTHZ03_W} 회사 작업 결과")
        p_pms = PmsProject(name=f"{AUTHZ03_P}-pms", scope="personal", status="active")
        w_pms = PmsProject(name=f"{AUTHZ03_W}-pms", scope="work", status="active")
        p_acct = f"{AUTHZ03_P}-acct-{suffix}"
        w_acct = f"{AUTHZ03_W}-acct-{suffix}"
        db.add_all([p_run, w_run, p_pms,
                    w_pms,
                    EngineAccount(name=p_acct, engine="codex", pool="personal"),
                    EngineAccount(name=w_acct, engine="codex", pool="work")])
        await db.commit()
        env = OrchEnv(base=base, personal_orch_id=p_orch.id, work_orch_id=w_orch.id,
                      personal_worker_id=p_worker.id, work_worker_id=w_worker.id,
                      personal_pms_id=p_pms.id, work_pms_id=w_pms.id,
                      personal_acct=p_acct, work_acct=w_acct)
    yield env
    async with sessionmaker()() as db:
        for name in (env.personal_acct, env.work_acct):
            acc = await db.scalar(select(EngineAccount).where(EngineAccount.name == name))
            if acc is not None:
                await db.delete(acc)
        for pid in (env.personal_pms_id, env.work_pms_id):
            proj = await db.get(PmsProject, pid)
            if proj is not None:
                await db.delete(proj)
        await db.commit()


def _work(env: OrchEnv) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(env.work_orch_id)}"}


def _personal(env: OrchEnv) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(env.personal_orch_id)}"}


def _chat(env: OrchEnv) -> dict[str, str]:
    """오케스트레이터가 아닌 일반 세션 토큰. 전부 403이 맞다."""
    return {"Authorization": f"Bearer {issue_token(env.base.personal_session_id)}"}


# ---- 인증 경계 (오케스트레이터가 아니면 전부 403)

@pytest.mark.asyncio
async def test_chat_token_rejected_on_orchestrator_routes(client, oenv):
    assert (await client.get("/api/orchestrator/workers", headers=_chat(oenv))).status_code == 403
    assert (await client.get("/api/orchestrator/pms/projects", headers=_chat(oenv))).status_code == 403
    assert (await client.post("/api/orchestrator/luna-common30-v2", headers=_chat(oenv))).status_code == 403


# ---- 작업 세션 트리 (자기 트리만, pool 상속)

@pytest.mark.asyncio
async def test_work_spawn_worker_inherits_work_pool(client, oenv):
    resp = await client.post("/api/orchestrator/workers", headers=_work(oenv),
                             json={"title": f"{AUTHZ03_W} 파생 작업", "prompt": "범위 확인용 파생 작업",
                                   "engine": "auto", "task_type": "general"})
    assert resp.status_code == 201, resp.text
    assert leaks_personal(resp.json()) is None
    async with sessionmaker()() as db:
        stored = await db.get(ChatSession, resp.json()["worker_id"])
        assert stored is not None
        assert stored.pool == "work" and stored.parent_id == oenv.work_orch_id


@pytest.mark.asyncio
async def test_work_list_workers_only_own_tree(client, oenv):
    resp = await client.get("/api/orchestrator/workers", headers=_work(oenv))
    assert resp.status_code == 200
    ids = [w["worker_id"] for w in resp.json()]
    assert str(oenv.work_worker_id) in ids
    assert str(oenv.personal_worker_id) not in ids
    assert leaks_personal(resp.json()) is None


@pytest.mark.asyncio
async def test_work_get_personal_worker_404(client, oenv):
    forbidden = await client.get(f"/api/orchestrator/workers/{oenv.personal_worker_id}",
                                 headers=_work(oenv))
    assert forbidden.status_code == 404
    allowed = await client.get(f"/api/orchestrator/workers/{oenv.work_worker_id}",
                               headers=_work(oenv))
    assert allowed.status_code == 200
    assert AUTHZ03_W in allowed.text


@pytest.mark.asyncio
async def test_work_message_personal_worker_404(client, oenv):
    resp = await client.post(f"/api/orchestrator/workers/{oenv.personal_worker_id}/messages",
                             headers=_work(oenv),
                             json={"prompt": "", "verification_outcome": "passed"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_work_record_verification_on_own_worker(client, oenv):
    resp = await client.post(f"/api/orchestrator/workers/{oenv.work_worker_id}/messages",
                             headers=_work(oenv),
                             json={"prompt": "", "verification_outcome": "passed",
                                   "verification_note": "대조군 기록"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["verification_status"] == "passed"


@pytest.mark.asyncio
async def test_work_verify_personal_worker_404(client, oenv):
    resp = await client.post(f"/api/orchestrator/workers/{oenv.personal_worker_id}/verification",
                             headers=_work(oenv), json={"outcome": "passed"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_personal_verify_own_worker_passed(client, oenv):
    resp = await client.post(f"/api/orchestrator/workers/{oenv.personal_worker_id}/verification",
                             headers=_personal(oenv), json={"outcome": "passed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["verification_status"] == "passed"


@pytest.mark.asyncio
async def test_work_stop_personal_worker_404(client, oenv):
    forbidden = await client.post(f"/api/orchestrator/workers/{oenv.personal_worker_id}/stop",
                                  headers=_work(oenv))
    assert forbidden.status_code == 404
    allowed = await client.post(f"/api/orchestrator/workers/{oenv.work_worker_id}/stop",
                                headers=_work(oenv))
    assert allowed.status_code == 200


# ---- 집계·컨텍스트·계정 (pool 스코핑)

@pytest.mark.asyncio
async def test_work_metrics_excludes_personal_pool(client, oenv):
    key = f"{PROBE_MODEL}:general"
    work = await client.get("/api/orchestrator/workers/metrics", headers=_work(oenv))
    assert work.status_code == 200
    assert work.json()["pool"] == "work"
    assert key not in {f'{m["model"]}:{m["task_type"]}' for m in work.json()["metrics"]}

    control = await client.get("/api/orchestrator/workers/metrics", headers=_personal(oenv))
    assert control.status_code == 200
    assert key in {f'{m["model"]}:{m["task_type"]}' for m in control.json()["metrics"]}


@pytest.mark.asyncio
async def test_work_context_is_own_only(client, oenv):
    resp = await client.get("/api/orchestrator/context", headers=_work(oenv))
    assert resp.status_code == 200
    assert leaks_personal(resp.json()) is None


@pytest.mark.asyncio
async def test_work_context_reset_allowed_control(client, oenv):
    resp = await client.post("/api/orchestrator/context/reset", headers=_work(oenv))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_work_accounts_usage_company_only(client, oenv):
    work = await client.get("/api/orchestrator/accounts/usage", headers=_work(oenv))
    assert work.status_code == 200
    assert oenv.personal_acct not in work.text
    assert oenv.work_acct in work.text

    control = await client.get("/api/orchestrator/accounts/usage", headers=_personal(oenv))
    assert control.status_code == 200
    assert oenv.personal_acct in control.text


# ---- 벤치 (개인 전용 시작 거부 + owner 스코핑 조회)

@pytest.mark.asyncio
async def test_work_luna_start_forbidden(client, oenv):
    resp = await client.post("/api/orchestrator/luna-common30-v2", headers=_work(oenv))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_luna_job_lookup_unknown_404(client, oenv):
    unknown = str(uuid.uuid4())
    assert (await client.get(f"/api/orchestrator/luna-common30-v2/{unknown}",
                            headers=_work(oenv))).status_code == 404
    assert (await client.delete(f"/api/orchestrator/luna-common30-v2/{unknown}",
                               headers=_personal(oenv))).status_code == 404


# ---- PMS

@pytest.mark.asyncio
async def test_work_pms_projects_excludes_personal(client, oenv):
    resp = await client.get("/api/orchestrator/pms/projects", headers=_work(oenv))
    assert resp.status_code == 200
    assert leaks_personal(resp.json()) is None


@pytest.mark.asyncio
async def test_work_create_pms_task_on_personal_project_forbidden(client, oenv):
    resp = await client.post(f"/api/orchestrator/pms/projects/{oenv.personal_pms_id}/tasks",
                             headers=_work(oenv), json={"title": f"{AUTHZ03_W} 침범 작업"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pms_controls(client, oenv):
    """대조군: work→회사 프로젝트, personal→개인 프로젝트 등록은 성공한다."""
    work = await client.post(f"/api/orchestrator/pms/projects/{oenv.work_pms_id}/tasks",
                             headers=_work(oenv), json={"title": f"{AUTHZ03_W} 정상 작업"})
    assert work.status_code == 201, work.text
    personal = await client.post(
        f"/api/orchestrator/pms/projects/{oenv.personal_pms_id}/tasks",
        headers=_personal(oenv), json={"title": f"{AUTHZ03_P} 직접 작업"})
    assert personal.status_code == 201, personal.text
    found = await client.get("/api/orchestrator/pms/projects", headers=_personal(oenv),
                             params={"q": f"{AUTHZ03_P}-pms"})
    assert found.status_code == 200 and found.json()["projects"]
