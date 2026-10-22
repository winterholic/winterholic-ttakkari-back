"""API processes can retire while a separate executor owns the engine child.

Host/private-PG test; no real provider calls. Uses existing fake CLI + SSE routes.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from test_generation_processes import server, wait_status
from test_run_executor import executor_schema  # noqa: F401 -- shared local fixture

from ttakkari.db import sessionmaker
from ttakkari.generation import activate
from ttakkari.models import Run, RunEvent, RuntimeControl, RuntimeGeneration


@asynccontextmanager
async def executor_process(tmp_path):
    logpath = tmp_path / "executor.log"
    env = os.environ | {"TTAKKARI_RUNTIME_GENERATION": "true", "TTAKKARI_RUN_EXECUTOR_MODE": "remote",
                        "TTAKKARI_RELEASE_REVISION": "e" * 40, "TTAKKARI_RUNTIME_POLL_SECONDS": "0.1"}
    with logpath.open("wb") as output:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "ttakkari.cli", "executor", "--activate",
            env=env, stdout=output, stderr=output, start_new_session=True)
        try:
            for _ in range(150):
                assert proc.returncode is None, logpath.read_text()[-3000:]
                async with sessionmaker()() as db:
                    control = await db.get(RuntimeControl, 1)
                    if control and control.executor_generation:
                        row = await db.get(RuntimeGeneration, control.executor_generation)
                        if row and row.pid == proc.pid and row.state == "active":
                            yield proc
                            return
                await asyncio.sleep(0.05)
            raise AssertionError("executor registration missing: " + logpath.read_text()[-3000:])
        finally:
            if proc.returncode is None:
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()


@pytest.mark.usefixtures("executor_schema")
async def test_api_replacement_keeps_executor_child_and_sse_cursor(tmp_path, new_session, auth, monkeypatch):
    _, sess = await new_session()
    monkeypatch.setenv("TTAKKARI_RUN_EXECUTOR_MODE", "remote")
    old = server(tmp_path, "a" * 40)
    a, ready_a = await old.__aenter__()
    old_open = True
    try:
        aid = uuid.UUID(ready_a["generation"])
        await activate(aid, expected=None)
        async with executor_process(tmp_path) as executor:
            response = await a.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": "SLEEP"})
            assert response.status_code == 202, response.text
            rid = response.json()["id"]
            before = await wait_status(a, rid, auth, "running")
            # Read a real SSE event, disconnect, and carry its actual cursor.
            cursor = 0
            async with a.stream("GET", f"/api/runs/{rid}/stream", headers=auth) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("id:"):
                        cursor = int(line.split(":", 1)[1])
                        break
            assert cursor > 0
            async with server(tmp_path, "b" * 40) as (b, ready_b):
                await activate(uuid.UUID(ready_b["generation"]), expected=aid)
                assert (await a.get("/api/ready")).json()["drain_ready"]
                await old.__aexit__(None, None, None)
                old_open = False
                os.kill(before["pid"], 0)  # same engine PID is still alive after old API exit
                assert executor.returncode is None
                live = await wait_status(b, rid, auth, "running")
                assert live["pid"] == before["pid"]
                assert (await b.post(f"/api/runs/{rid}/cancel", headers=auth)).status_code == 200
                await wait_status(b, rid, auth, "cancelled")
                resumed = await b.get(f"/api/runs/{rid}/stream", headers=auth | {"Last-Event-ID": str(cursor)})
                ids = [int(line.split(":", 1)[1]) for line in resumed.text.splitlines() if line.startswith("id:")]
                async with sessionmaker()() as db:
                    expected = list(await db.scalars(select(RunEvent.seq).where(
                        RunEvent.run_id == uuid.UUID(rid), RunEvent.seq > cursor).order_by(RunEvent.seq)))
                assert ids == expected and ids and min(ids) > cursor
                assert "event: end" in resumed.text
    finally:
        if old_open:
            await old.__aexit__(None, None, None)


@pytest.mark.usefixtures("executor_schema")
async def test_idle_executor_restart_resumes_db_waiting_queue(tmp_path, new_session, auth, monkeypatch):
    from datetime import timedelta

    from ttakkari.models import utcnow

    ws, sess = await new_session()
    monkeypatch.setenv("TTAKKARI_RUN_EXECUTOR_MODE", "remote")
    async with server(tmp_path, "a" * 40) as (api, ready):
        await activate(uuid.UUID(ready["generation"]), expected=None)
        async with sessionmaker()() as db:
            run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                      prompt="waiting across executor restart", engine="claude", resume_after=utcnow()+timedelta(hours=1))
            db.add(run)
            await db.commit()
            rid = str(run.id)
        async with executor_process(tmp_path):
            assert (await api.get(f"/api/runs/{rid}", headers=auth)).json()["status"] == "queued"
        async with executor_process(tmp_path):
            from ttakkari.generation import GenerationRuntime
            runtime = GenerationRuntime(tmp_path, revision="a"*40, api_url="http://127.0.0.1:18799")
            await runtime.command("release_waiting", key="release-after-restart")
            await wait_status(api, rid, auth, "succeeded")
