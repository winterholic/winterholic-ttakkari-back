"""External report adapter for importing external benchmarks and manual evaluations."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from ttakkari.research.adapters.base import (
    AdapterEnvironmentStatus,
    AdapterRunResult,
    ExecutionAdapter,
    MeasurementRecord,
    ResourceEstimate,
)

VALID_PROVENANCE = {"external_benchmark", "manual_evaluation", "imported_log"}


class ExternalReportAdapter:
    """Adapter for importing external benchmarks, papers, or manual evaluations."""

    id: str = "external_report"
    version: str = "1.0.0"
    name_ko: str = "외부 벤치마크 및 리포트 반입 어댑터"
    domains: list[str] = ["*"]
    supported_methods: list[str] = ["*"]
    supported_parameters: list[str] = [
        "provenance",
        "source_title",
        "source_url",
        "source_year",
        "external_run_id",
        "notes",
        "measurements",
    ]
    required_inputs: list[str] = ["provenance", "measurements"]

    def check_environment(self, *, scope: str) -> AdapterEnvironmentStatus:
        """External report import is always available regardless of scope or local runtime."""
        return AdapterEnvironmentStatus(
            adapter_id=self.id,
            ready=True,
            execution_mode="manual",
            reason="외부 결과 및 보고서 반입 상시 지원",
            available_models=[],
            details={
                "scope": scope,
                "runtime": "external_report",
                "hardware": "external",
                "supported_provenance": sorted(VALID_PROVENANCE),
            },
        )

    def validate_config(self, config: dict[str, Any], *, scope: str) -> list[str]:
        errors: list[str] = []
        prov = config.get("provenance")
        if not prov:
            errors.append("provenance 필드가 필요합니다 (external_benchmark, manual_evaluation, imported_log 중 하나).")
        elif prov not in VALID_PROVENANCE:
            errors.append(f"지원되지 않는 provenance: '{prov}'. 허용 값: {sorted(VALID_PROVENANCE)}")

        measurements = config.get("measurements")
        if measurements is None:
            errors.append("measurements 필드가 필요합니다.")
        elif not isinstance(measurements, list) or len(measurements) == 0:
            errors.append("measurements는 최소 1개 이상의 측정 항목 리스트여야 합니다.")
        else:
            for i, m in enumerate(measurements):
                if not isinstance(m, dict):
                    errors.append(f"measurements[{i}] 항목은 객체(dict)여야 합니다.")
                    continue
                if "metric_key" not in m:
                    errors.append(f"measurements[{i}]에 metric_key가 누락되었습니다.")
                if "value" not in m:
                    errors.append(f"measurements[{i}]에 value가 누락되었습니다.")
                elif not isinstance(m["value"], (int, float)):
                    errors.append(f"measurements[{i}]의 value는 숫자여야 합니다.")

        return errors

    def estimate_resources(self, config: dict[str, Any]) -> ResourceEstimate:
        return ResourceEstimate(
            estimated_seconds=0,
            estimated_memory_mb=0,
            requires_gpu_lease=False,
            requires_tts_lock=False,
            requires_cpu_lock=False,
        )

    async def execute_run(
        self,
        run_id: UUID,
        config: dict[str, Any],
        *,
        scope: str,
        cancel_event: Any = None,
    ) -> AdapterRunResult:
        errors = self.validate_config(config, scope=scope)
        if errors:
            return AdapterRunResult(
                status="failed",
                raw_output={"errors": errors, "config": config},
                environment_snapshot={"scope": scope, "execution_origin": "external_report"},
                error_code="invalid_config",
                error_message="; ".join(errors),
                wall_seconds=0.0,
            )

        env_snapshot = {
            "execution_origin": "external_report",
            "provenance": config.get("provenance"),
            "source_title": config.get("source_title"),
            "source_url": config.get("source_url"),
            "source_year": config.get("source_year"),
            "imported_at": time.time(),
            "scope": scope,
        }

        return AdapterRunResult(
            status="succeeded",
            raw_output={
                "execution_origin": "external_report",
                "provenance": config.get("provenance"),
                "source_title": config.get("source_title"),
                "source_url": config.get("source_url"),
                "source_year": config.get("source_year"),
                "notes": config.get("notes"),
                "measurements": config.get("measurements", []),
            },
            environment_snapshot=env_snapshot,
            wall_seconds=0.0,
        )

    def to_measurements(
        self, result: AdapterRunResult, config: dict[str, Any]
    ) -> list[MeasurementRecord]:
        if result.status != "succeeded":
            return []

        raw_items = result.raw_output.get("measurements") or config.get("measurements") or []
        records: list[MeasurementRecord] = []
        prov = result.raw_output.get("provenance") or config.get("provenance") or "unknown"

        for item in raw_items:
            key = str(item["metric_key"])
            label = str(item.get("metric_label") or key)
            val = float(item["value"])
            unit = str(item.get("unit") or "")
            direction = str(item.get("direction") or "higher_is_better")
            is_proxy = bool(item.get("is_proxy", False))
            aggregator = str(item.get("aggregator") or "single")
            slice_key = str(item.get("slice_key") or "overall")

            meta = dict(item.get("meta") or {})
            meta["execution_origin"] = "external_report"
            meta["provenance"] = prov

            records.append(
                MeasurementRecord(
                    metric_key=key,
                    metric_label=label,
                    value=val,
                    unit=unit,
                    direction=direction,
                    is_proxy=is_proxy,
                    aggregator=aggregator,
                    slice_key=slice_key,
                    meta=meta,
                )
            )

        return records
