"""Real ASGI generations + fake engines, sharing only a disposable test DB/home."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest

from ttakkari.db import sessionmaker
from ttakkari.generation import activate
from ttakkari.models import Run


@asynccontextmanager
async def server(tmp_path, revision):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    env = os.environ | {"TTAKKARI_RUNTIME_GENERATION": "true", "TTAKKARI_RUNTIME_API_URL": origin,
        "TTAKKARI_RELEASE_REVISION": revision, "TTAKKARI_RUNTIME_POLL_SECONDS": "0.1",
        "TTAKKARI_RUNTIME_SSE_DRAIN_SECONDS": "1",
        "TTAKKARI_LOCAL_STT_ENABLED": "false", "TTAKKARI_DESK_WARM_POOL": "false"}
    logpath = tmp_path / (revision[0] + ".log")
    with logpath.open("wb") as output:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "ttakkari.cli", "serve",
            "--host", "127.0.0.1", "--port", str(port), env=env, stdout=output, stderr=output,
            start_new_session=True)
        async with httpx.AsyncClient(base_url=origin, timeout=2) as client:
            try:
                for _ in range(100):
                    try:
                        response = await client.get("/api/ready")
                        if response.status_code == 200:
                            yield client, response.json()
                            break
                    except httpx.HTTPError:
                        pass
                    if proc.returncode is not None:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("generation readiness missing: " + logpath.read_text()[-2000:])
                assert proc.returncode is None, logpath.read_text()[-2000:]
            finally:
                if proc.returncode is None:
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 8)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()


async def wait_status(client, run_id, auth, expected):
    for _ in range(150):
        value = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
        if value["status"] == expected:
            if expected == "running":
                async with sessionmaker()() as db:
                    pid = (await db.get(Run, uuid.UUID(run_id))).pid
                if not pid:
                    await asyncio.sleep(0.04)
                    continue
                value["pid"] = pid
            return value
        await asyncio.sleep(0.04)
    raise AssertionError(value)


async def test_real_two_process_cutover_preserves_old_cli_and_runs_new_work(tmp_path, new_session, auth):
    _, old_session = await new_session()
    _, new_sess = await new_session()
    async with server(tmp_path, "a" * 40) as (a, ready_a):
        assert ready_a["state"] == "standby" and ready_a["owned_runs"] == 0
        aid = uuid.UUID(ready_a["generation"])
        await activate(aid, expected=None)
        first = await a.post(f"/api/sessions/{old_session['id']}/runs", headers=auth, json={"prompt": "SLEEP"})
        assert first.status_code == 202, first.text
        old_id = first.json()["id"]
        before = await wait_status(a, old_id, auth, "running")
        async with server(tmp_path, "b" * 40) as (b, ready_b):
            bid = uuid.UUID(ready_b["generation"])
            assert ready_b["state"] == "standby"
            assert (await a.get(f"/api/runs/{old_id}", headers=auth)).json()["status"] == "running"
            await activate(bid, expected=aid)
            second = await b.post(f"/api/sessions/{new_sess['id']}/runs", headers=auth, json={"prompt": "new generation"})
            assert second.status_code == 202, second.text
            await wait_status(b, second.json()["id"], auth, "succeeded")
            old = (await b.get(f"/api/runs/{old_id}", headers=auth)).json()
            async with sessionmaker()() as db:
                live_pid = (await db.get(Run, uuid.UUID(old_id))).pid
            assert old["status"] == "running" and live_pid == before["pid"]
            response = await b.post(f"/api/runs/{old_id}/cancel", headers=auth)
            assert response.status_code == 200, response.text
            await wait_status(b, old_id, auth, "cancelled")
            snapshot = (await a.get("/api/ready")).json()
            assert snapshot["state"] == "draining" and snapshot["owned_runs"] == 0


async def test_real_three_process_cutovers_keep_both_old_engines_and_proxy(tmp_path, new_session, auth):
    sessions = [(await new_session())[1] for _ in range(3)]
    async with AsyncExitStack() as stack:
        a, ra = await stack.enter_async_context(server(tmp_path, "a" * 40))
        aid = uuid.UUID(ra["generation"])
        await activate(aid, expected=None)
        routed, switch = await stack.enter_async_context(proxy(tmp_path, a.base_url))
        first = await routed.post(f"/api/sessions/{sessions[0]['id']}/runs", headers=auth, json={"prompt": "SLEEP"})
        assert first.status_code == 202, first.text
        first_id = first.json()["id"]
        first_before = await wait_status(a, first_id, auth, "running")
        b, rb = await stack.enter_async_context(server(tmp_path, "b" * 40))
        bid = uuid.UUID(rb["generation"])
        await activate(bid, expected=aid)
        await switch(b.base_url)
        second = await routed.post(f"/api/sessions/{sessions[1]['id']}/runs", headers=auth, json={"prompt": "SLEEP"})
        assert second.status_code == 202, second.text
        second_id = second.json()["id"]
        second_before = await wait_status(b, second_id, auth, "running")
        c, rc = await stack.enter_async_context(server(tmp_path, "c" * 40))
        await activate(uuid.UUID(rc["generation"]), expected=bid)
        await switch(c.base_url)
        assert (await routed.get("/api/version")).json()["revision"] == "c" * 40
        third = await routed.post(f"/api/sessions/{sessions[2]['id']}/runs", headers=auth,
                                  json={"prompt": "third generation work"})
        assert third.status_code == 202, third.text
        await wait_status(routed, third.json()["id"], auth, "succeeded")
        for owner, rid, before in ((a, first_id, first_before), (b, second_id, second_before)):
            live = await wait_status(owner, rid, auth, "running")
            assert live["pid"] == before["pid"]
            assert (await routed.post(f"/api/runs/{rid}/cancel", headers=auth)).status_code == 200
            await wait_status(owner, rid, auth, "cancelled")
            ready = (await owner.get("/api/ready")).json()
            assert ready["state"] == "draining" and ready["owned_runs"] == 0


@asynccontextmanager
async def proxy(tmp_path, backend):
    from ttakkari.generation_deployment import proxy_config
    binary = shutil.which("caddy")
    if not binary:
        pytest.skip("Caddy is required for the real reverse-proxy test")
    with socket.socket() as s1, socket.socket() as s2:
        s1.bind(("127.0.0.1", 0))
        s2.bind(("127.0.0.1", 0))
        port, admin = s1.getsockname()[1], s2.getsockname()[1]

    def config(origin):
        value = proxy_config(0)
        value["admin"] = {"listen": f"127.0.0.1:{admin}", "config": {"persist": False}}
        server = value["apps"]["http"]["servers"]["ttakkari"]
        server["listen"] = [f"127.0.0.1:{port}"]
        server["routes"][-1]["handle"][0]["upstreams"] = [{"dial": str(origin).split("//")[1].rstrip("/")}]
        return value

    path = tmp_path / "caddy.json"
    path.write_text(json.dumps(config(backend)))
    with (tmp_path / "proxy.log").open("wb") as log:
        proc = await asyncio.create_subprocess_exec(binary, "run", "--config", str(path), stdout=log, stderr=log)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            try:
                for _ in range(100):
                    try:
                        if (await client.get("/api/health")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError((tmp_path / "proxy.log").read_text())

                async def switch(origin):
                    async with httpx.AsyncClient() as admin_client:
                        result = await admin_client.post(f"http://127.0.0.1:{admin}/load", json=config(origin))
                        assert result.status_code == 200, result.text

                yield client, switch
            finally:
                if proc.returncode is None:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), 5)


async def test_caddy_switch_has_no_http_errors_and_sse_replays_from_cursor(tmp_path, new_session, auth):
    _ws, sess = await new_session()
    async with server(tmp_path, "a" * 40) as (a, ra):
        aid = uuid.UUID(ra["generation"])
        await activate(aid, expected=None)
        async with server(tmp_path, "b" * 40) as (b, rb), proxy(tmp_path, a.base_url) as (client, switch):
            created = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": "SLEEP"})
            assert created.status_code == 202
            run_id = created.json()["id"]
            await wait_status(a, run_id, auth, "running")
            statuses = []
            done = asyncio.Event()

            async def poll():
                while not done.is_set():
                    response = await client.get("/api/version")
                    statuses.append(response.status_code)
                    await asyncio.sleep(0.02)

            task = asyncio.create_task(poll())
            try:
                cursor = 0
                async with client.stream("GET", f"/api/runs/{run_id}/stream", headers=auth) as response:
                    lines = response.aiter_lines()
                    async for line in lines:
                        if line.startswith("id:"):
                            cursor = int(line[3:])
                            break
                    assert cursor > 0
                    await activate(uuid.UUID(rb["generation"]), expected=aid)
                    await switch(b.base_url)
                    async with asyncio.timeout(5):
                        async for line in lines:
                            if line.startswith("id:"):
                                cursor = max(cursor, int(line[3:]))
                assert (await client.get("/api/version")).json()["revision"] == "b" * 40
                cancelled = await client.post(f"/api/runs/{run_id}/cancel", headers=auth)
                assert cancelled.status_code == 200, cancelled.text
                await wait_status(b, run_id, auth, "cancelled")
                replay = await client.get(f"/api/runs/{run_id}/stream", headers=auth | {"Last-Event-ID": str(cursor)})
                ids = [int(line[3:]) for line in replay.text.splitlines() if line.startswith("id:")]
                events = (await b.get(f"/api/runs/{run_id}/events", headers=auth)).json()
                assert ids == [e["seq"] for e in events if e["seq"] > cursor]
                assert "event: end" in replay.text
            finally:
                done.set()
                await task
            assert len(statuses) >= 2 and set(statuses) == {200}
