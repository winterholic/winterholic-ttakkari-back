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
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ttakkari import host_gpu_guard as guard
from ttakkari.host_gpu_guard import ALIAS
from ttakkari.host_resource import (
    LeaseBusy,
    LeaseError,
    LeaseOwnershipError,
    ResourceLeaseHandle,
    ResourceLeaseManager,
)

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


class LaunchMismatch(GpuError):
    """stop(expected_launch_id=...) found a launch other than the one the caller started."""
    status_code = 409


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
            # GGML INFO backend-placement messages map to verbosity 4 in current llama.cpp.
            # The default 3 leaves device_evidence empty even when model loading succeeds.
            "--parallel", str(req.parallel), "--no-webui", "--metrics", "--log-verbosity", "4",
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
_PLACEMENT_EVIDENCE = re.compile(r"offload|(?:model|KV|compute) buffer size", re.IGNORECASE)


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
    # Initialization can emit many device-capability lines before actual placement.
    # Keep the literal placement reports first, including zero-layer offload reports.
    placement = [line for line in lines if _PLACEMENT_EVIDENCE.search(line)]
    initialization = [line for line in lines if not _PLACEMENT_EVIDENCE.search(line)]
    return (placement + initialization)[:limit]


# 서버 후보의 시작 시각 하한 여유. 시계가 뒤로 감기면 서버 create_time 이 감독자 시작 기록보다 이르게 보일 수 있다.
# 서버는 포트·argv 로 이미 특정되므로 여유를 넉넉히 둬도 오탐 위험이 작다(같은 포트의 옛 서버는 공존할 수 없다).
PENDING_MATCH_MARGIN = 300.0


def _setuid_foreign_binary(exe: object, own_exe: str | None) -> bool:
    """argv 조회가 거부된 이유가 setuid/setgid 라서이고 우리 서버 실행 파일이 아님이 확실한 프로세스(예: /usr/bin/login).
    exe 를 읽지 못했거나 일반 파일이 아니면 False(불완전으로 본다)."""
    if not isinstance(exe, str) or not exe:
        return False
    try:
        mode = os.stat(exe).st_mode
    except OSError:
        return False
    if not stat.S_ISREG(mode) or not mode & (stat.S_ISUID | stat.S_ISGID):
        return False
    return own_exe is None or os.path.realpath(exe) != own_exe


ENUMERATION_ATTEMPTS = 3
ENUMERATION_RETRY_DELAY = 0.1


def _enumerate_strict(match: Callable[[list[str], float], bool], since: float | None,
                      own_exe: str | None = None) -> list[tuple[int, float]] | None:
    """`_enumerate_once` 를 몇 번 다시 시도한다. exec 도중인 프로세스의 argv 가 잠깐 비는 일시적 불완전을 넘기되,
    계속 불완전하면 None 이다(fail-closed)."""
    for attempt in range(ENUMERATION_ATTEMPTS):
        found = _enumerate_once(match, since, own_exe)
        if found is not None:
            return found
        if attempt + 1 < ENUMERATION_ATTEMPTS:
            time.sleep(ENUMERATION_RETRY_DELAY)
    return None


def _enumerate_once(match: Callable[[list[str], float], bool], since: float | None,
                    own_exe: str | None = None) -> list[tuple[int, float]] | None:
    """조건에 맞는 같은 사용자 프로세스 (pid, 시작 시각). 열거가 불완전하면 None(빈 목록은 부재 증거여야 한다).

    다른 사용자 프로세스는 우리 서버일 수 없어 건너뛰고, 종료된 좀비는 살아 있는 서버가 아니다. 같은 사용자 프로세스의
    argv·시작 시각·상태를 읽지 못하면 불완전으로 본다. 다만 다음은 우리 서버일 수 없으므로 건너뛴다.
    - `since`(서버가 뜰 수 있었던 가장 이른 시각)보다 먼저 시작한 프로세스
    - argv 는 못 읽지만 exe 를 읽을 수 있고 그것이 우리 서버가 아닌 setuid/setgid 바이너리인 프로세스(터미널 창마다 하나씩
      있는 /usr/bin/login 이 대표적이다)
    exe 도 읽지 못하는 프로세스는 계속 불완전으로 본다(fail-closed).
    """
    uid = os.getuid()
    found: list[tuple[int, float]] = []
    try:
        for proc in psutil.process_iter(["pid", "cmdline", "create_time", "uids", "status", "exe"]):
            info = proc.info
            uids = info.get("uids")
            if uids is not None and uids.real != uid:
                continue
            if info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            cmd, created = info.get("cmdline"), info.get("create_time")
            if cmd is None or created is None or info.get("status") is None:
                if created is not None and since is not None and created < since - guard.START_SKEW:
                    continue
                if cmd is None and _setuid_foreign_binary(info.get("exe"), own_exe):
                    continue
                return None
            if match(cmd, created):
                found.append((int(info["pid"]), float(created)))
    except (psutil.Error, OSError):
        return None
    return found


def find_our_servers_strict(server_path: str, port: int, since: float) -> list[tuple[int, float]] | None:
    """`guard.find_our_servers` 와 같은 기준의 엄격 열거."""
    return _enumerate_strict(lambda cmd, created: created >= since - PENDING_MATCH_MARGIN
                             and guard._is_our_server(cmd, server_path, port), since,
                             own_exe=os.path.realpath(server_path))


def find_alias_processes_strict(since: float | None) -> list[tuple[int, float]] | None:
    """포트·경로와 무관하게 시험 서버 별칭을 argv 에 가진 같은 사용자 프로세스(수동 복구·고아 lease 확인용)."""
    return _enumerate_strict(lambda cmd, _created: guard.ALIAS in cmd, since)


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
            state = {
                "launch_id": self.spec["launch_id"], "phase": "starting",
                "lease_id": lease.lease_id, "lease_generation_token": lease.generation_token,
                "runtime_instance": lease.runtime_instance,
                "supervisor_pid": os.getpid(), "supervisor_start": _proc_start(os.getpid()),
                "watchdog_pid": watchdog.pid, "server_pid": None, "server_start": None, "server_spawn": "pending",
                "port": port, "model": self.req.model, "request": self.req.model_dump(),
                "command": command, "started_at": started, "deadline_epoch": started + self.max_seconds,
                "baseline_swap_bytes": baseline, "swap_limit_bytes": limit, "peak_swap_growth_bytes": 0,
            }
            # 서버는 이 state 가 durable 해진 뒤에만 띄운다. 감독자가 lease 획득과 이 쓰기 사이에서 죽으면 서버가 없었음이
            # 보장되고, 이 쓰기와 아래 pid 기록 사이에서 죽으면 포트·argv 로 서버를 열거해 정리 여부를 확인한다.
            _write_state(self.dir, state)
            log = open(self.dir / "server.log", "wb")  # noqa: SIM115
            server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      start_new_session=True, env=_min_env())
            log.close()
            state.update(server_pid=server.pid, server_start=_proc_start(server.pid), server_spawn="spawned")
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
            # 우리 자식이므로 poll() 이 종료의 유일한 증거다. 아직 안 끝났으면 lease 를 풀지 않고 live 상태를 남겨
            # HostGpu 가 신원 관찰 후 정리하게 한다(감독자 종료로 flock 만 풀린다).
            server_stopped = server is None or server.poll() is not None
            final = {**state} if state else {"launch_id": self.spec["launch_id"], "request": self.req.model_dump(),
                                             "model": self.req.model, "started_at": time.time(),
                                             "lease_id": lease.lease_id,
                                             "lease_generation_token": lease.generation_token,
                                             "runtime_instance": lease.runtime_instance}
            if server_stopped:
                final.update(phase="stopped", stop_reason=self.reason, stopped_at=time.time(),
                             server_exit_code=None if server is None else server.returncode)
            else:
                final.setdefault("phase", "starting")
                final.update(server_pid=server.pid, stop_reason=self.reason, stop_unconfirmed=True)
            _write_state(self.dir, final)
            if server_stopped:
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

    def _manager(self) -> ResourceLeaseManager:
        return ResourceLeaseManager(data_dir=self.dir.parent)

    def _observe_pending_server(self, state: dict[str, Any]) -> guard.ProcState:
        """pid 를 적기 전에 감독자가 죽은 경우: 포트·argv 로 서버를 열거해 정리하고 부재를 확인한다."""
        command, port = state.get("command"), state.get("port")
        since = state.get("supervisor_start") or state.get("started_at")
        if not command or not port or not since:
            return guard.ProcState.UNKNOWN
        for _ in range(2):  # 1차: 발견한 서버 정리, 2차: 다시 열거해 비어 있는지 확인
            found = find_our_servers_strict(str(command[0]), int(port), float(since))
            if found is None:
                return guard.ProcState.UNKNOWN
            if not found:
                return guard.ProcState.ABSENT
            for pid, start in found:
                guard.kill_our_server(pid, start, port)
        return guard.ProcState.PRESENT

    def _reconcile(self, lease_id: str, generation_token: str, proof: dict[str, Any]) -> None:
        """lease 회수. 실패하면 호출한 쪽 state 는 live 로 남아 다음 조회가 다시 시도한다."""
        manager = self._manager()
        try:
            manager.reconcile(lease_id, generation_token, proof=proof)
        except LeaseOwnershipError:
            if manager.recorded_phase(lease_id, generation_token) != "released":
                raise GpuError("lease owner evidence no longer matches the recorded lease; resource lease "
                               "remains held (see ADR 0037 manual recovery)") from None
            # 앞선 시도에서 lease 는 이미 풀렸고 state 쓰기만 실패했다. state 만 마저 확정한다.
        except (LeaseError, OSError, ValueError) as exc:
            raise GpuError(f"lease reconcile failed; state stays live for retry: {type(exc).__name__}: {exc}") from exc

    def _reap_lost(self, state: dict[str, Any]) -> dict[str, Any]:
        """감독자가 사라졌는데 상태가 살아 있으면 정리한다. 상태 파일에 적힌 우리 서버 프로세스만 건드린다."""
        lease_id = state.get("lease_id")
        generation_token = state.get("lease_generation_token")
        if not lease_id or not generation_token:
            raise GpuError("host resource lease owner evidence is missing; refusing automatic reconciliation "
                           "(see ADR 0037 manual recovery)")
        server_pid, server_start = state.get("server_pid"), state.get("server_start")
        # kill_our_server 의 bool 은 신호를 보냈는지일 뿐 부재 증거가 아니다. 조회 실패·권한 거부·신원 근거 누락(UNKNOWN)을
        # 부재로 보면 살아 있는 서버가 있는데 lease 가 풀려 다음 모델이 겹쳐 올라간다. 부재가 관찰될 때만 해제한다.
        if state.get("server_spawn") == "pending":
            observed = self._observe_pending_server(state)
        else:
            observed = guard.process_state(server_pid, server_start)
            if observed is guard.ProcState.PRESENT:
                guard.kill_our_server(int(server_pid), server_start, state.get("port"))
                observed = guard.process_state(server_pid, server_start)
        if observed is not guard.ProcState.ABSENT:
            raise GpuError(f"owned server stop is not confirmed (observed {observed.value}); "
                           "resource lease remains held")
        # 순서가 중요하다: lease 회수가 성공한 뒤에만 stopped 를 확정한다. 반대면 reconcile 이 실패했을 때 state 는
        # 비활성인데 lease 는 live 라서 다시는 정리 경로를 타지 못하고 acquire 가 영구히 거부된다.
        self._reconcile(lease_id, generation_token, {"runtime_status": "stopped", "evidence": {
            "runtime_instance": state.get("runtime_instance"), "server_pid": server_pid,
            "server_start": server_start, "server_process_state": observed.value,
            "server_spawn": state.get("server_spawn"), "supervisor_lost": True,
        }})
        state = {**state, "phase": "stopped", "stop_reason": "supervisor_lost", "stopped_at": time.time()}
        _write_state(self.dir, state)
        return state

    def _recover_orphan_lease(self, record: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any] | None:
        """lease 는 live 인데 state 가 비활성이거나 다른 launch 의 것이다(감독자가 lease 획득 직후나 stopped 기록 직후에
        죽은 경우). 소유 프로세스가 ABSENT 로 확인될 때만 회수한다. UNKNOWN 이면 보존한다."""
        owner = guard.process_state(record.get("owner_pid"), record.get("owner_start"))
        if owner is not guard.ProcState.ABSENT:
            raise GpuError(f"orphan lease owner stop is not confirmed (observed {owner.value}); resource lease "
                           "remains held (see ADR 0037 manual recovery)")
        matching = state if state and state.get("lease_id") == record["lease_id"] else None
        evidence: dict[str, Any] = {
            "runtime_instance": record["runtime_instance"], "owner_pid": record.get("owner_pid"),
            "owner_start": record.get("owner_start"), "owner_process_state": owner.value, "orphan_lease": True,
        }
        if matching is None:
            # 서버는 state 를 먼저 쓴 뒤에만 띄우지만 그 불변식(state 소실·덮어쓰기 포함)에만 기대지 않고, 별칭 서버가
            # 하나도 없음을 엄격 열거로 확인한 뒤에만 회수한다. 발견한 서버는 여기서 신호하지 않는다(watchdog/수동 정지).
            aliased = find_alias_processes_strict(record.get("created_at"))
            if aliased is None:
                raise GpuError("orphan lease recovery needs a complete process table; enumeration was incomplete, "
                               "resource lease remains held")
            if aliased:
                raise GpuError(f"test-server processes still exist {[pid for pid, _ in aliased]} for an orphan "
                               "lease; resource lease remains held until they stop")
            evidence.update(server_started=False, alias_processes=0)
        else:
            # stopped 는 서버 종료를 관찰한 뒤에만 쓰이지만, 회수 전에 한 번 더 부재를 확인한다.
            pid, start = matching.get("server_pid"), matching.get("server_start")
            server = guard.process_state(pid, start) if pid else guard.ProcState.ABSENT
            if server is not guard.ProcState.ABSENT:
                raise GpuError(f"orphan lease server stop is not confirmed (observed {server.value}); "
                               "resource lease remains held")
            evidence.update(server_pid=pid, server_start=start, server_process_state=server.value,
                            state_phase=matching.get("phase"))
        self._reconcile(record["lease_id"], record["generation_token"],
                        {"runtime_status": "stopped", "evidence": evidence})
        return state

    def _current(self) -> dict[str, Any] | None:
        state = _read_state(self.dir)
        if _lock_held(self.dir):
            return state  # 감독자가 flock 을 쥐고 있다. 살아 있는 소유자의 것은 건드리지 않는다.
        try:
            record = self._manager().current()
        except LeaseError as exc:
            raise GpuError(f"resource lease record is unreadable; refusing automatic recovery: {exc}") from exc
        if state and state.get("phase") in _PHASES_LIVE:
            return self._reap_lost(state)
        if record and record.get("owner_task") == "host_gpu":  # 다른 소비자의 lease 는 그 소비자의 reconcile 몫이다
            return self._recover_orphan_lease(record, state)
        return state

    def _verify_consumer_stopped(self, record: dict[str, Any], *, accept_unrecorded_runtime: bool) -> dict[str, Any]:
        """host_gpu 소유가 아닌 lease 는 별칭 검사만으로는 그 소비자의 런타임이 멈췄는지 알 수 없다. 소비자별 검증기가
        있는 경우(managed Comfy)에만 그 검증을 통과해야 하고, 검증할 수 없는 소비자의 lease 는 거부한다."""
        owner_task = str(record.get("owner_task"))
        if record.get("resource_family") == "comfy" and re.fullmatch(r"[a-f0-9]{32}", owner_task):
            from ttakkari import managed_comfy
            try:
                return managed_comfy.force_release_evidence(
                    self.dir.parent, record, allow_unrecorded_pid=accept_unrecorded_runtime)
            except ValueError as exc:
                raise GpuError(f"managed Comfy runtime is not confirmed stopped: {exc}") from exc
        raise GpuError(f"lease owner {owner_task!r} (family {record.get('resource_family')!r}) has no runtime "
                       "verifier; that consumer must reconcile its own lease, force-release refuses it")

    def force_release(self, *, reason: str, lease_id: str | None = None, accept_unrelated_pid: bool = False,
                      accept_unrecorded_runtime: bool = False) -> dict[str, Any]:
        """자동 복구가 불가능한 상태(legacy state, 서버 pid·start 근거 누락, 우리 서버로 확인되지 않는 프로세스,
        lease 에 소유자 신원 없음)를 운영자가 직접 해제한다. 아무 프로세스에도 신호를 보내지 않으며, 살아 있는
        시험 서버가 없음을 이 함수가 직접 확인한 경우에만 해제한다(ADR 0037 수동 복구). host_gpu 가 아닌 소비자의
        lease 는 그 소비자의 런타임 검증을 통과해야만 해제한다."""
        if not isinstance(reason, str) or not 10 <= len(reason.strip()) <= 300:
            raise GpuError("reason must describe the verification in 10 to 300 characters")
        if _lock_held(self.dir):
            raise GpuError("a live owner still holds the shared host resource lock; nothing was released")
        state = _read_state(self.dir)
        try:
            record = self._manager().current()
        except LeaseError as exc:
            raise GpuError(f"resource lease record is unreadable; refusing forced release: {exc}") from exc
        state_live = bool(state and state.get("phase") in _PHASES_LIVE)
        if not record and not state_live:
            raise GpuError("no live lease or live host_gpu state; nothing to release")
        if record and lease_id != record["lease_id"]:
            raise GpuError("--lease-id must equal the live lease id shown by the status command")
        host_owned = record is None or record.get("owner_task") == "host_gpu"
        consumer: dict[str, Any] = {}
        if record and not host_owned:
            consumer = self._verify_consumer_stopped(record, accept_unrecorded_runtime=accept_unrecorded_runtime)
        references = [value for value in (
            (record or {}).get("created_at"), (state or {}).get("started_at"), (state or {}).get("supervisor_start"))
            if isinstance(value, (int, float)) and not isinstance(value, bool)]
        # 서버는 lease 획득·감독자 시작 뒤에만 뜬다. 가장 이른 기준보다 먼저 시작한 프로세스는 후보가 아니다.
        aliased = find_alias_processes_strict(min(references) if references else None)
        if aliased is None:
            raise GpuError("process table could not be enumerated completely; refusing forced release")
        if aliased:
            raise GpuError(f"live test-server processes exist {[pid for pid, _ in aliased]}; stop them first, "
                           "this command never signals processes")
        recorded: dict[str, str] = {}
        unrelated: list[dict[str, Any]] = []
        host_state = (state or {}) if host_owned else {}  # 다른 소비자의 lease 는 host_gpu state 와 무관하다
        for label, pid, start in (
            ("server", host_state.get("server_pid"), host_state.get("server_start")),
            ("supervisor", host_state.get("supervisor_pid"), host_state.get("supervisor_start")),
            ("lease_owner", (record or {}).get("owner_pid"), (record or {}).get("owner_start")),
        ):
            observed = guard.process_state(pid, start)
            recorded[label] = observed.value
            if not isinstance(pid, int) or isinstance(pid, bool) or not psutil.pid_exists(pid):
                continue
            if observed is guard.ProcState.ABSENT:
                continue
            if label != "server" or not accept_unrelated_pid:
                raise GpuError(f"recorded {label} pid {pid} still exists; refusing forced release")
            unrelated.append({"pid": pid, "cmdline": guard.cmdline(pid)[:6]})
        evidence = {
            "operator_override": True, "reason": reason.strip(), "alias_processes": 0,
            "recorded_process_states": recorded, "unrelated_pid_accepted": unrelated,
            **({"consumer_verification": consumer} if consumer else {}),
        }
        released_lease = None
        if record:  # lease 를 먼저 회수하고 그 뒤에 state 를 확정한다
            self._reconcile(record["lease_id"], record["generation_token"], {
                "runtime_status": "stopped", "evidence": {"runtime_instance": record["runtime_instance"], **evidence}})
            released_lease = record["lease_id"]
        state_live = state_live and host_owned
        if state_live:
            _write_state(self.dir, {**state, "phase": "stopped", "stop_reason": "operator_force_release",
                                    "stopped_at": time.time(), "operator_reason": reason.strip()})
        return {"released_lease": released_lease, "state_stopped": state_live, **evidence}

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

    def stop(self, expected_launch_id: str | None = None) -> dict[str, Any]:
        """Stop the live instance. With expected_launch_id, refuse unless the single state read here is that launch.

        The check and everything after it (lease cancel, supervisor signal) use the same `state`,
        so a caller that started its own launch cannot stop one started by someone else.
        """
        state = self._current()
        if expected_launch_id is not None and (state or {}).get("launch_id") != expected_launch_id:
            raise LaunchMismatch(
                f"current host GPU launch {(state or {}).get('launch_id')!r} is not the expected launch "
                f"{expected_launch_id!r}; refusing to stop it")
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
    force = sub.add_parser("force-release", help="manual recovery when automatic reconciliation refuses (ADR 0037)")
    force.add_argument("--data-dir", type=Path, default=None, help="backend data dir (default: settings.data_dir)")
    force.add_argument("--reason", required=True, help="what was verified (10-300 characters)")
    force.add_argument("--lease-id", default=None, help="id of the live lease; required when a lease is live")
    force.add_argument("--accept-unrelated-pid", action="store_true",
                       help="the recorded server pid is alive but verified NOT to be our test server")
    force.add_argument("--accept-unrecorded-comfy-runtime", action="store_true",
                       help="managed Comfy lease whose runtime pid was never recorded; I verified no Comfy "
                            "runtime process exists")
    force.add_argument("--confirm-no-live-process", action="store_true", required=True,
                       help="I verified no test server process is running for this lease")
    args = parser.parse_args(argv)
    if args.command == "force-release":
        data_dir = args.data_dir
        if data_dir is None:
            from ttakkari.config import get_settings
            data_dir = get_settings().data_dir
        try:
            result = HostGpu(SimpleNamespace(data_dir=Path(data_dir))).force_release(
                reason=args.reason, lease_id=args.lease_id, accept_unrelated_pid=args.accept_unrelated_pid,
                accept_unrecorded_runtime=args.accept_unrecorded_comfy_runtime)
        except GpuError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    spec_path = Path(args.spec_file)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    supervisor = Supervisor(spec)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: supervisor.stop.set())
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
