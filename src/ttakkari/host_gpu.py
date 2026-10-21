"""엔진과 무관한 호스트 GPU 시험 경로(ADR 0037): 로컬 llama-server 시험 인스턴스 하나의 시작·상태·벤치·중지.

어떤 엔진의 작업 세션이든 MCP 도구를 거쳐 백엔드에 요청하면, 샌드박스 밖 호스트에서 이 모듈이 실행한다.
- 임의 명령은 받지 않는다. 모델은 허용 목록 별칭, 플래그는 아래 StartRequest 의 고정 필드와 범위만 argv 로 바뀐다.
- 인스턴스는 세대(백엔드 프로세스)와 무관한 분리된 감독자(`python -m ttakkari.host_gpu supervise`)가 소유한다.
  감독자가 flock 으로 한 번에 하나만 보장하고, 최대 실행 시간·스왑 증가 가드를 직접 집행한다(백엔드 세대가 내려가도 유지).
- 운영 Qwen 서버(예약 포트)와 포트를 공유하지 않고, 상태 파일에 기록한 우리 프로세스(pid+시작 시각 일치)만 신호를 보낸다.
- 감독자가 SIGKILL 로 죽어도 서버가 남지 않도록, 서버보다 먼저 경량 watchdog(host_gpu_guard)을 띄워 감독자 생사를 지켜보게 한다.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ttakkari import host_gpu_guard as guard
from ttakkari.host_gpu_guard import ALIAS
from ttakkari.host_resource import LeaseBusy, ResourceLeaseHandle, ResourceLeaseManager

MEASURED_ON = "host_gpu_path"
MEASURED_NOTE = ("Measured on the host GPU path: a llama-server child run by the backend outside any engine sandbox, "
                 "started through the host_gpu tools. Report it as such, with the request settings below.")
EXIT_BUSY = 3
WATCHDOG_READY_SECONDS = 10.0
GIB = 1024 ** 3
_PHASES_LIVE = ("starting", "ready")
_BENCH_LOCK = threading.Lock()


class GpuError(Exception):
    status_code = 400


class Invalid(GpuError):
    status_code = 422


class Busy(GpuError):
    status_code = 409


class Disabled(GpuError):
    status_code = 503


class StartRequest(BaseModel):
    """시험 인스턴스에 허용하는 인자 전부. 여기 없는 llama-server 플래그는 지정할 수 없다."""
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=80)
    gpu_layers: int = Field(default=999, ge=0, le=999, description="--gpu-layers (999 = all layers)")
    cpu_moe: bool = Field(default=False, description="--cpu-moe: keep all MoE expert weights on the CPU")
    n_cpu_moe: int | None = Field(default=None, ge=0, le=256, description="--n-cpu-moe N")
    ctx_size: int = Field(default=8192, ge=512, le=1010000,
                            description="--ctx-size: total context size allocated across all slots (each slot receives ctx_size // parallel)")
    threads: int = Field(default=6, ge=1, le=64, description="--threads and --threads-batch")
    batch_size: int | None = Field(default=None, ge=32, le=8192, description="--batch-size")
    ubatch_size: int | None = Field(default=None, ge=32, le=4096, description="--ubatch-size")
    flash_attn: Literal["on", "off", "auto"] | None = Field(default=None, description="--flash-attn")
    device: Literal["default", "none"] = Field(default="default", description="none = --device none (CPU only)")
    spec_type: Literal["none", "ngram-mod"] = Field(default="none", description="--spec-type (none or ngram-mod)")
    parallel: Literal[1, 2] = Field(default=1, description="--parallel: number of server slots (1 or 2)")
    cache_type_k: Literal["f16", "q8_0", "q4_0"] | None = Field(default=None, description="--cache-type-k")
    cache_type_v: Literal["f16", "q8_0", "q4_0"] | None = Field(default=None, description="--cache-type-v")
    rope_scaling: Literal["none", "yarn"] = Field(default="none", description="yarn uses scale=4 and original context=262144")
    max_seconds: int | None = Field(default=None, ge=60, le=7200,
                                    description="shorter run limit; capped by host_gpu_max_runtime_seconds")

    @field_validator("parallel", mode="before")
    @classmethod
    def _parallel_not_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("parallel must be 1 or 2, not a boolean")  # noqa: TRY004 -- Pydantic requires ValueError for 422
        return value

    @model_validator(mode="after")
    def _consistent(self) -> StartRequest:
        if self.ctx_size > 262144 * self.parallel and self.rope_scaling != "yarn":
            raise ValueError("per-slot contexts above 262144 require rope_scaling=yarn")
        if self.cpu_moe and self.n_cpu_moe is not None:
            raise ValueError("cpu_moe and n_cpu_moe are mutually exclusive")
        if self.device == "none" and self.gpu_layers > 0:
            raise ValueError("device=none cannot offload layers; set gpu_layers=0 or device=default")
        if self.threads > (os.cpu_count() or 64):
            raise ValueError(f"threads must not exceed the {os.cpu_count()} logical cores")
        return self


def build_args(server: Path, model: Path, port: int, req: StartRequest) -> list[str]:
    """argv 는 이 함수만 만든다. 값은 검증된 정수·열거형뿐이라 요청 문자열이 셸이나 플래그로 해석될 길이 없다."""
    args = [str(server), "--model", str(model), "--alias", ALIAS, "--host", "127.0.0.1", "--port", str(port),
            "--parallel", str(req.parallel), "--no-webui", "--metrics",
            "--ctx-size", str(req.ctx_size), "--threads", str(req.threads), "--threads-batch", str(req.threads)]
    if req.device == "none":
        args += ["--device", "none"]
    args += ["--gpu-layers", str(req.gpu_layers)]
    if req.cpu_moe:
        args.append("--cpu-moe")
    if req.n_cpu_moe is not None:
        args += ["--n-cpu-moe", str(req.n_cpu_moe)]
    if req.batch_size is not None:
        args += ["--batch-size", str(req.batch_size)]
    if req.ubatch_size is not None:
        args += ["--ubatch-size", str(req.ubatch_size)]
    if req.flash_attn is not None:
        args += ["--flash-attn", req.flash_attn]
    if req.spec_type != "none":
        args += ["--spec-type", req.spec_type]
    for flag, value in (("--cache-type-k", req.cache_type_k), ("--cache-type-v", req.cache_type_v)):
        if value is not None:
            args += [flag, value]
    if req.rope_scaling == "yarn":
        args += ["--rope-scaling", "yarn", "--rope-scale", "4", "--yarn-orig-ctx", "262144"]
    return args


# ---- 상태 파일(세대와 무관)

def state_dir(base: Path) -> Path:
    return base / "host-gpu"


def _read_state(directory: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(directory: Path, state: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, directory / "state.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_proc_start = guard.proc_start
_same_process = guard.same_process
_cmdline = guard.cmdline
_zombie = guard.zombie
_terminate_group = guard.terminate_group


def _lock_held(directory: Path) -> bool:
    """기존 llama lock 경로를 포함한 공용 호스트 자원 잠금 상태."""
    return ResourceLeaseManager(data_dir=directory.parent).is_locked()


def swap_used_bytes() -> int:
    return int(psutil.swap_memory().used)


def _min_env() -> dict[str, str]:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH")
    return {k: v for k, v in os.environ.items() if k in keep}


def _ports_in_use_by_processes() -> set[int]:
    ports: set[int] = set()
    with contextlib.suppress(psutil.Error, OSError):
        for proc in psutil.process_iter(["cmdline"]):
            cmd = proc.info.get("cmdline") or []
            for i, token in enumerate(cmd[:-1]):
                if token == "--port" and cmd[i + 1].isdigit():
                    ports.add(int(cmd[i + 1]))
    return ports


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(low: int, high: int, reserved: set[int]) -> int:
    taken = reserved | _ports_in_use_by_processes()
    for port in range(low, high + 1):
        if port not in taken and _port_free(port):
            return port
    raise Busy(f"no free test port in {low}-{high}")


def _health(port: int, timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=timeout, trust_env=False)
    except httpx.HTTPError:
        return False
    return r.status_code == 200


_EVIDENCE = re.compile(r"offload|MTL|Metal|GPU|\bdevice\b|ggml_metal|Apple", re.IGNORECASE)


def device_evidence(log: Path, limit: int = 8) -> list[str]:
    """서버 로그에서 어느 장치에 올렸는지 보여 주는 줄만 뽑는다."""
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [ln.strip()[:200] for ln in text.splitlines() if _EVIDENCE.search(ln)]
    return lines[:limit]


# ---- 감독자: 별도 프로세스가 llama-server 하나를 소유하고 가드를 집행한다

class Supervisor:
    def __init__(self, spec: dict[str, Any], *, swap_reader: Callable[[], int] = swap_used_bytes,
                 poll: float = 2.0, max_seconds: float | None = None, health: Callable[[int], bool] = _health) -> None:
        self.spec = spec
        self.req = StartRequest.model_validate(spec["request"])
        self.dir = Path(spec["state_dir"])
        self.swap_reader = swap_reader
        self.poll = poll
        self.max_seconds = float(max_seconds if max_seconds is not None else spec["max_seconds"])
        self.health = health
        self.stop = threading.Event()
        self.reason = "requested"
        self.lease: ResourceLeaseHandle | None = None

    def _acquire(self):
        try:
            self.lease = ResourceLeaseManager(data_dir=self.dir.parent).acquire(
                owner_task="host_gpu",
                owner_attempt=str(self.spec["launch_id"]),
                runtime_instance=f"host-gpu:{self.spec['launch_id']}",
                resource_family="llm",
                claim={"gpu_layers": self.req.gpu_layers, "ctx_size": self.req.ctx_size,
                       "threads": self.req.threads},
                ttl_seconds=max(30, self.poll * 4),
            )
        except LeaseBusy:
            return None
        return self.lease

    def run(self) -> int:
        lease = self._acquire()
        if lease is None:
            return EXIT_BUSY
        server: subprocess.Popen | None = None
        watchdog: subprocess.Popen | None = None
        state: dict[str, Any] = {}
        limit = int(self.spec["swap_limit_bytes"])
        try:
            port = pick_port(int(self.spec["port_min"]), int(self.spec["port_max"]),
                             {int(p) for p in self.spec["reserved_ports"]})
            command = build_args(Path(self.spec["server_path"]), Path(self.spec["model_path"]), port, self.req)
            baseline = self.swap_reader()
            started = time.time()
            # 서버보다 먼저 띄운다: 감독자가 서버 직후 SIGKILL 을 맞아도 watchdog 이 이미 지켜보고 있어야 한다.
            watchdog = self._start_watchdog(command[0], port)
            log = open(self.dir / "server.log", "wb")  # noqa: SIM115
            server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      start_new_session=True, env=_min_env())
            log.close()
            state = {
                "launch_id": self.spec["launch_id"], "phase": "starting",
                "lease_id": lease.lease_id, "lease_generation_token": lease.generation_token,
                "runtime_instance": lease.runtime_instance,
                "supervisor_pid": os.getpid(), "supervisor_start": _proc_start(os.getpid()),
                "watchdog_pid": watchdog.pid, "server_pid": server.pid, "server_start": _proc_start(server.pid),
                "port": port, "model": self.req.model, "request": self.req.model_dump(),
                "command": command, "started_at": started, "deadline_epoch": started + self.max_seconds,
                "baseline_swap_bytes": baseline, "swap_limit_bytes": limit, "peak_swap_growth_bytes": 0,
            }
            _write_state(self.dir, state)
            self._watch(server, state, baseline, limit, time.monotonic())
        except GpuError as exc:
            self.reason = f"start_failed: {exc}"
        except Exception as exc:  # noqa: BLE001 -- 감독자는 어떤 실패에도 서버를 정리하고 사유를 남긴다
            self.reason = f"supervisor_error: {type(exc).__name__}: {exc}"[:200]
        finally:
            if server is not None:
                _terminate_group(server.pid)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    server.wait(timeout=5)
            final = {**state} if state else {"launch_id": self.spec["launch_id"], "request": self.req.model_dump(),
                                             "model": self.req.model, "started_at": time.time(),
                                             "lease_id": lease.lease_id,
                                             "lease_generation_token": lease.generation_token,
                                             "runtime_instance": lease.runtime_instance}
            final.update(phase="stopped", stop_reason=self.reason, stopped_at=time.time(),
                         server_exit_code=None if server is None else server.returncode)
            _write_state(self.dir, final)
            if server is None:
                evidence = {"runtime_instance": lease.runtime_instance, "server_started": False,
                            "supervisor_stop_reason": self.reason}
            else:
                evidence = {"runtime_instance": lease.runtime_instance, "server_pid": server.pid,
                            "server_start": state.get("server_start"), "server_exit_code": server.returncode,
                            "supervisor_stop_reason": self.reason}
            lease.release(proof={"runtime_status": "stopped", "evidence": evidence})
        return 0

    def _start_watchdog(self, server_path: str, port: int) -> subprocess.Popen:
        """감시 실패는 fail-closed: watchdog 이 준비되지 않으면 서버를 띄우지 않는다."""
        ready = self.dir / f"watchdog-{self.spec['launch_id']}.ready"
        ready.unlink(missing_ok=True)
        child = subprocess.Popen(
            [sys.executable, "-m", "ttakkari.host_gpu_guard", "--supervisor-pid", str(os.getpid()),
             "--supervisor-start", repr(_proc_start(os.getpid())), "--server-path", server_path, "--port", str(port),
             "--ready-file", str(ready)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True, env=_min_env())
        threading.Thread(target=child.wait, daemon=True).start()
        deadline = time.monotonic() + WATCHDOG_READY_SECONDS
        while time.monotonic() < deadline:
            if ready.exists():
                ready.unlink(missing_ok=True)
                return child
            if child.poll() is not None:
                break
            time.sleep(0.05)
        guard.terminate_group(child.pid, grace=2.0)
        raise GpuError("watchdog did not start; refusing to run an unguarded server")

    def _watch(self, server: subprocess.Popen, state: dict[str, Any], baseline: int, limit: int, t0: float) -> None:
        failures = 0
        start_timeout = float(self.spec["start_timeout"])
        deadline = time.monotonic() + self.max_seconds
        while not self.stop.wait(self.poll):
            if server.poll() is not None:
                self.reason = f"server_exited:{server.returncode}"
                return
            if self.lease is not None:
                self.lease.heartbeat(ttl_seconds=max(30, self.poll * 4))
            try:
                growth = self.swap_reader() - baseline
                failures = 0
            except Exception:  # noqa: BLE001 -- 감시를 못 하면 계속 돌리지 않는다
                failures += 1
                if failures >= 3:
                    self.reason = "swap_monitor_failed"
                    return
                continue
            if growth > state["peak_swap_growth_bytes"]:
                state["peak_swap_growth_bytes"] = growth
                _write_state(self.dir, state)
            if growth >= limit:
                self.reason = f"swap_guard: swap grew {growth} bytes (limit {limit})"
                return
            if time.monotonic() >= deadline:
                self.reason = "max_runtime"
                return
            if state["phase"] == "starting":
                if self.health(state["port"]):
                    state.update(phase="ready", ready_at=time.time())
                    _write_state(self.dir, state)
                    if self.lease is not None:
                        self.lease.mark_running()
                elif time.monotonic() - t0 > start_timeout:
                    self.reason = "start_timeout"
                    return
        self.reason = "requested"


# ---- 백엔드 쪽 창구

class HostGpu:
    def __init__(self, settings) -> None:
        self.s = settings
        self.dir = state_dir(settings.data_dir)

    # 요청 검증
    def _paths(self, req: StartRequest) -> tuple[Path, Path]:
        if not self.s.host_gpu_enabled:
            raise Disabled("host GPU test path is disabled (TTAKKARI_HOST_GPU_ENABLED=false)")
        models = self.s.host_gpu_models
        if req.model not in models:
            raise Invalid(f"model must be one of the allowlisted aliases: {', '.join(sorted(models)) or '(none)'}")
        model = Path(models[req.model]).expanduser()
        server = Path(self.s.host_gpu_server_path).expanduser()
        if not model.is_file():
            raise Invalid(f"allowlisted model file is missing for alias {req.model}")
        if not server.is_file() or not os.access(server, os.X_OK):
            raise Invalid("llama-server executable is missing or not executable")
        return server, model

    def _reap_lost(self, state: dict[str, Any]) -> dict[str, Any]:
        """감독자가 사라졌는데 상태가 살아 있으면 정리한다. 상태 파일에 적힌 우리 서버 프로세스만 건드린다."""
        lease_id = state.get("lease_id")
        generation_token = state.get("lease_generation_token")
        if not lease_id or not generation_token:
            raise GpuError("host resource lease owner evidence is missing; refusing automatic reconciliation")
        server_pid = state.get("server_pid")
        if server_pid:
            guard.kill_our_server(int(server_pid), state.get("server_start"), state.get("port"))
            if guard.same_process(int(server_pid), state.get("server_start")):
                raise GpuError("owned server is still present; resource lease remains held")
        state = {**state, "phase": "stopped", "stop_reason": "supervisor_lost", "stopped_at": time.time()}
        _write_state(self.dir, state)
        ResourceLeaseManager(data_dir=self.dir.parent).reconcile(
            lease_id,
            generation_token,
            proof={"runtime_status": "stopped", "evidence": {
                "runtime_instance": state.get("runtime_instance"), "server_pid": server_pid,
                "server_start": state.get("server_start"), "supervisor_lost": True,
            }},
        )
        return state

    def _current(self) -> dict[str, Any] | None:
        state = _read_state(self.dir)
        if state and state.get("phase") in _PHASES_LIVE and not _lock_held(self.dir):
            state = self._reap_lost(state)
        return state

    def start(self, req: StartRequest) -> dict[str, Any]:
        server, model = self._paths(req)
        current = self._current()
        if current and current.get("phase") in _PHASES_LIVE or _lock_held(self.dir):
            raise Busy("a host GPU test instance is already running; stop it first")
        cap = int(self.s.host_gpu_max_runtime_seconds)
        launch_id = uuid.uuid4().hex
        spec = {
            "launch_id": launch_id, "request": req.model_dump(), "state_dir": str(self.dir),
            "server_path": str(server), "model_path": str(model),
            "max_seconds": min(req.max_seconds or cap, cap), "start_timeout": self.s.host_gpu_start_timeout_seconds,
            "swap_limit_bytes": int(self.s.host_gpu_swap_growth_gb * GIB),
            "port_min": self.s.host_gpu_port_min, "port_max": self.s.host_gpu_port_max,
            "reserved_ports": sorted({*self.s.host_gpu_reserved_ports, self.s.qwen_flash_monitor_port}),
        }
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        spec_path = self.dir / f"spec-{launch_id}.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        os.chmod(spec_path, 0o600)
        supervisor_log = open(self.dir / "supervisor.log", "ab")  # noqa: SIM115
        child = subprocess.Popen([sys.executable, "-m", "ttakkari.host_gpu", "supervise", "--spec-file", str(spec_path)],
                                 stdout=supervisor_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True, env=_min_env())
        supervisor_log.close()
        threading.Thread(target=child.wait, daemon=True).start()  # 끝난 감독자가 좀비로 남지 않게 거둔다
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            code = child.poll()
            state = _read_state(self.dir)
            if state and state.get("launch_id") == launch_id:
                spec_path.unlink(missing_ok=True)
                return self.status()
            if code is not None:
                spec_path.unlink(missing_ok=True)
                if code == EXIT_BUSY:
                    raise Busy("a host GPU test instance is already running; stop it first")
                raise GpuError(f"supervisor exited {code} before starting the server; see supervisor.log")
            time.sleep(0.1)
        raise GpuError("supervisor did not report a state within 20s")

    def status(self) -> dict[str, Any]:
        state = self._current()
        base: dict[str, Any] = {"execution_path": "host_gpu", "measured_on": MEASURED_ON, "note": MEASURED_NOTE}
        if not state:
            return {**base, "phase": "idle", "running": False}
        now = time.time()
        running = state.get("phase") in _PHASES_LIVE
        out = {**base, "phase": state.get("phase"), "running": running, "model": state.get("model"),
               "request": state.get("request"), "port": state.get("port"), "stop_reason": state.get("stop_reason"),
               "started_at": state.get("started_at"), "stopped_at": state.get("stopped_at"),
               "server_exit_code": state.get("server_exit_code")}
        if state.get("request"):
            req_data = state["request"]
            total_ctx = req_data.get("ctx_size")
            par = req_data.get("parallel", 1) or 1
            if total_ctx is not None:
                out["slot_ctx_size"] = total_ctx // par
                out["context"] = {
                    "total": total_ctx,
                    "per_slot": total_ctx // par,
                    "parallel_slots": par,
                }
        if state.get("started_at"):
            end = now if running else state.get("stopped_at") or now
            out["elapsed_seconds"] = round(end - state["started_at"], 1)
        if running:
            out["remaining_seconds"] = max(0, round(state["deadline_epoch"] - now, 1))
            try:
                current = swap_used_bytes()
            except Exception:  # noqa: BLE001
                current = None
            growth = None if current is None else current - state["baseline_swap_bytes"]
            out["swap"] = {"baseline_bytes": state["baseline_swap_bytes"], "current_bytes": current,
                           "growth_bytes": growth, "limit_bytes": state["swap_limit_bytes"],
                           "peak_growth_bytes": max(state.get("peak_swap_growth_bytes", 0), growth or 0)}
            out["ready"] = state["phase"] == "ready" and _health(state["port"])
        else:
            out["swap"] = {"baseline_bytes": state.get("baseline_swap_bytes"),
                           "limit_bytes": state.get("swap_limit_bytes"),
                           "peak_growth_bytes": state.get("peak_swap_growth_bytes")}
            out["ready"] = False
        out["device_evidence"] = device_evidence(self.dir / "server.log")
        return out

    def bench(self, prompt: str, n_predict: int = 128, runs: int = 3) -> dict[str, Any]:
        state = self._current()
        if not state or state.get("phase") != "ready":
            raise Busy("no ready host GPU test instance; start one and wait until status.ready is true")
        if not _BENCH_LOCK.acquire(blocking=False):
            raise Busy("a benchmark is already running on this instance")
        try:
            port = int(state["port"])
            rows = []
            for i in range(runs):
                t0 = time.monotonic()
                try:
                    r = httpx.post(f"http://127.0.0.1:{port}/completion", trust_env=False, timeout=600, json={
                        "prompt": prompt, "n_predict": n_predict, "temperature": 0, "cache_prompt": False,
                        "stream": False})
                    r.raise_for_status()
                    body = r.json()
                except (httpx.HTTPError, ValueError) as exc:
                    rows.append({"run": i + 1, "error": f"{type(exc).__name__}: {exc}"[:200]})
                    break
                t = body.get("timings") or {}
                rows.append({
                    "run": i + 1, "wall_seconds": round(time.monotonic() - t0, 3),
                    "prompt_tokens": t.get("prompt_n"), "prompt_tokens_per_second": t.get("prompt_per_second"),
                    "generated_tokens": t.get("predicted_n"),
                    "generation_tokens_per_second": t.get("predicted_per_second"),
                    "output_preview": str(body.get("content", ""))[:200],
                })
            speeds = [r["generation_tokens_per_second"] for r in rows if r.get("generation_tokens_per_second")]
            prefill = [r["prompt_tokens_per_second"] for r in rows if r.get("prompt_tokens_per_second")]
            status = self.status()
            return {
                "execution_path": "host_gpu", "measured_on": MEASURED_ON, "note": MEASURED_NOTE,
                "request": state.get("request"), "n_predict": n_predict, "runs": rows,
                "median_generation_tokens_per_second": statistics.median(speeds) if speeds else None,
                "median_prompt_tokens_per_second": statistics.median(prefill) if prefill else None,
                "swap": status.get("swap"), "device_evidence": status.get("device_evidence"),
                "instance_phase": status.get("phase"), "instance_stop_reason": status.get("stop_reason"),
            }
        finally:
            _BENCH_LOCK.release()

    def stop(self) -> dict[str, Any]:
        state = self._current()
        if not state or state.get("phase") not in _PHASES_LIVE:
            return {**self.status(), "stopped_now": False}
        if state.get("lease_id") and state.get("lease_generation_token"):
            ResourceLeaseManager(data_dir=self.dir.parent).request_cancel(
                state["lease_id"], state["lease_generation_token"])
        pid = int(state["supervisor_pid"])
        if not _same_process(pid, state.get("supervisor_start")) or "ttakkari.host_gpu" not in " ".join(_cmdline(pid)):
            raise GpuError("supervisor identity mismatch; refusing to signal an unrelated process")
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if _lock_held(self.dir) is False:
                break
            time.sleep(0.2)
        return {**self.status(), "stopped_now": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ttakkari.host_gpu")
    sub = parser.add_subparsers(dest="command", required=True)
    sup = sub.add_parser("supervise")
    sup.add_argument("--spec-file", required=True)
    args = parser.parse_args(argv)
    spec_path = Path(args.spec_file)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    supervisor = Supervisor(spec)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: supervisor.stop.set())
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
