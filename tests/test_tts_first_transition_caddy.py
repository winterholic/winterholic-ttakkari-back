"""Isolated real Caddy transport + old API algorithm; never production services."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import uuid

import httpx
import pytest
from test_tts_first_transition import (
    _controller,
    _job,
    _job_row,
    _old_accept,
    _old_guard_pass,
    _seed,
    _Slots,
)

from ttakkari.generation import activate
from ttakkari.generation_deployment import proxy_config


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_real_caddy_reload_does_not_drain_forwarded_but_unadmitted_post(tmp_path):
    caddy = shutil.which("caddy")
    if caddy is None:
        pytest.fail("Caddy required for this explicit transport verification")
    old_id, new_id = await _seed()
    forwarded, deliver, checked, commit = (asyncio.Event() for _ in range(4))
    handler_tasks = set()
    admitted = 0

    async def upstream(reader, writer):
        nonlocal admitted
        handler_tasks.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            forwarded.set()  # Reverse proxy has forwarded, API admission has not run.
            await deliver.wait()
            admitted += 1
            jid = await _old_accept(old_id, commit, checked, ["forwarded original"])
            body = json.dumps({"job_id": str(jid)}).encode()
            writer.write(b"HTTP/1.1 202 Accepted\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            admitted = max(0, admitted - 1)
            writer.close()
            await writer.wait_closed()
            handler_tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = server.sockets[0].getsockname()[1]
    admin_port, public_port = _free_port(), _free_port()
    assert len({admin_port, public_port, upstream_port, 8787, 20191}) == 5

    def config(closed=False):
        value = proxy_config(0, tts_ingress_closed=closed)
        value["admin"]["listen"] = f"127.0.0.1:{admin_port}"
        http_server = value["apps"]["http"]["servers"]["ttakkari"]
        http_server["listen"] = [f"127.0.0.1:{public_port}"]
        http_server["routes"][-1]["handle"][0]["upstreams"][0]["dial"] = f"127.0.0.1:{upstream_port}"
        return value

    config_path = tmp_path / "caddy.json"
    config_path.write_text(json.dumps(config()), encoding="utf-8")
    log_path = tmp_path / "caddy.log"
    post = None
    with log_path.open("wb") as logfile:
        proc = await asyncio.create_subprocess_exec(
            caddy, "run", "--config", str(config_path), stdout=logfile, stderr=logfile,
            env=dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "config"),
                     XDG_DATA_HOME=str(tmp_path / "data")))
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                admin = f"http://127.0.0.1:{admin_port}"
                for _ in range(60):
                    if proc.returncode is not None:
                        pytest.fail(log_path.read_text(encoding="utf-8"))
                    try:
                        ready = await client.get(admin + "/config/")
                        if ready.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("isolated Caddy admin readiness timed out")

                class Slots(_Slots):
                    async def close_tts_ingress(self, slot):
                        response = await client.post(admin + "/load", json=config(True))
                        response.raise_for_status()
                        await super().close_tts_ingress(slot)

                    async def snapshot(self, slot):
                        self.inflight = admitted
                        return await super().snapshot(slot)

                slots = Slots(old_id)
                post = asyncio.create_task(client.post(f"http://127.0.0.1:{public_port}/api/tts/jobs"))
                await asyncio.wait_for(forwarded.wait(), 3)
                assert admitted == 0
                await asyncio.wait_for(_controller(tmp_path, slots).drain_previous_tts(
                    _job(old_id), timeout=1, poll=0.01), 2)
                assert not post.done() and not checked.is_set()
                upstreams = await client.get(admin + "/reverse_proxy/upstreams")
                upstreams.raise_for_status()
                print("caddy_upstreams_after_gate=" + json.dumps(upstreams.json()))
                refused = await client.post(f"http://127.0.0.1:{public_port}/api/tts/jobs")
                assert refused.status_code == 503
                # The request in the OLD config still reaches the old API after
                # the controller's drain gate already reported success.
                deliver.set()
                await asyncio.wait_for(checked.wait(), 2)
                await activate(new_id, expected=old_id)
                commit.set()
                response = await post
                assert response.status_code == 202
                await _old_guard_pass(old_id)
                status, error, chunks = await _job_row(uuid.UUID(response.json()["job_id"]))
                print(json.dumps({"load_and_gate_returned_before_admission": True,
                                  "new_post": refused.status_code, "old_post": response.status_code,
                                  "status": status, "error": error, "chunks": chunks}))
                assert (status, error) == ("failed", "generation_draining")
        finally:
            deliver.set()
            commit.set()
            if post is not None and not post.done():
                post.cancel()
                await asyncio.gather(post, return_exceptions=True)
            server.close()
            await server.wait_closed()
            await asyncio.gather(*handler_tasks, return_exceptions=True)
            if proc.returncode is None:
                proc.terminate()  # Only this test's own isolated child.
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
