"""P6 실제 엔진 여정(opt-in). 기본 스위트에서는 건너뛴다.

TTAKKARI_REAL_ENGINE_JOURNEY=1 일 때만 실제 `claude` 로 Run 2건(모델 호출 2회)을 돌린다. Core·실행부는 test_executor_process_journey
의 실제 entrypoint child 하네스를 그대로 쓰고, 엔진만 fake_claude 대신 실제 claude 바이너리를 가리킨다.
실행: TTAKKARI_REAL_ENGINE_JOURNEY=1 PYTHONPATH=$PWD/src python -m pytest tests/test_real_engine_journey.py -s
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid

import pytest
from sqlalchemy import select
from test_executor_process_journey import (
    assert_core_untouched,
    observed_core,
    poll,
    reap,
    run_row,
    start_executor,
    stop_executor,
    wait_executor_active,
)
from test_run_executor import executor_schema  # noqa: F401 -- shared local fixture

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, RunEvent

pytestmark = pytest.mark.skipif(
    os.environ.get("TTAKKARI_REAL_ENGINE_JOURNEY") != "1",
    reason="실제 모델 호출 여정은 TTAKKARI_REAL_ENGINE_JOURNEY=1 일 때만 실행한다")

TOOL = "mcp__ttakkari__list_memo_categories"


async def wait_terminal(run_id: str, seconds: float = 240):
    async def finished():
        run = await run_row(run_id)
        return run if run.status not in {"queued", "running"} else None

    return await poll(finished, seconds, f"run {run_id} terminal status")


async def engine_sessions(session_id: str) -> dict:
    async with sessionmaker()() as db:
        return dict((await db.get(ChatSession, uuid.UUID(session_id))).engine_sessions or {})


async def events(run_id: str, *types: str) -> list[RunEvent]:
    async with sessionmaker()() as db:
        return list(await db.scalars(select(RunEvent).where(
            RunEvent.run_id == uuid.UUID(run_id), RunEvent.type.in_(types)).order_by(RunEvent.seq)))


@pytest.mark.usefixtures("executor_schema")
async def test_real_claude_run_mcp_call_and_resume_across_executor_replacement(tmp_path, new_session, auth):
    claude = shutil.which("claude")
    assert claude, "claude 바이너리를 찾지 못했다"
    _, sess = await new_session()
    codeword = f"PERSIMMON{uuid.uuid4().hex[:6].upper()}"
    async with observed_core(tmp_path) as (core_proc, core, core_generation, obs):
        # 엔진만 실제 claude 로 바꾼다. MCP 서버가 임시 Core 로 돌아오도록 내부 API 주소를 Core 로 고정한다
        # (기본값은 고정 포트 8787 이라 반드시 덮어쓴다). 사용자 전역 hook/CLAUDE.md 는 읽지 않게 소스를 좁힌다.
        real = {"TTAKKARI_CLAUDE_BIN": claude, "TTAKKARI_CLAUDE_SETTING_SOURCES": "project,local",
                "TTAKKARI_INTERNAL_API_URL": str(core.base_url).rstrip("/"), "TTAKKARI_RUN_TIMEOUT_SECONDS": "240"}
        old, old_log = await start_executor(tmp_path, "old", "e" * 40, **real)
        new = None
        try:
            await wait_executor_active(old, old_log, "e" * 40)
            prompt1 = (f"The codeword is {codeword}. Call the MCP tool list_memo_categories exactly once, then "
                       f"answer in one short sentence containing the codeword and the number of categories.")
            first = await core.post(f"/api/sessions/{sess['id']}/runs", headers=auth,
                                    json={"prompt": prompt1, "model": "haiku", "effort": "low"})
            assert first.status_code == 202, first.text
            run1 = first.json()["id"]
            done1 = await wait_terminal(run1)
            assert done1.status == "succeeded", (done1.status, (done1.error or "")[:400])

            # 1) 실제 모델 응답이 DB/이벤트에 저장되고 engine session id 가 기록된다.
            assert (done1.result_text or "").strip() and codeword in done1.result_text
            # agent.session 이벤트의 id 값은 제품이 마스킹한다. 실제 id 는 ChatSession.engine_sessions 에 저장된다.
            assert (await events(run1, "agent.session"))[0].payload.get("engine_session_id")
            saved1 = await engine_sessions(sess["id"])
            assert len(saved1) == 1
            engine_session = next(iter(saved1.values()))

            # 2) 제품이 Run 에 붙이는 MCP 경로로 도구가 실제로 1회 호출되고 결과가 기록된다.
            calls = [e for e in await events(run1, "tool.call") if e.payload["name"] == TOOL]
            results = await events(run1, "tool.result")
            assert len(calls) == 1, [e.payload["name"] for e in await events(run1, "tool.call")]
            result = next(e for e in results if e.payload["call_id"] == calls[0].payload["call_id"])
            assert not result.payload["is_error"] and result.payload["output"]
            # MCP 서버는 HTTP 오류도 문자열로 돌려주므로 본문이 오류 JSON 이 아닌지까지 본다. 성공 본문이면 토큰이
            # 임시 Core 에서 검증됐다는 뜻이다(고정 포트 8787 로 갔다면 이 Run 의 토큰이 거부된다).
            output = result.payload["output"]
            inner = json.loads(json.loads(output)["result"])  # MCP 래퍼 {"result": "<API JSON 문자열>"}
            assert "error" not in inner and "categories" in inner, output[:200]

            # 4) 2)와 3) 사이에 실행부를 교체한다. 진행 Run 이 없는 idle 교체다.
            mark = obs.mark()
            assert await stop_executor(old) == 0
            new, new_log = await start_executor(tmp_path, "new", "f" * 40, **real)
            await wait_executor_active(new, new_log, "f" * 40)

            # 3) 같은 대화 재개: 새 실행부가 같은 engine session 으로 이어 첫 답의 내용을 기억한다.
            second = await core.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={
                "prompt": "What was the codeword? Reply with only the codeword.", "model": "haiku", "effort": "low"})
            assert second.status_code == 202, second.text
            run2 = second.json()["id"]
            done2 = await wait_terminal(run2)
            assert done2.status == "succeeded", (done2.status, (done2.error or "")[:400])
            assert codeword in (done2.result_text or "")
            saved2 = await engine_sessions(sess["id"])
            resumed = saved2.get(next(iter(saved1)))
            assert not await events(run2, "tool.call")  # 두 번째 답은 도구 없이 기억만으로 나왔다
            window = obs.window(mark, 20)
            await assert_core_untouched(core_proc, core, core_generation, core_proc.pid)
            print(json.dumps({
                "run1": {"status": done1.status, "cost_usd": done1.cost_usd, "reply": done1.result_text[:120],
                         "session": engine_session[:8], "tool": TOOL, "tool_output": output[:80]},
                "run2": {"status": done2.status, "cost_usd": done2.cost_usd, "reply": done2.result_text[:60],
                         "session": (resumed or "")[:8], "same_engine_session": resumed == engine_session},
                "executor_replaced": True, "core_samples": window[0], "waited_s": round(time.monotonic() - mark[1], 1)},
                ensure_ascii=False))
        finally:
            for proc in (old, new):
                if proc is not None:
                    await reap(proc)
            await asyncio.sleep(0)
