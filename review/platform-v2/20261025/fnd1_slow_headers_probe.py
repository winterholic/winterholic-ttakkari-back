"""Real isolated Caddy + old admission model, no database, GPU or production ports.

Exercise a connection whose headers are incomplete across the /load boundary.
Uses the preserved e522187 prototype controller and its real upstream counter.
"""
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import uuid
from pathlib import Path

import httpx

prototype = Path(__file__).resolve().parents[3].parent / "back-platform-v2-fnd1-proto-20261025" / "src"
sys.path.insert(0, str(prototype))
from ttakkari import generation_deployment as gd
from ttakkari.deployment import JobStore


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def probe(root):
    state = {"active": True, "http": 0, "job": None}
    admitted, commit = asyncio.Event(), asyncio.Event()
    handlers = set()
    old_id = uuid.uuid4()

    async def upstream(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            state["http"] += 1
            if not state["active"]:
                response = b"503 Service Unavailable"
            else:
                admitted.set()  # old accept refreshes generation only once
                await commit.wait()
                state["job"] = "queued"
                response = b"202 Accepted"
            writer.write(b"HTTP/1.1 " + response + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            state["http"] = max(0, state["http"] - 1)
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = server.sockets[0].getsockname()[1]
    admin_port, public_port = free_port(), free_port()
    assert len({admin_port, public_port, upstream_port}) == 3
    assert not {admin_port, public_port, upstream_port} & {8787, 8791, 8792, 8793, 20191}
    gd.PORTS = (upstream_port, *gd.PORTS[1:])  # this isolated interpreter only

    def config(closed=False):
        value = gd.proxy_config(0, tts_ingress_closed=closed)
        value["admin"]["listen"] = f"127.0.0.1:{admin_port}"
        value["apps"]["http"]["servers"]["ttakkari"]["listen"] = [f"127.0.0.1:{public_port}"]
        return value

    path = root / "proxy.json"
    path.write_text(json.dumps(config()))
    caddy = shutil.which("caddy")
    if caddy is None:
        raise RuntimeError("Caddy is required for this explicit transport probe")
    writer = None
    proc = None
    try:
        with (root / "caddy.log").open("wb") as log:
            proc = await asyncio.create_subprocess_exec(
                caddy, "run", "--config", str(path), stdout=log, stderr=log,
                env=dict(os.environ, XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data")))
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                admin = f"http://127.0.0.1:{admin_port}"
                for _ in range(50):
                    if proc.returncode is not None:
                        raise RuntimeError((root / "caddy.log").read_text())
                    try:
                        response = await client.get(admin + "/config/")
                        response.raise_for_status()
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.02)
                else:
                    raise RuntimeError("isolated Caddy did not become ready")

                class Slots(gd.Slots):
                    async def close_tts_ingress(self, slot):
                        response = await client.post(admin + "/load", json=config(True))
                        response.raise_for_status()

                    async def switch(self, slot):
                        response = await client.post(admin + "/load", json=config(False))
                        response.raise_for_status()

                    async def snapshot(self, slot):
                        return {"generation": str(old_id), "http_inflight": state["http"]}

                slots = Slots(root, admin_url=admin)
                controller = gd.GenerationController(JobStore(root / "jobs"), slots=slots)

                async def count_jobs(_):
                    return int(state["job"] in {"queued", "running"})

                controller._previous_tts_queued = count_jobs
                reader, writer = await asyncio.open_connection("127.0.0.1", public_port)
                writer.write(b"POST /api/tts/jobs HTTP/1.1\r\nHost: probe\r\nX-Probe: ")
                await writer.drain()
                await asyncio.sleep(0.1)
                await controller.drain_previous_tts(
                    {"previous_slot": 0, "previous_generation": str(old_id)}, timeout=2, poll=0.5)
                zero_snapshot = {"proxy": await slots.proxy_inflight(0), "http": state["http"], "jobs": await count_jobs(old_id)}
                refused = await client.post(f"http://127.0.0.1:{public_port}/api/tts/jobs")
                assert refused.status_code == 503
                writer.write(b"resume\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                try:
                    await asyncio.wait_for(admitted.wait(), 1)
                    state["active"] = False  # activate after old request's active check
                    commit.set()
                    status = (await asyncio.wait_for(reader.readline(), 2)).decode().strip()
                    if state["job"] == "queued":
                        state["job"] = "failed/generation_draining"
                except TimeoutError:
                    status = (await asyncio.wait_for(reader.readline(), 1)).decode().strip()
                return {"caddy": "2.11.7", "prototype": "e522187", "zero_snapshot": zero_snapshot,
                        "new_post_status": refused.status_code, "old_connection_status": status,
                        "old_api_admitted": admitted.is_set(), "modeled_job_status": state["job"]}
    finally:
        commit.set()
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)
        if proc is not None and proc.returncode is None:
            proc.terminate()  # exclusively the child created above
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ttk-fnd1-slow-headers-") as directory:
        print(json.dumps(asyncio.run(probe(Path(directory))), ensure_ascii=False))
