"""LLM inference execution adapter using host GPU and llama-server."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from ttakkari.research.adapters.base import (
    AdapterEnvironmentStatus,
    AdapterRunResult,
    ExecutionAdapter,
    MeasurementRecord,
    ResourceEstimate,
)


class LlmInferenceAdapter:
    """Execution adapter for local LLM inference benchmarking via HostGpu."""

    id: str = "llm_inference"
    version: str = "1.0.0"
    name_ko: str = "로컬 LLM 추론 및 GPU 가속 벤치마크 어댑터"
    domains: list[str] = ["llm"]

    # Explicit whitelist: exactly match methods implemented by this adapter
    supported_methods: list[str] = [
        "kv_cache_quantization",
        "prompt_context_length_tuning",
        "cpu_gpu_offloading",
        "flash_attention",
        "weight_quantization",
        "system_cpu_thread_binding",
        "system_batch_size_throughput_tuning",
    ]

    # Explicit whitelist of parameters (both catalog seed keys and adapter internal keys)
    supported_parameters: list[str] = [
        # Catalog seed parameter keys (llm_inference.json, systems.json)
        "cache_type_k",
        "cache_type_v",
        "ctx_size",
        "batch_size",
        "ubatch_size",
        "n_gpu_layers",
        "main_gpu",
        "flash_attn_enabled",
        "quant_type",
        "quant_method",
        "threads_batch",
        "logical_batch_size",
        "physical_ubatch_size",
        # Adapter internal parameter keys
        "model",
        "gpu_layers",
        "cpu_moe",
        "n_cpu_moe",
        "threads",
        "flash_attn",
        "device",
        "spec_type",
        "parallel",
        "rope_scaling",
        "prompt",
        "n_predict",
        "temperature",
        "repetitions",
        "max_seconds",
    ]
    required_inputs: list[str] = ["model"]

    def check_environment(self, *, scope: str) -> AdapterEnvironmentStatus:
        """Probe real host GPU environment, llama-server binary, and model files."""
        if scope != "personal":
            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=False,
                execution_mode="host_supervisor",
                reason="호스트 GPU 실행은 개인(personal) 스코프 전용입니다 (회사 범위 격리).",
                available_models=[],
                details={"scope": scope, "allowed_scope": "personal"},
            )

        try:
            from ttakkari.config import get_settings
            s = get_settings()
        except Exception as exc:
            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=False,
                execution_mode="host_supervisor",
                reason=f"설정 로드 실패: {exc}",
                available_models=[],
            )

        if not getattr(s, "host_gpu_enabled", False):
            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=False,
                execution_mode="host_supervisor",
                reason="호스트 GPU 기능이 비활성화되어 있습니다 (TTAKKARI_HOST_GPU_ENABLED=false).",
                available_models=[],
            )

        server_path = Path(getattr(s, "host_gpu_server_path", "")).expanduser()
        server_exists = server_path.is_file() and os.access(server_path, os.X_OK)

        models_dict = getattr(s, "host_gpu_models", {}) or {}
        available_models: list[str] = []
        for alias, path_str in models_dict.items():
            p = Path(path_str).expanduser()
            if p.is_file():
                available_models.append(alias)

        ready = server_exists and len(available_models) > 0
        reason = None
        if not server_exists:
            reason = f"llama-server 실행 파일 부재 또는 실행 권한 없음: {server_path}"
        elif not available_models:
            reason = "허용된 모델 별칭 중 로컬에 존재하는 모델 파일이 없습니다."
        else:
            reason = "호스트 GPU 경로 (llama-server) 사용 가능"

        # Check lease status
        lease_held = False
        try:
            from ttakkari.host_gpu import _lock_held, state_dir
            s_dir = state_dir(s.data_dir)
            lease_held = _lock_held(s_dir)
        except Exception:
            pass

        return AdapterEnvironmentStatus(
            adapter_id=self.id,
            ready=ready,
            execution_mode="host_supervisor",
            reason=reason,
            available_models=available_models,
            details={
                "server_path": str(server_path),
                "server_exists": server_exists,
                "allowlisted_aliases": list(models_dict.keys()),
                "available_models": available_models,
                "lease_currently_held": lease_held,
                "scope": scope,
            },
        )

    def validate_config(self, config: dict[str, Any], *, scope: str) -> list[str]:
        errors: list[str] = []

        if scope != "personal":
            errors.append("호스트 GPU 실행은 personal 스코프에서만 허용됩니다.")

        # Check for unsupported seed parameters
        if config.get("draft_model") and config["draft_model"] not in ("none", None):
            errors.append(
                "Draft 모델 기반 투기적 디코딩(speculative_decoding)은 미지원됩니다 (llama-server는 --spec-type ngram-mod만 지원)."
            )
        if "max_num_seqs" in config or "waiting_served_ratio" in config:
            errors.append(
                "연속 배치(continuous_batching)는 llama-server에서 동적 조절이 미지원됩니다 (parallel 슬롯 분할만 가능)."
            )
        if "cache_ttl_seconds" in config:
            errors.append("접두사 캐시 TTL 제어(prefix_caching)는 llama-server에서 미지원됩니다.")
        if "fused_layernorm" in config or "fused_rotary_emb" in config:
            errors.append("커널 퓨전(kernel_fusion)은 컴파일 타임 최적화로 런타임 제어가 미지원됩니다.")

        # Resolve model
        model = config.get("model")
        if not model:
            # Map quant_type to available models if provided
            quant_type = config.get("quant_type")
            if quant_type == "Q4_K_M":
                model = "qwen3.5-4b-q4_k_m"
            elif quant_type in ("Q2_K", "Q2K"):
                model = "qwen3.8-flash-next-q2k"
            else:
                model = "qwen3.5-4b-q4_k_m"

        try:
            from ttakkari.config import get_settings
            s = get_settings()
            models_dict = getattr(s, "host_gpu_models", {}) or {}
            if model not in models_dict:
                errors.append(
                    f"허용되지 않은 모델 별칭: '{model}'. 허용 목록: {sorted(models_dict.keys())}"
                )
        except Exception:
            pass

        # Check gpu_layers / n_gpu_layers
        gpu_layers_val = config.get("n_gpu_layers") if "n_gpu_layers" in config else config.get("gpu_layers")
        if gpu_layers_val is not None and (not isinstance(gpu_layers_val, int) or gpu_layers_val < 0 or gpu_layers_val > 999):
            errors.append("gpu_layers(또는 n_gpu_layers)는 0 이상 999 이하의 정수여야 합니다.")

        ctx_size = config.get("ctx_size")
        if ctx_size is not None and (not isinstance(ctx_size, int) or ctx_size < 512 or ctx_size > 1010000):
            errors.append("ctx_size는 512 이상 1010000 이하의 정수여야 합니다.")

        cache_type_k = config.get("cache_type_k")
        if cache_type_k is not None and cache_type_k not in {"f16", "q8_0", "q4_0"}:
            errors.append(f"지원되지 않는 cache_type_k: '{cache_type_k}'. 허용 값: f16, q8_0, q4_0")

        cache_type_v = config.get("cache_type_v")
        if cache_type_v is not None and cache_type_v not in {"f16", "q8_0", "q4_0"}:
            errors.append(f"지원되지 않는 cache_type_v: '{cache_type_v}'. 허용 값: f16, q8_0, q4_0")

        flash_attn = config.get("flash_attn")
        flash_attn_enabled = config.get("flash_attn_enabled")
        if flash_attn is not None and flash_attn not in {"on", "off", "auto"}:
            errors.append(f"지원되지 않는 flash_attn: '{flash_attn}'. 허용 값: on, off, auto")
        if flash_attn_enabled is not None and not isinstance(flash_attn_enabled, bool):
            errors.append("flash_attn_enabled는 boolean 값이어야 합니다.")

        spec_type = config.get("spec_type")
        if spec_type is not None and spec_type not in {"none", "ngram-mod"}:
            errors.append(f"지원되지 않는 spec_type: '{spec_type}'. 허용 값: none, ngram-mod")

        prompt = config.get("prompt")
        if prompt is not None and len(str(prompt)) > 16000:
            errors.append("prompt 길이는 최대 16000자까지 허용됩니다.")

        return errors

    def estimate_resources(self, config: dict[str, Any]) -> ResourceEstimate:
        return ResourceEstimate(
            estimated_seconds=45,
            estimated_memory_mb=4096,
            requires_gpu_lease=True,
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
                environment_snapshot={"scope": scope},
                error_code="invalid_config",
                error_message="; ".join(errors),
                wall_seconds=0.0,
            )

        from ttakkari.config import get_settings
        from ttakkari.host_gpu import HostGpu, StartRequest

        s = get_settings()
        host_gpu = HostGpu(s)

        # Resolve model
        model = config.get("model")
        if not model:
            quant_type = config.get("quant_type")
            if quant_type == "Q4_K_M":
                model = "qwen3.5-4b-q4_k_m"
            elif quant_type in ("Q2_K", "Q2K"):
                model = "qwen3.8-flash-next-q2k"
            else:
                model = "qwen3.5-4b-q4_k_m"

        # Build start request with parameter mappings
        req_kwargs: dict[str, Any] = {"model": model}

        # Map gpu_layers / n_gpu_layers
        if "n_gpu_layers" in config:
            req_kwargs["gpu_layers"] = int(config["n_gpu_layers"])
        elif "gpu_layers" in config and config["gpu_layers"] is not None:
            req_kwargs["gpu_layers"] = int(config["gpu_layers"])

        # Map flash attention
        if "flash_attn_enabled" in config:
            req_kwargs["flash_attn"] = "on" if config["flash_attn_enabled"] else "off"
        elif "flash_attn" in config and config["flash_attn"] is not None:
            req_kwargs["flash_attn"] = config["flash_attn"]

        # Map threads
        if "threads_batch" in config:
            req_kwargs["threads"] = int(config["threads_batch"])
        elif "threads" in config and config["threads"] is not None:
            req_kwargs["threads"] = int(config["threads"])

        # Map batch sizes
        if "logical_batch_size" in config:
            req_kwargs["batch_size"] = int(config["logical_batch_size"])
        elif "batch_size" in config and config["batch_size"] is not None:
            req_kwargs["batch_size"] = int(config["batch_size"])

        if "physical_ubatch_size" in config:
            req_kwargs["ubatch_size"] = int(config["physical_ubatch_size"])
        elif "ubatch_size" in config and config["ubatch_size"] is not None:
            req_kwargs["ubatch_size"] = int(config["ubatch_size"])

        for key in [
            "cpu_moe", "n_cpu_moe", "ctx_size", "device", "spec_type",
            "parallel", "cache_type_k", "cache_type_v", "rope_scaling", "max_seconds"
        ]:
            if key in config and config[key] is not None:
                req_kwargs[key] = config[key]

        start_req = StartRequest(**req_kwargs)
        bench_prompt = str(config.get("prompt") or "한국어 AI 모델의 성능을 측정하는 표준 벤치마크 테스트 문장입니다.")
        n_predict = int(config.get("n_predict", 128))
        repetitions = int(config.get("repetitions", 2))

        started_wall = time.monotonic()
        server_started = False

        try:
            if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
                return AdapterRunResult(
                    status="cancelled",
                    raw_output={"reason": "cancelled_before_server_start"},
                    environment_snapshot={"scope": scope},
                    wall_seconds=time.monotonic() - started_wall,
                )

            await asyncio.to_thread(host_gpu.start, start_req)
            server_started = True

            deadline = time.monotonic() + 45.0
            ready = False
            while time.monotonic() < deadline:
                if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
                    return AdapterRunResult(
                        status="cancelled",
                        raw_output={"reason": "cancelled_while_waiting_readiness"},
                        environment_snapshot={"scope": scope},
                        wall_seconds=time.monotonic() - started_wall,
                    )
                st = await asyncio.to_thread(host_gpu.status)
                if st.get("ready"):
                    ready = True
                    break
                await asyncio.sleep(0.5)

            if not ready:
                return AdapterRunResult(
                    status="failed",
                    raw_output={"status": host_gpu.status()},
                    environment_snapshot={"scope": scope},
                    error_code="server_not_ready",
                    error_message="llama-server가 45초 내에 준비 상태(ready)가 되지 못했습니다.",
                    wall_seconds=time.monotonic() - started_wall,
                )

            if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
                return AdapterRunResult(
                    status="cancelled",
                    raw_output={"reason": "cancelled_before_bench"},
                    environment_snapshot={"scope": scope},
                    wall_seconds=time.monotonic() - started_wall,
                )

            bench_result = await asyncio.to_thread(
                host_gpu.bench,
                prompt=bench_prompt,
                n_predict=n_predict,
                runs=repetitions,
            )

            total_wall = time.monotonic() - started_wall
            return AdapterRunResult(
                status="succeeded",
                raw_output=bench_result,
                environment_snapshot={
                    "scope": scope,
                    "model": model,
                    "execution_path": "host_gpu",
                    "measured_on": "host GPU path outside sandbox",
                },
                wall_seconds=round(total_wall, 3),
            )

        except Exception as exc:
            total_wall = time.monotonic() - started_wall
            return AdapterRunResult(
                status="failed",
                raw_output={"error": str(exc), "config": config},
                environment_snapshot={"scope": scope},
                error_code=type(exc).__name__,
                error_message=str(exc),
                wall_seconds=round(total_wall, 3),
            )

        finally:
            if server_started:
                try:
                    await asyncio.to_thread(
                        host_gpu.stop, reason=f"research_run_{run_id}_complete"
                    )
                except Exception:
                    pass

    def to_measurements(
        self, result: AdapterRunResult, config: dict[str, Any]
    ) -> list[MeasurementRecord]:
        if result.status != "succeeded":
            return []

        raw = result.raw_output
        records: list[MeasurementRecord] = []

        decode_tps = raw.get("median_generation_tokens_per_second")
        if decode_tps is not None:
            records.append(
                MeasurementRecord(
                    metric_key="decode_tps",
                    metric_label="디코딩 처리량 (Decode tok/s)",
                    value=float(decode_tps),
                    unit="tok/s",
                    direction="higher_is_better",
                    aggregator="median",
                )
            )

        prefill_tps = raw.get("median_prompt_tokens_per_second")
        if prefill_tps is not None:
            records.append(
                MeasurementRecord(
                    metric_key="prefill_tps",
                    metric_label="프리필 처리량 (Prefill tok/s)",
                    value=float(prefill_tps),
                    unit="tok/s",
                    direction="higher_is_better",
                    aggregator="median",
                )
            )

        runs = raw.get("runs") or []
        first_run = runs[0] if runs else {}
        p_tokens = first_run.get("prompt_tokens")
        if p_tokens and prefill_tps and float(prefill_tps) > 0:
            approx_ms = (float(p_tokens) / float(prefill_tps)) * 1000.0
            records.append(
                MeasurementRecord(
                    metric_key="approx_prefill_ms",
                    metric_label="근사 Prefill 시간 (대리 지표)",
                    value=round(approx_ms, 2),
                    unit="ms",
                    direction="lower_is_better",
                    is_proxy=True,
                    aggregator="mean",
                    meta={
                        "explanation": "비스트리밍 호출 응답의 timings에서 역산한 근사 Prefill 지연 시간 (순수 TTFT 아님)",
                        "prompt_tokens": p_tokens,
                    },
                )
            )

        wall = raw.get("wall_seconds") or result.wall_seconds
        records.append(
            MeasurementRecord(
                metric_key="wall_seconds",
                metric_label="총 벤치마크 소요 시간",
                value=float(wall),
                unit="s",
                direction="lower_is_better",
                aggregator="single",
            )
        )

        status = raw.get("status") or {}
        swap = status.get("swap") or {}
        peak_bytes = swap.get("peak_growth_bytes")
        if peak_bytes is not None:
            records.append(
                MeasurementRecord(
                    metric_key="swap_peak_growth_mb",
                    metric_label="스왑 증가량 피크 (호스트 메모리)",
                    value=round(float(peak_bytes) / 1048576.0, 2),
                    unit="MB",
                    direction="lower_is_better",
                    aggregator="single",
                    meta={"explanation": "Metal VRAM 전용 피크는 측정 불가하여 시스템 스왑 증가량 계측"},
                )
            )

        return records
