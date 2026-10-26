"""Disposable model worker supervision. No ML imports or inherited credentials."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
_observations: ContextVar[Callable[[dict], Awaitable[None]] | None] = ContextVar(
    "model_worker_observations", default=None,
)
_memory_limit: ContextVar[WorkerMemoryGuard | None] = ContextVar("model_worker_memory_limit", default=None)


class WorkerMemoryLimitError(RuntimeError):
    """A sampled threshold or an unavailable observation stops owned inference."""


@dataclass
class WorkerMemoryGuard:
    limit_mb: int
    sampled_peak_rss_bytes: int = 0
    samples: int = 0
    error_code: str | None = None

    def snapshot(self) -> dict:
        return {"scope": "each_owned_model_child_rss", "limit_bytes": self.limit_mb * 1048576,
                "sampled_peak_rss_bytes": self.sampled_peak_rss_bytes, "samples": self.samples,
                "poll_seconds": 0.05, "hard_limit": False, "error_code": self.error_code}

    def check(self, process: asyncio.subprocess.Process, *, allow_exit: bool = False) -> bool:
        import psutil  # parent-only; optional ML runtimes import this module for the guard

        if self.error_code:
            raise WorkerMemoryLimitError(self.error_code)
        try:
            rss = _read_worker_rss(process.pid)
        except psutil.NoSuchProcess as exc:
            if allow_exit:
                return False  # exit raced this sample; still require communicate/wait below
            self.error_code = "memory_probe_unavailable"
            raise WorkerMemoryLimitError(self.error_code) from exc
        except (psutil.Error, OSError) as exc:
            self.error_code = "memory_probe_unavailable"
            raise WorkerMemoryLimitError(self.error_code) from exc
        self.samples += 1
        self.sampled_peak_rss_bytes = max(self.sampled_peak_rss_bytes, rss)
        if rss > self.limit_mb * 1048576:
            self.error_code = "memory_budget_exceeded"
            raise WorkerMemoryLimitError(self.error_code)
        return True


def _read_worker_rss(pid: int) -> int:
    import psutil

    # Only a Process handle created and not yet reaped by this supervisor is queried.
    return psutil.Process(pid).memory_info().rss


@contextmanager
def _owner_liveness_pipe():
    """Only the supervisor retains the writer; owner death produces kernel EOF."""
    read_fd, write_fd = os.pipe()
    try:
        yield read_fd
    finally:
        os.close(read_fd)
        os.close(write_fd)


@contextmanager
def worker_memory_limit(limit_mb: int | None) -> Iterator[WorkerMemoryGuard | None]:
    if limit_mb is not None and (type(limit_mb) is not int or limit_mb <= 0):
        raise ValueError("max_memory_mb must be a positive integer")
    guard = WorkerMemoryGuard(limit_mb) if limit_mb is not None else None
    token = _memory_limit.set(guard)
    try:
        yield guard
    finally:
        _memory_limit.reset(token)


async def _communicate_with_memory(process: asyncio.subprocess.Process, payload: bytes,
                                   guard: WorkerMemoryGuard) -> None:
    guard.check(process)  # fail closed before inference receives its request
    communicate = asyncio.create_task(process.communicate(payload))
    try:
        while True:
            done, _ = await asyncio.wait({communicate}, timeout=0.05)
            if done:
                communicate.result()
                return
            if process.returncode is None and not guard.check(process, allow_exit=True):
                await communicate
                return
    finally:
        if not communicate.done():
            communicate.cancel()
        await asyncio.gather(communicate, return_exceptions=True)


@contextmanager
def worker_observations(write: Callable[[dict], Awaitable[None]]) -> Iterator[None]:
    """Optional caller-owned receipts; the model worker has no DB/app dependency."""
    token = _observations.set(write)
    try:
        yield
    finally:
        _observations.reset(token)


async def _observe_worker(process: asyncio.subprocess.Process, identity: dict, *, exited: bool) -> None:
    write = _observations.get()
    if write is not None:
        await write({**identity, "pid": process.pid, "state": "exited" if exited else "running",
                     "returncode": process.returncode if exited else None})


async def _reap_and_observe(process: asyncio.subprocess.Process, identity: dict) -> str | None:
    await _kill_and_wait(process)
    try:
        await _observe_worker(process, identity, exited=True)
    except Exception as exc:
        logger.exception("Model worker exit receipt failed after process wait")
        return type(exc).__name__
    return None


async def _settle_owned_task(task: asyncio.Task):
    """Finish a spawn/reap already owned by us, even after repeated cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
    return task.result()


async def _kill_and_wait(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass  # The child exited between the returncode check and signal.
    await process.wait()


async def run_json_worker(
    *,
    module: str,
    request: dict,
    timeout: float,
    python: Path | None = None,
    error_prefix: str = "model_worker",
) -> dict:
    guard = _memory_limit.get()
    if guard is not None and guard.error_code:
        raise WorkerMemoryLimitError(guard.error_code)
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYANNOTE_METRICS_ENABLED": "0",
            "DO_NOT_TRACK": "1",
            # The optional runtime contains dependencies, while workers use this release code.
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            # Bind before spawn; a reparented child must not adopt a new owner.
            "TTAKKARI_MODEL_PARENT_PID": str(os.getpid()),
        }
    )
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="ttakkari-model-") as temp, _owner_liveness_pipe() as owner_fd:
        env.update(
            {
                "HOME": temp,
                "HF_HOME": str(Path(temp) / "hf"),
                "MPLCONFIGDIR": str(Path(temp) / "mpl"),
                "XDG_CACHE_HOME": str(Path(temp) / "cache"),
                "TORCH_HOME": str(Path(temp) / "torch"),
                "TTAKKARI_MODEL_OWNER_FD": str(owner_fd),
            }
        )
        output = Path(temp) / "result.json"
        identity = {"worker_id": str(uuid.uuid4()), "module": module, "parent_pid": os.getpid()}
        # stderr is a small fixed error code from worker.main; keep it off API output.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            str(python or sys.executable),
            "-m",
            module,
            str(output),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            pass_fds=(owner_fd,),
        ))
        process = None
        result = None
        try:
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError as cancelled:
                try:
                    process = await _settle_owned_task(spawn)
                except Exception:  # noqa: BLE001 -- preserve cancellation after failed spawn
                    raise cancelled from None
                raise
            # No request payload reaches inference until the caller persists ownership.
            await _observe_worker(process, identity, exited=False)
            await asyncio.wait_for(
                (_communicate_with_memory(process, json.dumps(request).encode(), guard)
                 if guard is not None else process.communicate(json.dumps(request).encode())),
                timeout=timeout,
            )
            if process.returncode != 0 or not output.is_file():
                raise RuntimeError(f"{error_prefix}_failed")
            if output.stat().st_size > 64 * 1024 * 1024:
                raise RuntimeError(f"{error_prefix}_output_too_large")
            result = json.loads(output.read_text(encoding="utf-8"))
            result.setdefault("metrics", {}).update(
                {
                    "process_exited": True,
                    "worker_pid": process.pid,
                    "wall_seconds": time.perf_counter() - started,
                }
            )
            return result
        finally:
            if process is not None:
                cleanup = asyncio.create_task(_reap_and_observe(process, identity))
                try:
                    receipt_error = await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await _settle_owned_task(cleanup)
                    raise
                if receipt_error and result is not None:
                    result.setdefault("metrics", {})["lifecycle_receipt_error"] = receipt_error


class ParentGuard:
    """Stop and reap this worker's stdlib-only fallback monitor on normal exit."""

    def __init__(self, process: subprocess.Popen, stop_fd: int, thread_stop: threading.Event):
        self.process, self.stop_fd, self.thread_stop = process, stop_fd, thread_stop

    def set(self) -> None:
        self.thread_stop.set()
        if self.stop_fd is not None:
            os.close(self.stop_fd)
            self.stop_fd = None
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()  # our own Popen handle, never an observed arbitrary PID
            self.process.wait()


def _start_native_parent_guard(owner_fd: int, stop: threading.Event) -> ParentGuard:
    if owner_fd < 3 or not stat.S_ISFIFO(os.fstat(owner_fd).st_mode):
        raise RuntimeError("model_owner_pipe_invalid")
    stop_read, stop_write = os.pipe()
    ready_read, ready_write = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "ttakkari.model_process_guard", str(os.getpid()),
             str(owner_fd), str(stop_read), str(ready_write)],
            pass_fds=(owner_fd, stop_read, ready_write),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(ready_write)
        ready_write = None
        ready, _, _ = select.select([ready_read], [], [], 3)
        if not ready or os.read(ready_read, 1) != b"1" or process.poll() is not None:
            raise RuntimeError("model_owner_guard_unavailable")
        return ParentGuard(process, stop_write, stop)
    except BaseException:
        os.close(stop_write)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
        raise
    finally:
        for fd in (stop_read, ready_read, ready_write):
            if fd is not None:
                os.close(fd)


def start_parent_guard() -> threading.Event | ParentGuard:
    """Reject a lost/malformed supervisor identity before loading any model."""
    raw = os.environ.get("TTAKKARI_MODEL_PARENT_PID")
    try:
        parent_pid = int(raw) if raw is not None else os.getppid()
    except ValueError:
        parent_pid = 1
    if parent_pid <= 1 or os.getppid() != parent_pid:
        os._exit(74)
        raise SystemExit(74)  # reachable only with a replaced exit primitive
    stop = threading.Event()
    raw_fd = os.environ.get("TTAKKARI_MODEL_OWNER_FD")
    guard = _start_native_parent_guard(int(raw_fd), stop) if raw_fd is not None else stop
    threading.Thread(target=watch_parent, args=(parent_pid, stop), daemon=True).start()
    return guard


def watch_parent(parent_pid: int, stop: threading.Event) -> None:
    while not stop.wait(1):
        if os.getppid() != parent_pid or parent_pid <= 1:
            os._exit(74)
            return
