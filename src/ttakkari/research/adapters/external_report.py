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
        elif prov == "external_benchmark":
            raw = config.get("raw_output") if isinstance(config.get("raw_output"), dict) else {}
            env = (
                config.get("environment_snapshot")
                if isinstance(config.get("environment_snapshot"), dict)
                else {}
            )

            title = (
                config.get("source_title")
                or raw.get("source_title")
                or env.get("source_title")
            )
            if not title or not str(title).strip():
                errors.append(
                    "external_benchmark는 최소한 비어있지 않은 출처 제목(source_title)이 필요합니다."
                )

            locator = (
                config.get("source_url")
                or config.get("source_locator")
                or raw.get("source_url")
                or raw.get("source_locator")
                or env.get("source_url")
                or env.get("source_locator")
            )
            if not locator or not str(locator).strip():
                errors.append(
                    "external_benchmark는 명확한 출처 위치(source_url 또는 locator)가 필요합니다."
                )

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

        raw_cfg = config.get("raw_output") if isinstance(config.get("raw_output"), dict) else {}
        env_cfg = (
            config.get("environment_snapshot")
            if isinstance(config.get("environment_snapshot"), dict)
            else {}
        )

        source_title = (
            config.get("source_title")
            or raw_cfg.get("source_title")
            or env_cfg.get("source_title")
        )
        source_url = (
            config.get("source_url")
            or config.get("source_locator")
            or raw_cfg.get("source_url")
            or raw_cfg.get("source_locator")
            or env_cfg.get("source_url")
            or env_cfg.get("source_locator")
        )
        source_year = (
            config.get("source_year")
            or raw_cfg.get("source_year")
            or env_cfg.get("source_year")
        )

        env_snapshot = {
            "execution_origin": "external_report",
            "provenance": config.get("provenance"),
            "source_title": source_title,
            "source_url": source_url,
            "source_year": source_year,
            "imported_at": time.time(),
            "scope": scope,
        }

        raw_out = {
            "execution_origin": "external_report",
            "provenance": config.get("provenance"),
            "source_title": source_title,
            "source_url": source_url,
            "source_year": source_year,
            "notes": config.get("notes") or raw_cfg.get("notes") or raw_cfg.get("note"),
            "measurements": config.get("measurements", []),
        }

        return AdapterRunResult(
            status="succeeded",
            raw_output=raw_out,
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
