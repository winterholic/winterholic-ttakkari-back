"""접수용 로컬 Qwen 서버의 수명주기. 백엔드(세대) 프로세스가 llama-server 자식 프로세스 하나를 소유하고 관리한다.

OS 서비스(LaunchAgent)를 등록하지 않는다. 앱의 lifespan 이 일반 자식 프로세스로 띄우고 끝낸다.
- 소유자 하나: 자식은 띄운 세대 프로세스만 죽인다. 세대마다 자기 loopback 포트(빈 포트를 골라 127.0.0.1 에만 바인드)와 자기 자식을 가져,
  무중단 전환 중 이전 세대가 끝나도 새 세대의 모델은 건드리지 않는다. 요청은 활성 세대가 자기 자식으로 보낸다.
- 이미 떠 있는 서버를 쓰려면 desk_qwen_base_url 을 지정한다(외부 모드). 이때는 health 만 확인하고 띄우지도 죽이지도 않는다.
  사용자가 지정한 원격 주소에는 로컬 모델을 켜지 않는다.
- 고아 방지: 소유자 프로세스 정보를 pid 파일(home/qwen)에 남기고, 시작할 때 소유자가 죽은 기록의 자식만 정리한다.
  명령줄(서버 경로·포트)과 시작 시각이 기록과 맞을 때만 죽인다(pid 재사용으로 남의 프로세스를 죽이지 않는다).
- 준비 전·실패 중에는 available() 이 False 라 접수는 Luna/Haiku 로 간다. 자식이 죽거나 health 가 연속 실패하면 백오프로 다시 띄운다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import psutil

from ttakkari.config import get_settings

log = logging.getLogger(__name__)

HEALTH_INTERVAL = 10.0
HEALTH_FAILURES = 3
BACKOFF_START = 5.0
BACKOFF_MAX = 300.0
SPAWN_PORT_TRIES = 3
TERMINATE_GRACE = 5.0
GIB = 1024 ** 3


def _state_dir() -> Path:
    d = get_settings().home / "qwen"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _proc_start(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def _same_process(pid: int, start: float | None) -> bool:
    """pid 가 그 시각에 시작된 같은 프로세스인가(pid 재사용 방지)."""
    cur = _proc_start(pid)
    return cur is not None and start is not None and abs(cur - start) < 1.5


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def _child_env() -> dict[str, str]:
    """자식에게 비밀을 넘기지 않는다: 실행에 필요한 최소 환경만."""
    return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}


def _server_args(server: Path, model: Path, port: int) -> list[str]:
    s = get_settings()
    return [str(server), "--model", str(model), "--device", "none", "--gpu-layers", "0",
            "--threads", str(s.desk_qwen_threads), "--threads-batch", str(s.desk_qwen_threads),
            "--ctx-size", str(s.desk_qwen_ctx), "--parallel", "1",
            "--host", "127.0.0.1", "--port", str(port), "--jinja", "--no-webui"]


def _terminate(pid: int, grace: float = TERMINATE_GRACE) -> None:
    """우리가 소유한 자식(새 세션 리더)을 그룹째 정리한다. SIGTERM → 유예 → SIGKILL."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)
        deadline = time.monotonic() + (grace if sig == signal.SIGTERM else 2.0)
        while time.monotonic() < deadline:
            if not psutil.pid_exists(pid) or _is_zombie(pid):
                return
            time.sleep(0.05)


def _is_zombie(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.Error:
        return True


def reap_orphans(server: Path) -> list[int]:
    """소유자가 죽은 기록의 자식만 정리한다. 소유자가 살아 있는(다른 세대) 기록과 명령줄이 다른 프로세스는 건드리지 않는다."""
    reaped: list[int] = []
    for f in sorted(_state_dir().glob("*.json")):
        try:
            rec = json.loads(f.read_text())
            owner, child, port = int(rec["owner_pid"]), int(rec["child_pid"]), int(rec["port"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if _same_process(owner, rec.get("owner_start")):
            continue  # 살아 있는 소유자(예: 이전/다음 세대)의 자식
        if _same_process(child, rec.get("child_start")):
            try:
                cmd = psutil.Process(child).cmdline()
            except psutil.Error:
                cmd = []
            if str(server) in cmd and "--port" in cmd and str(port) in cmd:
                log.warning("reaping orphan qwen server pid=%s port=%s (owner %s gone)", child, port, owner)
                _terminate(child)
                reaped.append(child)
            else:
                log.warning("pid file %s points at a different process; not touching pid %s", f.name, child)
        with contextlib.suppress(OSError):
            f.unlink()
    return reaped


async def probe(base: str, timeout: float = 2.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            r = await c.get(base.rstrip("/") + "/health")
        return r.status_code == 200 and r.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError):
        return False


class QwenServer:
    """이 프로세스의 Qwen 서버 하나. start/stop 은 여러 번 불러도 안전하다."""

    def __init__(self) -> None:
        self.state = "stopped"  # stopped | disabled | external | starting | ready | failed
        self.reason = ""
        self._base: str | None = None
        self._child: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._pidfile: Path | None = None
        self._log: Any = None
        self.spawn_count = 0
        self.external_ready: bool | None = None
        self._token = uuid.uuid4().hex[:8]  # 같은 프로세스의 인스턴스(시험)끼리 pid 파일이 겹치지 않게

    # ---- 조회
    @property
    def endpoint(self) -> str | None:
        return self._base if self.state == "ready" else None

    def status(self) -> dict[str, Any]:
        s = get_settings()
        mode = "external" if s.desk_qwen_base_url else ("managed" if s.desk_qwen_manage else "off")
        return {"enabled": bool(s.desk_qwen_enabled), "mode": mode, "state": self.state, "reason": self.reason or None,
                "ready": self.state in ("ready", "external") and self.external_ready is not False}

    # ---- 수명주기
    async def start(self) -> None:
        s = get_settings()
        if self._task is not None and not self._task.done():
            return  # 중복 spawn 방지: 이미 돌고 있다
        if not s.desk_qwen_enabled:
            self.state, self.reason = "disabled", "desk_qwen_enabled=false"
            return
        if s.desk_qwen_base_url:
            self.state = "external"
            self._task = asyncio.create_task(self._watch_external(s.desk_qwen_base_url), name="qwen-external")
        elif s.desk_qwen_manage:
            self.state = "starting"
            self._task = asyncio.create_task(self._run_managed(), name="qwen-managed")
        else:
            self.state, self.reason = "disabled", "no base url and manage=false"

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._kill_child()
        self.state = "stopped" if self.state != "disabled" else "disabled"
        self._base = None
        self.external_ready = None

    # ---- 외부 서버: health 만 본다(소유하지 않는다)
    async def _watch_external(self, base: str) -> None:
        while True:
            self.external_ready = await probe(base)
            await asyncio.sleep(HEALTH_INTERVAL if self.external_ready else 3.0)

    # ---- 관리 서버
    async def _run_managed(self) -> None:
        s = get_settings()
        server, model = _expand(s.desk_qwen_server_path), _expand(s.desk_qwen_model_path)
        with contextlib.suppress(Exception):
            reaped = await asyncio.to_thread(reap_orphans, server)
            if reaped:
                log.warning("reaped %d orphan qwen servers", len(reaped))
        backoff = BACKOFF_START
        while True:
            try:
                if await self._launch(server, model):
                    backoff = BACKOFF_START
                    await self._supervise()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reason = f"{type(exc).__name__}: {str(exc)[:160]}"
                log.exception("qwen server management failed")
            await self._kill_child()
            self.state = "failed"
            self._base = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _preflight(self, server: Path, model: Path) -> str | None:
        s = get_settings()
        if not server.is_file() or not os.access(server, os.X_OK):
            return f"server binary missing: {server}"
        if not model.is_file():
            return f"model missing: {model}"
        need = max(model.stat().st_size * 1.5, s.desk_qwen_min_free_memory_gb * GIB)
        free = psutil.virtual_memory().available
        if free < need:
            return f"insufficient memory: free {free / GIB:.1f} GiB < {need / GIB:.1f} GiB"
        return None

    async def _launch(self, server: Path, model: Path) -> bool:
        s = get_settings()
        problem = self._preflight(server, model)
        if problem:
            self.state, self.reason = "failed", problem
            log.warning("qwen server not started: %s", problem)
            return False
        self.state, self.reason = "starting", ""
        for _ in range(SPAWN_PORT_TRIES):
            port = _pick_port()
            await self._spawn(server, model, port)
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + s.desk_qwen_start_timeout_seconds
            while time.monotonic() < deadline:
                if self._child is None or self._child.returncode is not None:
                    break  # 포트 충돌 등으로 일찍 죽음 → 다른 포트로 다시
                if await probe(base, timeout=1.5):
                    await self._warm(base)
                    self._base, self.state = base, "ready"
                    log.info("qwen server ready on %s (pid %s)", base, self._child.pid)
                    return True
                await asyncio.sleep(0.5)
            else:
                self.reason = "start timeout"
                return False
            self.reason = f"child exited early (code {self._child.returncode if self._child else '?'}) on port {port}"
            await self._kill_child()
        return False

    async def _warm(self, base: str) -> None:
        """준비 표시 전에 한 번 데운다. 실패해도 준비는 막지 않는다(첫 요청이 조금 느려질 뿐)."""
        from ttakkari.runs import desk_qwen

        try:
            await asyncio.wait_for(desk_qwen.warmup(base), 90.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("qwen warm-up failed (continuing): %s", exc)

    async def _spawn(self, server: Path, model: Path, port: int) -> None:
        d = _state_dir()
        logfile = d / f"server-{os.getpid()}-{self._token}.log"
        self._log = logfile.open("ab")
        args = _server_args(server, model, port)
        self._child = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=self._log, stderr=asyncio.subprocess.STDOUT,
            env=_child_env(), cwd=str(d), start_new_session=True)
        self.spawn_count += 1
        self._pidfile = d / f"{os.getpid()}-{self._token}.json"
        rec = {"owner_pid": os.getpid(), "owner_start": _proc_start(os.getpid()), "child_pid": self._child.pid,
               "child_start": _proc_start(self._child.pid), "port": port, "server": str(server), "model": str(model),
               "started_at": time.time()}
        tmp = self._pidfile.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec))
        tmp.replace(self._pidfile)

    async def _supervise(self) -> None:
        """준비된 서버를 지켜본다. 자식이 죽거나 health 가 연속 실패하면 돌아간다(호출한 쪽이 다시 띄운다)."""
        failures = 0
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            if self._child is None or self._child.returncode is not None:
                self.reason = "child exited"
                return
            if await probe(self._base or ""):
                failures = 0
                self.state = "ready"
            else:
                failures += 1
                if failures >= HEALTH_FAILURES:
                    self.reason = "health check failed"
                    return

    async def _kill_child(self) -> None:
        child, self._child = self._child, None
        self._base = None
        if child is not None and child.returncode is None:
            await asyncio.to_thread(_terminate, child.pid)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(child.wait(), 3.0)
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log.close()
            self._log = None
        if self._pidfile is not None:
            with contextlib.suppress(OSError):
                self._pidfile.unlink()
            self._pidfile = None


service = QwenServer()
