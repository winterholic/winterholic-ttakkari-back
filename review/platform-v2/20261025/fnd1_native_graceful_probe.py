"""Own-child-only Caddy shutdown experiment; no operational proxy or DB access.

The upstream models HTTP and a queued job, not a real API/consumer. The body
buffer is an explicit fixture delta used to pause before the upstream counter.
Negative observation windows measure a held condition; they do not prove safety.
"""
import asyncio
import contextlib
import errno
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

import httpx

from ttakkari.generation_deployment import proxy_config


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def run_case(root, *, reloaded=False, grace_ns=0, stop_method="signal"):
    release_sse = asyncio.Event()
    post_entered = asyncio.Event()
    handlers = set()
    state = {"job": None, "post_received": False}
    timeline = []
    started = time.monotonic()

    def record(event, **fields):
        timeline.append({"event": event, "seconds": round(time.monotonic() - started, 4), **fields})

    async def upstream(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            if header.startswith(b"GET /stream "):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                             b"d\r\ndata: start\n\n\r\n")
                await writer.drain()
                await release_sse.wait()
                writer.write(b"b\r\ndata: end\n\n\r\n0\r\n\r\n")
                await writer.drain()
                record("upstream_sse_finished")
            elif header.startswith(b"POST /api/tts/jobs "):
                await reader.readexactly(2)
                state.update(job="queued", post_received=True)
                post_entered.set()
                writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                record("upstream_post_accepted", modeled_job=state["job"])
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = server.sockets[0].getsockname()[1]
    admin_port, public_port = free_port(), free_port()
    ports = {upstream_port, admin_port, public_port}
    assert len(ports) == 3 and not ports & {8787, 8791, 8792, 8793, 20191, 56750}

    def config(closed=False):
        value = proxy_config(0, tts_ingress_closed=closed)
        value["admin"]["listen"] = f"127.0.0.1:{admin_port}"
        app = value["apps"]["http"]
        app["metrics"] = {}
        if grace_ns:
            app["grace_period"] = grace_ns
        srv = app["servers"]["ttakkari"]
        srv["listen"] = [f"127.0.0.1:{public_port}"]
        proxy = srv["routes"][-1]["handle"][0]
        proxy["upstreams"] = [{"dial": f"127.0.0.1:{upstream_port}"}]
        proxy["request_buffers"] = 16
        return value

    root.mkdir()
    cfg = root / "proxy.json"
    cfg.write_text(json.dumps(config()))
    proc, stop_task, exit_task, post_read, stream_read = None, None, None, None, None
    writers = []
    try:
        with (root / "caddy.log").open("wb") as output:
            proc = await asyncio.create_subprocess_exec(
                shutil.which("caddy"), "run", "--config", str(cfg), stdout=output, stderr=output,
                env=dict(os.environ, XDG_CONFIG_HOME=str(root / "config"), XDG_DATA_HOME=str(root / "data")))
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                admin = f"http://127.0.0.1:{admin_port}"
                for _ in range(100):
                    if proc.returncode is not None:
                        raise RuntimeError((root / "caddy.log").read_text())
                    try:
                        (await client.get(admin + "/config/")).raise_for_status()
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.02)
                else:
                    raise RuntimeError("own Caddy readiness timed out")

                sr, sw = await asyncio.open_connection("127.0.0.1", public_port)
                writers.append(sw)
                sw.write(b"GET /stream HTTP/1.1\r\nHost: probe\r\n\r\n")
                await sw.drain()
                sse_header = await asyncio.wait_for(sr.readuntil(b"\r\n\r\n"), 3)
                assert sse_header.startswith(b"HTTP/1.1 200 OK")
                sse_start = await asyncio.wait_for(sr.readuntil(b"data: start\n\n"), 3)
                record("sse_start_delivered")
                stream_read = asyncio.create_task(sr.read())

                pr, pw = await asyncio.open_connection("127.0.0.1", public_port)
                writers.append(pw)
                pw.write(b"POST /api/tts/jobs HTTP/1.1\r\nHost: probe\r\nContent-Length: 2\r\n"
                         b"Expect: 100-continue\r\n\r\n")
                await pw.drain()
                interim = await asyncio.wait_for(pr.readuntil(b"\r\n\r\n"), 3)
                assert interim.startswith(b"HTTP/1.1 100 Continue")
                record("pre_counter_body_pause_ack", upstream_entered=post_entered.is_set())
                assert not post_entered.is_set()
                post_read = asyncio.create_task(pr.read())

                if reloaded:
                    (await client.post(admin + "/load", json=config(True))).raise_for_status()
                    record("reload_returned")

                exit_task = asyncio.create_task(proc.wait())
                if stop_method == "admin":
                    stop_task = asyncio.create_task(client.post(admin + "/stop"))
                else:
                    proc.terminate()  # the subprocess created above only
                record("shutdown_requested", method=stop_method)

                # Observe actual connection failure, not a shutdown-start log.
                connect_error = None
                for _ in range(200):
                    try:
                        with socket.socket() as test_socket:
                            test_socket.setblocking(False)
                            await asyncio.get_running_loop().sock_connect(
                                test_socket, ("127.0.0.1", public_port))
                    except OSError as exc:
                        if exc.errno not in {errno.ECONNREFUSED, errno.ECONNRESET}:
                            raise
                        connect_error = {"type": type(exc).__name__, "errno": exc.errno}
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise RuntimeError("shutdown did not close own listener")
                record("public_connection_unavailable", process_alive=proc.returncode is None,
                       error=connect_error)

                if grace_ns:
                    code = await asyncio.wait_for(asyncio.shield(exit_task), 4)
                    post_result, stream_result = await asyncio.gather(post_read, stream_read)
                    record("deadline_exit", returncode=code, post_202=b"202 Accepted" in post_result,
                           sse_end=b"data: end" in stream_result)
                    assert code == 0 and not state["post_received"]
                    assert b"202 Accepted" not in post_result and b"data: end" not in stream_result
                else:
                    done, _ = await asyncio.wait({exit_task, stream_read}, timeout=0.15,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    assert not done, "exit or SSE EOF before held HTTP work was released"
                    record("held_work_observation", observation_window_seconds=0.15,
                           exit_pending=True, sse_open=True,
                           admin_stop_returned=stop_task.done() if stop_task else None)
                    pw.write(b"{}")
                    await pw.drain()
                    await asyncio.wait_for(post_entered.wait(), 3)
                    post_result = await asyncio.wait_for(asyncio.shield(post_read), 3)
                    assert b"HTTP/1.1 202 Accepted" in post_result
                    record("client_202_delivered", process_alive=proc.returncode is None,
                           modeled_job=state["job"])
                    assert not exit_task.done() and not stream_read.done()
                    release_sse.set()
                    stream_result = await asyncio.wait_for(asyncio.shield(stream_read), 3)
                    assert b"data: end" in stream_result
                    record("client_sse_end_delivered")
                    code = await asyncio.wait_for(asyncio.shield(exit_task), 4)
                    record("process_exit", returncode=code, modeled_job=state["job"])
                    assert code == 0 and state["job"] == "queued"

                if stop_task:
                    try:
                        response = await stop_task
                        record("admin_stop_result", status=response.status_code)
                    except httpx.HTTPError as exc:
                        record("admin_stop_result", error=type(exc).__name__)
                logs = (root / "caddy.log").read_text()
                return {"reloaded": reloaded, "grace_ns": grace_ns, "stop_method": stop_method,
                        "timeline": timeline, "caddy_log": logs,
                        "fixture_delta": "request_buffers=16; metrics; optional finite app grace_period",
                        "http_only_model": True, "production_verified": False,
                        "sse_start_bytes": len(sse_start)}
    finally:
        record("cleanup_started", forced_child_kill=proc is not None and proc.returncode is None)
        release_sse.set()
        for writer in writers:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        if proc is not None and proc.returncode is None:
            proc.kill()  # cleanup of own child, never a successful-drain signal
            await proc.wait()
        for task in [stop_task, exit_task, post_read, stream_read, *handlers]:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in [stop_task, exit_task, post_read, stream_read, *handlers]
                               if task is not None), return_exceptions=True)
        server.close()
        await server.wait_closed()


async def main():
    version = await asyncio.create_subprocess_exec(shutil.which("caddy"), "version",
                                                    stdout=asyncio.subprocess.PIPE)
    stdout, _ = await version.communicate()
    results = []
    with tempfile.TemporaryDirectory(prefix="ttk-native-graceful-") as tmp:
        for index, args in enumerate([
            {}, {"reloaded": True}, {"stop_method": "admin"}, {"grace_ns": 200_000_000},
        ]):
            result = await run_case(Path(tmp) / str(index), **args)
            results.append(result)
            print(json.dumps({"case": index, "timeline": result["timeline"]}), flush=True)
    target = Path(__file__).with_name("fnd1-native-graceful-result.json")
    target.write_text(json.dumps({"version": stdout.decode().strip(), "cases": results},
                                 ensure_ascii=False, indent=2) + "\n")
    print(f"4 cases passed; evidence={target}")


if __name__ == "__main__":
    asyncio.run(main())
