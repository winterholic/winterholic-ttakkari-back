from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
import uuid

from conftest import FAKES, agent_calls
from helpers import run_events, run_to_end, sse_events, start_run, wait_run, wait_status
from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunEvent
from ttakkari.runs.manager import recover_interrupted


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def wait_agent_log(n: int = 1, timeout: float = 10.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while len(agent_calls()) < n:
        assert time.monotonic() < deadline, "가짜 에이전트가 기동하지 않음"
        await asyncio.sleep(0.05)
    return agent_calls()


async def test_run_lifecycle_succeeds_with_events_and_artifact(client, auth, new_session):
    _ws, sess = await new_session()
    r = await client.post(f"/api/sessions/{sess['id']}/runs", json={"prompt": "hello"}, headers=auth)
    assert r.status_code == 202
    run = r.json()
    assert run["status"] == "queued"

    done = await wait_run(client, auth, run["id"])
    assert done["status"] == "succeeded", done
    assert done["exit_code"] == 0 and done["error"] is None
    assert done["cost_usd"] == 0.01 and done["result_text"] == "done"
    assert done["started_at"] and done["finished_at"]
    assert done["checkpoint_ref"] is None  # git 이 아닌 워크스페이스

    evs = await run_events(client, auth, run["id"])
    types = [e["type"] for e in evs]
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    assert done["last_seq"] == len(evs)
    assert types[0] == "run.status" and evs[0]["payload"]["status"] == "queued"
    assert evs[1]["type"] == "run.status" and evs[1]["payload"]["status"] == "running"
    for t in ("agent.session", "tool.call", "tool.result", "message", "usage", "artifact.created"):
        assert t in types, t
    assert types[-1] == "run.finished" and evs[-1]["payload"]["status"] == "succeeded"
    assert types.index("artifact.created") < types.index("run.finished")
    # 파일 본문 전체가 아니라 크기만 이벤트로 보낸다.
    call = next(e for e in evs if e["type"] == "tool.call")
    assert call["payload"]["name"] == "Write" and "content" not in call["payload"]["input"]

    created = [e["payload"]["artifact"] for e in evs if e["type"] == "artifact.created"]
    assert [a["filename"] for a in created] == ["report.md"]
    assert created[0]["source"] == "agent_output" and created[0]["run_id"] == run["id"]

    sess_now = (await client.get(f"/api/sessions/{sess['id']}", headers=auth)).json()
    assert sess_now["engine_session_id"] and sess_now["last_run_status"] == "succeeded"
    assert sess_now["active_run_id"] is None
    # 첫 프롬프트로 제목이 바뀐다.
    assert sess_now["title"] == "hello"


async def test_second_run_resumes_same_engine_session(client, auth, new_session):
    _ws, sess = await new_session()
    r1 = await run_to_end(client, auth, sess["id"], "first")
    assert r1["status"] == "succeeded"
    engine_sid = (await client.get(f"/api/sessions/{sess['id']}", headers=auth)).json()["engine_session_id"]
    assert engine_sid

    r2 = await run_to_end(client, auth, sess["id"], "second")
    assert r2["status"] == "succeeded"
    calls = agent_calls()
    assert len(calls) == 2
    assert "--resume" not in calls[0]["argv"]
    argv = calls[1]["argv"]
    assert argv[argv.index("--resume") + 1] == engine_sid
    assert (await client.get(f"/api/sessions/{sess['id']}", headers=auth)).json()["engine_session_id"] == engine_sid
    runs = (await client.get(f"/api/sessions/{sess['id']}/runs", headers=auth)).json()
    assert [x["prompt"] for x in runs] == ["first", "second"]


async def test_child_env_is_scrubbed_and_cwd_is_workspace(client, auth, new_session):
    ws, sess = await new_session()
    await run_to_end(client, auth, sess["id"], "env")
    call = agent_calls()[0]
    assert call["cwd"] == ws["root_path"]
    assert "TTAKKARI_RUN_ID" in call["ttk_env"]
    assert "TTAKKARI_DATABASE_URL" not in call["ttk_env"] and "TTAKKARI_JWT_SECRET" not in call["ttk_env"]
    argv = call["argv"]
    assert "--dangerously-skip-permissions" in argv and argv[0] == "-p"


async def test_second_run_while_running_is_409(client, auth, new_session):
    _ws, sess = await new_session()
    run = await start_run(client, auth, sess["id"], "SLEEP")
    await wait_status(client, auth, run["id"], "running")
    r = await client.post(f"/api/sessions/{sess['id']}/runs", json={"prompt": "again"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "conflict"
    sess_now = (await client.get(f"/api/sessions/{sess['id']}", headers=auth)).json()
    assert sess_now["active_run_id"] == run["id"]
    await client.post(f"/api/runs/{run['id']}/cancel", headers=auth)


async def test_run_in_other_session_is_not_blocked(client, auth, new_session):
    ws, sess = await new_session()
    r2 = await client.post("/api/sessions", json={"workspace_id": ws["id"]}, headers=auth)
    other = r2.json()
    slow = await start_run(client, auth, sess["id"], "SLEEP")
    await wait_status(client, auth, slow["id"], "running")
    assert (await run_to_end(client, auth, other["id"], "quick"))["status"] == "succeeded"
    await client.post(f"/api/runs/{slow['id']}/cancel", headers=auth)


async def test_fail_scenario_marks_failed_with_error(client, auth, new_session):
    _ws, sess = await new_session()
    run = await run_to_end(client, auth, sess["id"], "FAIL please")
    assert run["status"] == "failed"
    assert run["exit_code"] == 1 and "fake failure" in run["error"]
    last = (await run_events(client, auth, run["id"]))[-1]
    assert last["type"] == "run.finished" and last["payload"]["status"] == "failed"
    # 실패한 Run 뒤에는 새 Run 을 받을 수 있다.
    assert (await run_to_end(client, auth, sess["id"], "ok again"))["status"] == "succeeded"


async def test_garbage_lines_become_log_events(client, auth, new_session):
    _ws, sess = await new_session()
    run = await run_to_end(client, auth, sess["id"], "GARBAGE")
    assert run["status"] == "succeeded"
    logs = [e["payload"] for e in await run_events(client, auth, run["id"]) if e["type"] == "log"]
    assert [x["text"] for x in logs] == ["this line is not json", "{broken json"]
    assert all(x["stream"] == "stdout" for x in logs)


async def test_missing_binary_fails_run(client, auth, new_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "claude_bin", "/nonexistent/claude")
    _ws, sess = await new_session()
    run = await run_to_end(client, auth, sess["id"], "x")
    assert run["status"] == "failed" and "찾을 수 없습니다" in run["error"]


async def test_cancel_kills_process(client, auth, new_session):
    _ws, sess = await new_session()
    run = await start_run(client, auth, sess["id"], "SLEEP")
    await wait_status(client, auth, run["id"], "running")
    pid = (await wait_agent_log())[0]["pid"]
    assert pid_alive(pid)

    r = await client.post(f"/api/runs/{run['id']}/cancel", headers=auth)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled", r.json()
    assert not pid_alive(pid)
    last = (await run_events(client, auth, run["id"]))[-1]
    assert last["type"] == "run.finished" and last["payload"]["status"] == "cancelled"
    # 이미 끝난 Run 의 cancel 은 상태 그대로 돌려준다.
    again = await client.post(f"/api/runs/{run['id']}/cancel", headers=auth)
    assert again.status_code == 200 and again.json()["status"] == "cancelled"


async def test_cancel_unknown_run_404(client, auth):
    r = await client.post(f"/api/runs/{uuid.uuid4()}/cancel", headers=auth)
    assert r.status_code == 404


async def test_cancel_run_without_live_process_is_409(client, auth, new_session):
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]), prompt="x", engine="claude",
                  status="running")
        db.add(run)
        await db.commit()
        rid = run.id
    r = await client.post(f"/api/runs/{rid}/cancel", headers=auth)
    assert r.status_code == 409


async def test_timeout_marks_failed_and_kills_process(client, auth, new_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 2)
    _ws, sess = await new_session()
    run = await start_run(client, auth, sess["id"], "SLEEP")
    pid = (await wait_agent_log())[0]["pid"]
    done = await wait_run(client, auth, run["id"], timeout=15)
    assert done["status"] == "failed"
    assert "시간 초과" in done["error"]
    assert not pid_alive(pid)


async def test_stop_all_cancels_active_runs(client, auth, new_session):
    _ws, sess = await new_session()
    run = await start_run(client, auth, sess["id"], "SLEEP")
    await wait_status(client, auth, run["id"], "running")
    pid = (await wait_agent_log())[0]["pid"]
    r = await client.post("/api/system/stop-all", headers=auth)
    assert r.json()["stopped"] == [run["id"]]
    assert (await wait_run(client, auth, run["id"]))["status"] == "cancelled"
    assert not pid_alive(pid)


def _spawn_agent_like() -> subprocess.Popen:
    """명령줄에 claude 가 보이는 별도 프로세스 그룹(재시작 전에 남은 에이전트 역할)."""
    return subprocess.Popen([str(FAKES / "fake_claude.py"), "-p", "SLEEP"], start_new_session=True,
                            stdout=subprocess.DEVNULL)


async def _insert_runs(ws, sess, pid: int | None):
    async with sessionmaker()() as db:
        running = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]), prompt="r",
                      engine="claude", status="running", pid=pid, last_seq=2)
        queued = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]), prompt="q",
                     engine="claude", status="queued")
        done = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]), prompt="d",
                   engine="claude", status="succeeded")
        db.add_all([running, queued, done])
        await db.commit()
        return running.id, queued.id, done.id


async def test_recover_interrupted_marks_running_runs(client, auth, new_session):
    ws, sess = await new_session()
    child = _spawn_agent_like()
    try:
        ids = await _insert_runs(ws, sess, child.pid)
        await asyncio.sleep(0.5)  # ps 에 명령줄이 보일 때까지
        assert await recover_interrupted() == 2

        for rid in ids[:2]:
            run = (await client.get(f"/api/runs/{rid}", headers=auth)).json()
            assert run["status"] == "interrupted" and run["finished_at"] and "재시작" in run["error"]
        assert (await client.get(f"/api/runs/{ids[2]}", headers=auth)).json()["status"] == "succeeded"

        evs = await run_events(client, auth, str(ids[0]))
        assert [e["seq"] for e in evs] == [3]  # last_seq=2 다음 번호
        assert evs[0]["type"] == "run.finished" and evs[0]["payload"]["status"] == "interrupted"
        # 남은 에이전트 프로세스 그룹에는 SIGTERM 이 갔다.
        assert child.wait(timeout=5) == -signal.SIGTERM
        # interrupted 이후에는 같은 세션에 새 Run 을 받을 수 있다.
        assert (await run_to_end(client, auth, sess["id"], "after restart"))["status"] == "succeeded"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


async def test_recover_does_not_kill_unrelated_process_with_reused_pid(client, auth, new_session):
    ws, sess = await new_session()
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)  # noqa: ASYNC220
    try:
        ids = await _insert_runs(ws, sess, other.pid)
        assert await recover_interrupted() == 2
        assert (await client.get(f"/api/runs/{ids[0]}", headers=auth)).json()["status"] == "interrupted"
        await asyncio.sleep(0.3)
        assert other.poll() is None  # PID 재사용으로 엉뚱한 프로세스를 죽이면 안 된다
    finally:
        other.kill()
        other.wait()


async def _finished_run(client, auth, new_session):
    _ws, sess = await new_session()
    run = await run_to_end(client, auth, sess["id"], "stream me")
    assert run["status"] == "succeeded"
    return run


async def test_sse_stream_full_replay_then_end(client, auth, new_session):
    run = await _finished_run(client, auth, new_session)
    r = await client.get(f"/api/runs/{run['id']}/stream", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = sse_events(r.text)
    assert [e["seq"] for e in evs] == list(range(1, run["last_seq"] + 1))
    assert [e["id"] for e in evs] == [e["seq"] for e in evs]
    assert "event: end" in r.text


async def test_sse_resumes_after_last_event_id(client, auth, new_session):
    run = await _finished_run(client, auth, new_session)
    cut = 3
    assert run["last_seq"] > cut
    r = await client.get(f"/api/runs/{run['id']}/stream", headers={**auth, "Last-Event-ID": str(cut)})
    seqs = [e["seq"] for e in sse_events(r.text)]
    assert seqs == list(range(cut + 1, run["last_seq"] + 1))
    assert "event: end" in r.text


async def test_sse_after_query_and_last_event_id_take_max(client, auth, new_session):
    run = await _finished_run(client, auth, new_session)
    last = run["last_seq"]
    r = await client.get(f"/api/runs/{run['id']}/stream", params={"after": 2}, headers={**auth, "Last-Event-ID": str(last - 1)})
    assert [e["seq"] for e in sse_events(r.text)] == [last]
    # 이미 다 받은 상태면 end 만 온다.
    r = await client.get(f"/api/runs/{run['id']}/stream", headers={**auth, "Last-Event-ID": str(last)})
    assert sse_events(r.text) == [] and "event: end" in r.text


async def test_sse_ignores_non_numeric_last_event_id(client, auth, new_session):
    run = await _finished_run(client, auth, new_session)
    r = await client.get(f"/api/runs/{run['id']}/stream", headers={**auth, "Last-Event-ID": "abc"})
    assert len(sse_events(r.text)) == run["last_seq"]


async def test_sse_unknown_run_404_and_requires_auth(client, auth):
    assert (await client.get(f"/api/runs/{uuid.uuid4()}/stream", headers=auth)).status_code == 404
    assert (await client.get(f"/api/runs/{uuid.uuid4()}/stream")).status_code == 401


async def test_events_endpoint_after_and_limit(client, auth, new_session):
    run = await _finished_run(client, auth, new_session)
    r = await client.get(f"/api/runs/{run['id']}/events", params={"after": 2, "limit": 2}, headers=auth)
    assert [e["seq"] for e in r.json()] == [3, 4]


async def test_create_run_validation_and_archived(client, auth, new_session):
    _ws, sess = await new_session()
    assert (await client.post(f"/api/sessions/{sess['id']}/runs", json={"prompt": ""}, headers=auth)).status_code == 422
    assert (await client.post(f"/api/sessions/{uuid.uuid4()}/runs", json={"prompt": "x"}, headers=auth)).status_code == 404
    await client.patch(f"/api/sessions/{sess['id']}", json={"archived": True}, headers=auth)
    r = await client.post(f"/api/sessions/{sess['id']}/runs", json={"prompt": "x"}, headers=auth)
    assert r.status_code == 409


async def test_codex_engine_new_then_resume(client, auth, new_session):
    _ws, sess = await new_session(engine="codex")
    assert sess["engine"] == "codex"
    r1 = await run_to_end(client, auth, sess["id"], "hi codex")
    assert r1["status"] == "succeeded" and r1["result_text"] == "codex done"
    assert r1["usage"]["output_tokens"] == 5
    thread = (await client.get(f"/api/sessions/{sess['id']}", headers=auth)).json()["engine_session_id"]
    assert thread
    r2 = await run_to_end(client, auth, sess["id"], "again")
    assert r2["status"] == "succeeded"
    a1, a2 = [c["argv"] for c in agent_calls()]
    assert a1[0] == "exec" and "resume" not in a1[:2] and "-C" in a1
    assert a2[:2] == ["exec", "resume"] and a2[a2.index("--") - 1] == thread
    assert a2[-1] == "again"
    async with sessionmaker()() as db:
        assert (await db.scalar(select(ChatSession.engine_session_id))) == thread


async def test_codex_failure(client, auth, new_session):
    _ws, sess = await new_session(engine="codex")
    run = await run_to_end(client, auth, sess["id"], "FAIL")
    assert run["status"] == "failed" and "fake codex failure" in run["error"]


async def test_run_events_rows_unique_seq_in_db(client, auth, new_session):
    run = await _finished_run(client, auth, new_session)
    async with sessionmaker()() as db:
        seqs = list(await db.scalars(select(RunEvent.seq).where(RunEvent.run_id == uuid.UUID(run["id"])).order_by(RunEvent.seq)))
    assert seqs == sorted(set(seqs))
