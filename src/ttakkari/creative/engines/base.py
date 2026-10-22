"""엔진 어댑터 공통 계약.

어댑터는 propose/prepare(소스·graph 작성)와 execute(고정된 프로필 실행)를 분리한다. 임의 shell 문자열은 실행 API로 받지 않고,
등록된 프로필의 검증된 매개변수만 받는다. G0 상태 전이(queued→running→collecting→review_pending)는 DirectorStore가 소유하므로
어댑터는 실행 결과(출력 바이트와 provenance)만 돌려준다. 동기 엔진은 submit·observe·collect를 `execute` 하나로 합친다.

ADR §6.3 생명주기와의 대응: probe=`probe`, validate=`validate`, submit/observe/collect=`execute`,
cancel=`ExecutionContext.cancelled()` 협조와 `cancel`, reconcile=`reconcile`, release=`release`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

import psutil

from ttakkari.creative.director import EmptyOutputError

InstallState = Literal["absent", "present", "probe_failed", "available"]
OutputRole = Literal["primary", "audio", "aux"]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class EngineError(Exception):
    """엔진 실행 실패. `kind`는 receipt와 사용자 화면에 남는 분류다."""

    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable

    def __str__(self) -> str:
        return f"[engine_error:{self.kind}] {super().__str__()}"


class EngineUnavailable(EngineError):
    def __init__(self, message: str) -> None:
        super().__init__("unavailable", message)


class EngineCancelled(EngineError):
    def __init__(
        self, message: str = "취소 요청으로 엔진 실행을 중단했습니다."
    ) -> None:
        super().__init__("cancelled", message)


class EngineEmptyOutput(EmptyOutputError):
    """엔진이 성공했다고 보고했지만 쓸 수 있는 결과가 없다. G0는 이를 error_kind=empty_output으로 기록한다."""


@dataclass(frozen=True)
class ProfileSpec:
    profile_id: str
    capabilities: frozenset[str]
    description: str
    output_kind: Literal["video", "audio", "image"]
    # 소비하는 자원군. gpu는 공용 host resource lease가 있어야 실행할 수 있다.
    resource_family: Literal["cpu", "chrome", "gpu"] = "cpu"
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    needs_assets: bool = False
    has_audio: bool = False
    # 생성형 프로필은 요청한 프레임 수·해상도를 보장하지 않는다. False면 기계 검사가 해당 항목을 unknown으로 남기고 컨폼에서 맞춘다.
    frame_exact: bool = True


@dataclass
class ProbeResult:
    state: InstallState
    environment: str
    runtime_path: str | None
    revision: str | None
    measured_at: str
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "environment": self.environment,
            "runtime_path": self.runtime_path,
            "revision": self.revision,
            "measured_at": self.measured_at,
            "details": self.details,
            "error": self.error,
        }


@dataclass
class EngineOutput:
    name: str
    media_type: str
    data: bytes
    role: OutputRole = "primary"


@dataclass
class EngineRun:
    outputs: list[EngineOutput]
    metrics: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionContext:
    spec: dict[str, Any]
    workdir: Path
    cancelled: Callable[[], bool] = lambda: False
    read_asset: Callable[[str], bytes] = lambda sha: (_ for _ in ()).throw(
        FileNotFoundError(sha)
    )
    emit: Callable[[str], None] = lambda state: None
    timeout_seconds: float = 600.0

    def check_cancel(self) -> None:
        if self.cancelled():
            raise EngineCancelled()


class EngineAdapter(ABC):
    engine_id: str
    display_name: str
    profiles: dict[str, ProfileSpec]
    # "mock"은 CPU 계약 시험용 대역이다. receipt에 그대로 남고 실제 생성 성공 증거로 쓰지 않는다.
    execution_mode: Literal["real", "mock"] = "real"
    rights: ClassVar[dict[str, str]] = {
        "license": "unrecorded",
        "use_scope": "personal",
    }

    @abstractmethod
    def probe(self) -> ProbeResult:
        """실제 도구를 가볍게 호출해 설치·실행 가능 여부를 잰다. 렌더·생성은 하지 않는다."""

    def smoke(self) -> dict[str, Any]:
        """아주 짧은 실제 실행으로 능력을 확인한다. 증거(시간·메모리·출력 요약)를 돌려준다."""
        raise EngineError(
            "unsupported", f"{self.engine_id}는 smoke를 지원하지 않습니다."
        )

    @abstractmethod
    def validate(self, spec: dict[str, Any]) -> list[str]:
        """실행 전 검증. 오류 문자열 목록을 돌려주며 비어 있으면 통과다."""

    @abstractmethod
    def execute(self, ctx: ExecutionContext) -> EngineRun:
        """고정 프로필을 실행해 결과를 수집한다. 취소는 ctx.cancelled()를 주기적으로 확인해 협조한다."""

    def cancel(self, spec: dict[str, Any]) -> None:
        """외부 작업(원격 prompt 등)이 있는 엔진만 덮어쓴다. 소유한 작업만 취소해야 한다."""

    def reconcile(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """응답 유실·재시작 뒤 같은 idempotency_key의 외부 작업이 있는지 대조한다. 없으면 None."""
        return None

    def release(self, ctx: ExecutionContext) -> None:
        """임시 작업 폴더 등 소유 자원을 반납한다."""

    def profile_for(self, spec: dict[str, Any]) -> ProfileSpec:
        profile = self.profiles.get(spec.get("profile", ""))
        if profile is None:
            raise EngineError("unknown_profile", "등록되지 않은 프로필입니다.")
        return profile


def validate_params(
    schema: dict[str, dict[str, Any]], params: dict[str, Any]
) -> list[str]:
    """프로필 매개변수 스키마 검증. 알 수 없는 키·형 불일치·범위 밖 값은 모두 오류다."""
    errors: list[str] = []
    for key in params:
        if key not in schema:
            errors.append(f"알 수 없는 매개변수: {key}")
    for key, rule in schema.items():
        value = params.get(key, rule.get("default"))
        if value is None:
            if rule.get("required"):
                errors.append(f"필수 매개변수가 없습니다: {key}")
            continue
        kind = rule["type"]
        if kind == "str":
            if not isinstance(value, str) or len(value) > rule.get("max_len", 200):
                errors.append(
                    f"{key}는 {rule.get('max_len', 200)}자 이하 문자열이어야 합니다."
                )
            elif rule.get("required") and not value.strip():
                errors.append(f"{key}가 비어 있습니다.")
        elif kind in {"int", "float"}:
            number_type = int if kind == "int" else (int, float)
            if isinstance(value, bool) or not isinstance(value, number_type):
                errors.append(f"{key}는 {kind}이어야 합니다.")
            elif (
                not rule.get("min", float("-inf"))
                <= value
                <= rule.get("max", float("inf"))
            ):
                errors.append(
                    f"{key}는 {rule.get('min')}~{rule.get('max')} 범위여야 합니다."
                )
        elif kind == "enum":
            if value not in rule["values"]:
                errors.append(f"{key}는 {rule['values']} 중 하나여야 합니다.")
        elif kind == "str_list":
            if (
                not isinstance(value, list)
                or len(value) > rule.get("max_items", 20)
                or any(
                    not isinstance(v, str) or len(v) > rule.get("max_len", 80)
                    for v in value
                )
            ):
                errors.append(f"{key}는 문자열 목록이어야 합니다.")
        elif kind == "bool" and not isinstance(value, bool):
            errors.append(f"{key}는 bool이어야 합니다.")
    return errors


def apply_defaults(
    schema: dict[str, dict[str, Any]], params: dict[str, Any]
) -> dict[str, Any]:
    return {
        key: params.get(key, rule.get("default"))
        for key, rule in schema.items()
        if params.get(key, rule.get("default")) is not None
    }


def _tree_rss(proc: psutil.Process) -> int:
    total = 0
    try:
        for p in [proc, *proc.children(recursive=True)]:
            try:
                total += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0
    return total


def run_process(
    argv: list[str],
    *,
    ctx: ExecutionContext,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    log_limit: int = 20_000,
) -> dict[str, Any]:
    """argv 리스트만 실행한다(shell 사용 금지). 취소·시간 초과 시 프로세스 그룹을 종료하고, 자식까지 합친 RSS 최댓값을 표본한다."""
    limit = timeout if timeout is not None else ctx.timeout_seconds
    log_path = ctx.workdir / f".proc-{os.getpid()}-{time.monotonic_ns()}.log"
    started = time.monotonic()
    peak = 0
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        handle = psutil.Process(proc.pid)
        try:
            while True:
                code = proc.poll()
                peak = max(peak, _tree_rss(handle) if code is None else 0)
                if code is not None:
                    break
                if ctx.cancelled():
                    _kill_group(proc)
                    raise EngineCancelled()
                if time.monotonic() - started > limit:
                    _kill_group(proc)
                    raise EngineError(
                        "timeout",
                        f"{limit:.0f}초 안에 끝나지 않았습니다.",
                        retryable=True,
                    )
                time.sleep(0.1)
        except BaseException:
            if proc.poll() is None:
                _kill_group(proc)
            raise
    tail = log_path.read_bytes()[-log_limit:].decode("utf-8", "replace")
    log_path.unlink(missing_ok=True)
    return {
        "returncode": proc.returncode,
        "log_tail": tail,
        "wall_seconds": round(time.monotonic() - started, 3),
        "rss_peak_mb": round(peak / 1_048_576, 1),
    }


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
