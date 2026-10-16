"""Own a loopback llama-server child for this backend generation only."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from ttakkari.config import get_settings

_process: asyncio.subprocess.Process | None = None
_url: str | None = None
_state = "not_started"
_detail: str | None = None
_lock = asyncio.Lock()


def _loopback(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and parts.hostname in {"127.0.0.1", "localhost", "::1"}


def _port_available(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _generation_port() -> int:
    from ttakkari import generation
    runtime = generation.current
    if runtime is None:
        for port in range(18081, 18181):
            if _port_available(port):
                return port
        raise RuntimeError("loopback ports unavailable")
    api_port = urlsplit(runtime.api_url).port
    if api_port not in {8791, 8792, 8793}:
        raise RuntimeError("generation has no reserved slot port")
    port = 18081 + api_port - 8791
    if not _port_available(port):
        raise RuntimeError("generation Qwen port is already occupied")
    return port


def _replace_port(url: str, port: int) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, f"127.0.0.1:{port}", "", "", ""))


async def _healthy(url: str, timeout: float = 1.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url.rstrip("/") + "/health")
            return response.is_success and response.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


async def ensure_running() -> str:
    """Return a healthy Qwen origin. Never spawn for remote endpoints or own external processes."""
    global _process, _url, _state, _detail
    s = get_settings()
    configured = s.desk_qwen_base_url.rstrip("/")
    async with _lock:
        if not s.desk_qwen_enabled:
            _state, _detail = "disabled", None
            return configured
        if _process is not None and _process.returncode is not None:
            _process = None
            _url = None
        if _process is not None and _url and await _healthy(_url):
            _state, _detail = "running", None
            return _url
        if await _healthy(configured):
            _url = configured
            _state, _detail = "external", None
            return configured
        if not _loopback(configured):
            _state, _detail = "unavailable", "remote endpoint unhealthy"
            raise RuntimeError(_detail)
        if not s.desk_qwen_server_enabled:
            _state, _detail = "unavailable", "local server auto-start disabled"
            raise RuntimeError(_detail)

        binary = Path(s.desk_qwen_server_path).expanduser().resolve()
        model = Path(s.desk_qwen_model_path).expanduser().resolve()
        if not binary.is_file() or not model.is_file():
            _state, _detail = "unavailable", "configured llama-server or model file missing"
            raise RuntimeError(_detail)
        port = _generation_port()
        origin = _replace_port(configured, port)
        argv = [str(binary), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
                "--device", "none", "--gpu-layers", "0", "--threads", str(s.desk_qwen_server_threads),
                "--parallel", "1", "--ctx-size", str(s.desk_qwen_server_context), "--jinja"]
        _state, _detail = "starting", None
        try:
            _process = await asyncio.create_subprocess_exec(
                *argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": str(Path.home()), "TMPDIR": os.environ.get("TMPDIR", "/tmp")},
            )
        except OSError as exc:
            _state, _detail = "unavailable", type(exc).__name__
            raise RuntimeError(_detail) from exc
        _url = origin
        deadline = asyncio.get_running_loop().time() + s.desk_qwen_server_startup_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if _process.returncode is not None:
                _state, _detail = "unavailable", f"llama-server exited ({_process.returncode})"
                _process = None
                _url = None
                raise RuntimeError(_detail)
            if await _healthy(origin, timeout=min(0.5, max(0.1, deadline - asyncio.get_running_loop().time()))):
                _state, _detail = "running", None
                return origin
            await asyncio.sleep(min(0.25, max(0.0, deadline - asyncio.get_running_loop().time())))
        _state, _detail = "unavailable", "llama-server health timeout"
        if _process is not None:
            if _process.poll() is None:
                _process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(_process.wait), timeout=5)
                except TimeoutError:
                    _process.kill()
                    await asyncio.to_thread(_process.wait)
            _process = None
            _url = None
        raise RuntimeError(_detail)


async def shutdown() -> None:
    global _process, _url, _state, _detail
    async with _lock:
        process = _process
        if process is not None:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            _process = None
        _url = None
        _state = "not_started" if get_settings().desk_qwen_enabled else "disabled"
        _detail = None


def status() -> dict:
    return {"enabled": get_settings().desk_qwen_enabled, "status": _state, "detail": _detail}
