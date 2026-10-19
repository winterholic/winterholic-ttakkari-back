"""SEC2-AUTHZ-03 2/3: host_env·deploy·notebooklm·stocks·reverse·oplogs·design 범위 매트릭스.

work 세션 토큰으로 개인 자산 접근 시도를 라우트별로 단언한다. 공용 canary
fixture(``authz03_canary.ensure_authz03``)만 쓰고 실제 개인 자료는 쓰지 않는다.
운영 코드는 고치지 않는다.

라우트별 기대 결과(work 토큰 기준):

| 라우트 | 기대 | 근거 |
| GET /api/agent/execution-capabilities | 200 허용(공유 호스트 기준선, canary 없음) | _personal 검사 없음. 공유 정보라 차단 대상이 아님 |
| POST /api/agent/execution-capabilities/challenge | 403 | _personal이 work 차단 |
| POST /api/agent/execution-capabilities/report | 403 | _personal이 work 차단 |
| POST/GET /api/agent/host-gpu/* (start/status/bench/stop) | 403 | _personal이 work 차단 |
| POST /api/agent/deploy, GET /api/agent/deploy/{job_id} | 403 | personal()이 work 차단 |
| /api/agent/notebooklm/* (auth/status·verify, notebooks CRUD, sources, query, jobs) | 403 | agent_access가 work 차단 |
| GET /api/agent/stocks/tossinvest/* (status/runs/login-help) | 403 | agent_access가 work 차단 |
| /api/agent/reverse/* (start, 목록, 단건, retry, messages) | 403 | agent_access가 work 차단 |
| /api/agent/ops-logs/* (issues, events, trend, deploys, log, sync) | 403 | agent_access가 work 차단 |
| /api/agent/design-systems/* (catalog, items, item) | 403 | agent_access가 work 차단 |

코드 판독상 2/3 범위 모듈은 전부 work 차단 가드가 있어 xfail(RED)은 없다.
Memo·CalendarEvent scope 부재(SEC2-AUTHZ-01)는 1/3 파일에서 이미 xfail로 표시했고
여기서는 다루지 않는다.

주의: personal 대조 호출은 부작용 없는 읽기·상태 조회로만 한다. host-gpu 기동,
배포 예약, NotebookLM 쓰기는 personal 토큰으로도 호출하지 않는다.
"""

from __future__ import annotations

import uuid

import pytest

from authz03_canary import (
    Authz03Env,
    ensure_authz03,
    leaks_personal,
)
from ttakkari.db import sessionmaker
from ttakkari.orchestrator.runtime import issue_token


@pytest.fixture
async def env(make_dir) -> Authz03Env:
    async with sessionmaker()() as db:
        return await ensure_authz03(db, make_dir)


def _work(env: Authz03Env) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(env.work_session_id)}"}


def _personal(env: Authz03Env) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(env.personal_session_id)}"}


def _assert_blocked(resp, *, route: str) -> None:
    assert resp.status_code == 403, f"{route}: {resp.status_code} {resp.text[:200]}"
    assert leaks_personal(resp.text) is None, route


# ---- host_env: 실행 환경·호스트 GPU (api/host_env.py)

@pytest.mark.asyncio
async def test_work_execution_capabilities_shared_allowed(client, env):
    """GET /execution-capabilities는 공유 호스트 기준선이라 work도 200. 개인 canary는 없다."""
    resp = await client.get("/api/agent/execution-capabilities", headers=_work(env))
    assert resp.status_code == 200, resp.text[:200]
    assert leaks_personal(resp.json()) is None

    control = await client.get("/api/agent/execution-capabilities", headers=_personal(env))
    assert control.status_code == 200
    assert leaks_personal(control.json()) is None


@pytest.mark.asyncio
async def test_work_capability_challenge_blocked(client, env):
    _assert_blocked(
        await client.post("/api/agent/execution-capabilities/challenge", headers=_work(env)),
        route="POST execution-capabilities/challenge",
    )
    # 대조군: 토큰 없이 호출하면 401이라 라우트가 살아 있음이 확인된다.
    # personal 토큰의 POST challenge는 Run 귀속 발급 성격이라 대조 호출로 쓰지 않는다.
    assert (await client.post("/api/agent/execution-capabilities/challenge")).status_code == 401


@pytest.mark.asyncio
async def test_work_capability_report_blocked(client, env):
    _assert_blocked(
        await client.post("/api/agent/execution-capabilities/report", headers=_work(env),
                          json={"report": {"challenge": "x", "digest": "y"}}),
        route="POST execution-capabilities/report",
    )
    # 대조군: 토큰 없이 호출하면 401이라 라우트가 살아 있음이 확인된다.
    assert (await client.post("/api/agent/execution-capabilities/report",
                             json={"report": {}})).status_code == 401


@pytest.mark.asyncio
async def test_work_host_gpu_routes_blocked(client, env):
    w = _work(env)
    _assert_blocked(
        await client.post("/api/agent/host-gpu/start", headers=w, json={"model": "tiny"}),
        route="POST host-gpu/start",
    )
    _assert_blocked(await client.get("/api/agent/host-gpu/status", headers=w),
                    route="GET host-gpu/status")
    _assert_blocked(
        await client.post("/api/agent/host-gpu/bench", headers=w, json={"prompt": "x"}),
        route="POST host-gpu/bench",
    )
    _assert_blocked(await client.post("/api/agent/host-gpu/stop", headers=w),
                    route="POST host-gpu/stop")


# ---- deploy: 배포 예약 (api/deployments.py)

@pytest.mark.asyncio
async def test_work_deploy_request_blocked(client, env):
    _assert_blocked(
        await client.post("/api/agent/deploy", headers=_work(env),
                          json={"revision": "a" * 40}),
        route="POST agent/deploy",
    )
    # 대조군: 토큰 없이 호출하면 401이라 라우트가 살아 있음이 확인된다.
    assert (await client.post("/api/agent/deploy", json={"revision": "a" * 40})).status_code == 401


@pytest.mark.asyncio
async def test_work_deploy_status_blocked(client, env):
    _assert_blocked(
        await client.get(f"/api/agent/deploy/{uuid.uuid4()}", headers=_work(env)),
        route="GET agent/deploy/{job_id}",
    )
    # 대조군: personal 조회는 가드를 통과해 미존재 404까지 간다(예약 생성 없음).
    control = await client.get(f"/api/agent/deploy/{uuid.uuid4()}", headers=_personal(env))
    assert control.status_code == 404, control.text[:200]


# ---- notebooklm (api/notebooklm.py, agent_router는 agent_access로 work 차단)

@pytest.mark.asyncio
async def test_work_notebooklm_reads_blocked(client, env):
    w = _work(env)
    for method, path, kwargs in [
        ("GET", "/api/agent/notebooklm/auth/status", {}),
        ("GET", "/api/agent/notebooklm/notebooks", {}),
        ("GET", "/api/agent/notebooklm/notebooks/nb1", {}),
        ("GET", "/api/agent/notebooklm/notebooks/nb1/sources", {}),
        ("GET", "/api/agent/notebooklm/jobs", {}),
        ("GET", "/api/agent/notebooklm/jobs/00000000-0000-0000-0000-000000000000", {}),
    ]:
        resp = await client.request(method, path, headers=w, **kwargs)
        _assert_blocked(resp, route=f"{method} {path}")


@pytest.mark.asyncio
async def test_work_notebooklm_writes_blocked(client, env):
    """쓰기 라우트도 의존성 단계에서 403이라 부작용(노트북 생성·질의) 없이 차단된다."""
    w = _work(env)
    for method, path, kwargs in [
        ("POST", "/api/agent/notebooklm/auth/verify", {}),
        ("POST", "/api/agent/notebooklm/notebooks", {"json": {"title": "work 시도"}}),
        ("PATCH", "/api/agent/notebooklm/notebooks/nb1", {"json": {"title": "work 시도"}}),
        ("DELETE", "/api/agent/notebooklm/notebooks/nb1", {}),
        ("POST", "/api/agent/notebooklm/notebooks/nb1/sources/url",
         {"json": {"url": "https://example.com/work"}}),
        ("POST", "/api/agent/notebooklm/notebooks/nb1/sources/text",
         {"json": {"title": "t", "text": "work 시도"}}),
        ("DELETE", "/api/agent/notebooklm/notebooks/nb1/sources/src1", {}),
        ("POST", "/api/agent/notebooklm/notebooks/nb1/query",
         {"json": {"question": "work 시도?"}}),
        ("POST", "/api/agent/notebooklm/jobs/00000000-0000-0000-0000-000000000000/cancel", {}),
    ]:
        resp = await client.request(method, path, headers=w, **kwargs)
        _assert_blocked(resp, route=f"{method} {path}")


@pytest.mark.asyncio
async def test_personal_notebooklm_status_control(client, env):
    """대조군: personal 토큰의 상태 조회는 200이고 Google 요청을 보내지 않는 경로다."""
    resp = await client.get("/api/agent/notebooklm/auth/status", headers=_personal(env))
    assert resp.status_code == 200, resp.text[:200]
    assert leaks_personal(resp.json()) is None
    # 라우트 생존 확인: 토큰 없으면 401/403.
    assert (await client.get("/api/agent/notebooklm/notebooks")).status_code in (401, 403)


# ---- stocks: 토스증권 어댑터 (api/stocks.py, agent_router는 agent_access로 work 차단)

@pytest.mark.asyncio
async def test_work_stocks_routes_blocked(client, env):
    w = _work(env)
    for path in ("/api/agent/stocks/tossinvest/status",
                 "/api/agent/stocks/tossinvest/runs",
                 "/api/agent/stocks/tossinvest/login-help"):
        _assert_blocked(await client.get(path, headers=w), route=f"GET {path}")


@pytest.mark.asyncio
async def test_personal_stocks_routes_control(client, env):
    """대조군: personal 세션은 세 라우트 모두 200이고 개인 canary는 DB에 없어 노출될 것이 없다."""
    p = _personal(env)
    for path in ("/api/agent/stocks/tossinvest/status",
                 "/api/agent/stocks/tossinvest/runs",
                 "/api/agent/stocks/tossinvest/login-help"):
        resp = await client.get(path, headers=p)
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:200]}"
        assert leaks_personal(resp.json()) is None, path


# ---- reverse (api/reverse.py, agent_access는 None 세션·work 풀을 403)

@pytest.mark.asyncio
async def test_work_reverse_routes_blocked(client, env):
    w = _work(env)
    job = "00000000-0000-0000-0000-000000000000"
    _assert_blocked(
        await client.post("/api/agent/reverse/start", headers=w,
                          json={"description": "work 세션의 분석 시도"}),
        route="POST agent/reverse/start",
    )
    _assert_blocked(await client.get("/api/agent/reverse", headers=w),
                    route="GET agent/reverse")
    _assert_blocked(await client.get(f"/api/agent/reverse/{job}", headers=w),
                    route="GET agent/reverse/{job_id}")
    _assert_blocked(await client.post(f"/api/agent/reverse/{job}/retry", headers=w),
                    route="POST agent/reverse/{job_id}/retry")
    _assert_blocked(
        await client.post(f"/api/agent/reverse/{job}/messages", headers=w,
                          json={"prompt": "work 세션의 추가 지시"}),
        route="POST agent/reverse/{job_id}/messages",
    )


@pytest.mark.asyncio
async def test_personal_reverse_listing_control(client, env):
    """대조군: personal 세션의 작업 목록 조회는 200이다."""
    resp = await client.get("/api/agent/reverse", headers=_personal(env))
    assert resp.status_code == 200, resp.text[:200]
    assert leaks_personal(resp.json()) is None


# ---- oplogs: 운영 로그 (api/oplogs.py, agent_access로 work 차단)

@pytest.mark.asyncio
async def test_work_oplogs_routes_blocked(client, env):
    w = _work(env)
    for method, path, kwargs in [
        ("GET", "/api/agent/ops-logs/issues", {}),
        ("GET", "/api/agent/ops-logs/issues/LOG-1", {}),
        ("PATCH", "/api/agent/ops-logs/issues/LOG-1", {"json": {"status": "ignored"}}),
        ("GET", "/api/agent/ops-logs/events", {}),
        ("GET", "/api/agent/ops-logs/trend", {}),
        ("GET", "/api/agent/ops-logs/deploys", {}),
        ("GET", "/api/agent/ops-logs/deploys/latest/log", {}),
        ("POST", "/api/agent/ops-logs/sync", {}),
    ]:
        resp = await client.request(method, path, headers=w, **kwargs)
        _assert_blocked(resp, route=f"{method} {path}")


@pytest.mark.asyncio
async def test_personal_oplogs_issues_control(client, env):
    """대조군: personal 세션의 이슈 목록 조회는 200이다."""
    resp = await client.get("/api/agent/ops-logs/issues", headers=_personal(env))
    assert resp.status_code == 200, resp.text[:200]
    assert leaks_personal(resp.json()) is None


# ---- design-system (api/design_system.py, agent_access로 work 차단)

@pytest.mark.asyncio
async def test_work_design_system_routes_blocked(client, env):
    w = _work(env)
    _assert_blocked(await client.get("/api/agent/design-systems", headers=w),
                    route="GET agent/design-systems")
    _assert_blocked(await client.get("/api/agent/design-systems/ttakkari/items", headers=w),
                    route="GET agent/design-systems/{system}/items")
    _assert_blocked(
        await client.get("/api/agent/design-systems/ttakkari/item", headers=w,
                         params={"item": "x"}),
        route="GET agent/design-systems/{system}/item",
    )


@pytest.mark.asyncio
async def test_personal_design_system_catalog_control(client, env):
    """대조군: personal 세션은 가드를 통과한다(백엔드 미가용 시 503일 수 있어 403이 아님만 본다)."""
    resp = await client.get("/api/agent/design-systems", headers=_personal(env))
    assert resp.status_code not in (401, 403), resp.text[:200]
    assert leaks_personal(resp.text) is None
