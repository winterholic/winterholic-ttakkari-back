"""Execution Adapter base protocols and data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class AdapterEnvironmentStatus:
    adapter_id: str
    ready: bool
    execution_mode: str  # "in_process" | "subprocess_worker" | "host_supervisor" | "manual"
    reason: str | None = None
    available_models: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResourceEstimate:
    estimated_seconds: int
    estimated_memory_mb: int
    requires_gpu_lease: bool = False
    requires_tts_lock: bool = False
    requires_cpu_lock: bool = False


@dataclass(frozen=True)
class AdapterRunResult:
    status: str  # "succeeded" | "failed" | "cancelled"
    raw_output: dict[str, Any]
    environment_snapshot: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None
    wall_seconds: float = 0.0
    artifacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MeasurementRecord:
    metric_key: str          # 예: "decode_tps", "recall_at_k", "rtf", "cer_normalized"
    metric_label: str        # 한국어 표시명
    value: float
    unit: str                # "tok/s", "ms", "ratio", "bytes", "count"
    direction: str           # "higher_is_better" | "lower_is_better"
    is_proxy: bool = False   # 간접 대리 지표 여부 (예: STT 재인식 CER)
    aggregator: str = "mean" # "single" | "mean" | "median" | "p95" | "std"
    slice_key: str = "overall"
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExecutionAdapter(Protocol):
    id: str
    version: str
    name_ko: str
    domains: list[str]
    supported_methods: list[str]      # method slug 목록 ("*" 또는 prefix 지원)
    supported_parameters: list[str]   # 어댑터가 실제 소비하는 파라미터 key 목록
    required_inputs: list[str]        # 예: ["prompt"] | ["corpus", "queries"] | ["texts"]

    def check_environment(self, *, scope: str) -> AdapterEnvironmentStatus: ...
    def validate_config(self, config: dict[str, Any], *, scope: str) -> list[str]: ...
    def estimate_resources(self, config: dict[str, Any]) -> ResourceEstimate: ...
    async def execute_run(
        self,
        run_id: UUID,
        config: dict[str, Any],
        *,
        scope: str,
        cancel_event: Any,
    ) -> AdapterRunResult: ...
    def to_measurements(self, result: AdapterRunResult, config: dict[str, Any]) -> list[MeasurementRecord]: ...
