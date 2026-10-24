"""Isolated real Caddy pre-upstream pause; old API/job behavior is an in-memory model.

Unlike production, this fixture enables request_buffers=16 to make scheduling
before countRequest observable without instrumenting a Caddy binary. Expect:
100-continue acknowledges entry into the body reader. It is not evidence that
production config buffers requests, or that production lost a job.

--baseline loads generation_deployment.py from the exact 1cc48b6 Git object.
No production ports, database, GPU, launchctl, or other processes are used.
"""
import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx

from ttakkari import generation_deployment as current
from ttakkari.deployment import JobStore

BASELINE = "1cc48b6a3c750ada88d0edf8d54a581dc3c96a94"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def probe(root, gd, baseline):
    state = {"active": True, "http": 0, "job": None}
    entered, commit = asyncio.Event(), asyncio.Event()
    handlers = set()
    old_id = uuid.uuid4()

    async def upstream(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            state["http"] += 1
            assert state["active"], "fixture expects the old active check before activation"
            entered.set()
            await commit.wait()
            state["job"] = "queued"
            writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            state["http"] = max(0, state["http"] - 1)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = server.sockets[0].getsockname()[1]
    admin_port, public_port = free_port(), free_port()
    assert len({upstream_port, admin_port, public_port}) == 3
    assert not {upstream_port, admin_port, public_port} & {8787, 8791, 8792, 8793, 20191}

    def config(closed=False):
        value = gd.proxy_config(0, tts_ingress_closed=closed)
        value["admin"]["listen"] = f"127.0.0.1:{admin_port}"
        app = value["apps"]["http"]
        app["metrics"] = {}
        srv = app["servers"]["ttakkari"]
        srv["listen"] = [f"127.0.0.1:{public_port}"]
        proxy = srv["routes"][-1]["handle"][0]
        proxy["upstreams"] = [{"dial": f"127.0.0.1:{upstream_port}"}]
        proxy["request_buffers"] = 16  # controlled pause BEFORE upstream counter
        return value

    path = root / "proxy.json"
    path.write_text(json.dumps(config()))
    proc, writer = None, None
    try:
        with (root / "caddy.log").open("wb") as log:
            proc = await asyncio.create_subprocess_exec(
                shutil.which("caddy"), "run", "--config", str(path), stdout=log, stderr=log,
                env=dict(os.environ, XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data")))
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                admin = f"http://127.0.0.1:{admin_port}"
                for _ in range(80):
                    if proc.returncode is not None:
                        raise RuntimeError((root / "caddy.log").read_text())
                    try:
                        response = await client.get(admin + "/config/")
                        response.raise_for_status()
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.02)  # readiness polling, not safety
                else:
                    raise RuntimeError("isolated Caddy readiness failed")

                async def observe():
                    resp = await client.get(admin + "/reverse_proxy/upstreams")
                    resp.raise_for_status()
                    proxy_count = sum(row["num_requests"] for row in resp.json()
                                      if row["address"] == f"127.0.0.1:{upstream_port}")
                    metric = await client.get(admin + "/metrics")
                    metric.raise_for_status()
                    lines = [line for line in metric.text.splitlines()
                             if line.startswith("caddy_http_requests_in_flight{")]
                    return {"proxy": proxy_count, "http": state["http"],
                            "jobs": int(state["job"] in {"queued", "running"}), "handler_metrics": lines}

                class Slots(gd.Slots):
                    async def close_tts_ingress(self, slot):
                        (await client.post(admin + "/load", json=config(True))).raise_for_status()

                    async def switch(self, slot):
                        (await client.post(admin + "/load", json=config())).raise_for_status()

                    async def snapshot(self, slot):
                        return {"generation": str(old_id), "http_inflight": state["http"]}

                controller = gd.GenerationController(JobStore(root / "jobs"), slots=Slots(root))

                async def count_jobs(_):
                    return int(state["job"] in {"queued", "running"})

                controller._previous_tts_queued = count_jobs
                reader, writer = await asyncio.open_connection("127.0.0.1", public_port)
                writer.write(b"POST /api/tts/jobs HTTP/1.1\r\nHost: probe\r\n"
                             b"Content-Length: 2\r\nExpect: 100-continue\r\n\r\n")
                await writer.drain()
                interim = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                assert interim.startswith(b"HTTP/1.1 100 Continue")
                before = await observe()
                assert before["proxy"] == before["http"] == before["jobs"] == 0
                assert any(line.endswith(" 1") for line in before["handler_metrics"])
                error = None
                try:
                    await controller.drain_previous_tts(
                        {"previous_slot": 0, "previous_generation": str(old_id)}, timeout=2, poll=0)
                    decision = "allowed"
                except gd.StageError as exc:
                    decision, error = "refused", str(exc)
                after = await observe()
                writer.write(b"{}")
                await writer.drain()
                await asyncio.wait_for(entered.wait(), 3)
                if decision == "allowed":
                    state["active"] = False
                commit.set()
                status = (await asyncio.wait_for(reader.readline(), 3)).decode().strip()
                if state["active"]:
                    state["job"] = "succeeded"  # old consumer can still finish it
                else:
                    state["job"] = "failed/generation_draining"  # old guard
                assert status == "HTTP/1.1 202 Accepted"
                assert decision == ("allowed" if baseline else "refused")
                assert state["job"] == ("failed/generation_draining" if baseline else "succeeded")
                return {"mode": "baseline" if baseline else "guarded", "baseline_revision": BASELINE,
                        "fixture_delta": "request_buffers=16; metrics enabled", "interim": interim.decode().strip(),
                        "before_reload": before, "after_decision": after, "decision": decision, "error": error,
                        "old_response": status, "modeled_job": state["job"], "production_loss_verified": False}
    finally:
        commit.set()
        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        if proc is not None and proc.returncode is None:
            proc.terminate()  # only the child created by this probe
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ttk-fnd1-pre-counter-") as directory:
        root = Path(directory)
        gd = current
        if args.baseline:
            path = root / "baseline.py"
            path.write_bytes(subprocess.check_output(["git", "show", f"{BASELINE}:src/ttakkari/generation_deployment.py"]))
            spec = importlib.util.spec_from_file_location("fnd1_baseline", path)
            gd = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gd)
        print(json.dumps(asyncio.run(probe(root, gd, args.baseline)), ensure_ascii=False))
