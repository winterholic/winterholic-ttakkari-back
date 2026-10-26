"""LLM inference execution adapter using host GPU and llama-server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

from ttakkari.research.adapters.base import (
    AdapterEnvironmentStatus,
    AdapterRunResult,
    MeasurementRecord,
    ResourceEstimate,
)
from ttakkari.research.resources import checkpoint_resource

# quant_type은 사전 양자화 GGUF 모델 별칭을 고르는 키다. 등록된 정확한 값만 받고 별칭("Q2K" 등)은 받지 않는다.
QUANT_TYPE_MODELS: dict[str, str] = {
    "Q4_K_M": "qwen3.5-4b-q4_k_m",
    "Q2_K": "qwen3.8-flash-next-q2k",
}
_LIVE_PHASES = ("starting", "ready")
logger = logging.getLogger(__name__)


class LlmInferenceAdapter:
    """Execution adapter for local LLM inference benchmarking via HostGpu."""

    id: str = "llm_inference"
    version: str = "1.0.0"
    name_ko: str = "로컬 LLM 추론 및 GPU 가속 벤치마크 어댑터"
    domains: ClassVar[list[str]] = ["llm"]

    # Explicit whitelist: exactly match methods implemented by this adapter
    supported_methods: ClassVar[list[str]] = [
        "kv_cache_quantization",
        "prompt_context_length_tuning",
        "cpu_gpu_offloading",
        "flash_attention",
        "system_batch_size_throughput_tuning",
    ]

    # Explicit whitelist of parameters (both catalog seed keys and adapter internal keys)
    supported_parameters: ClassVar[list[str]] = [
        # Catalog seed parameter keys (llm_inference.json, systems.json)
        "cache_type_k",
        "cache_type_v",
        "ctx_size",
        "batch_size",
        "ubatch_size",
        "n_gpu_layers",
        "flash_attn_enabled",
        "quant_type",
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
    required_inputs: ClassVar[list[str]] = ["model"]

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
            logger.warning("LLM environment inspection failed", exc_info=True)
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
        lease_held = None
        try:
            from ttakkari.host_gpu import _lock_held, state_dir
            s_dir = state_dir(s.data_dir)
            lease_held = _lock_held(s_dir)
        except Exception:
            logger.warning("LLM lease status inspection failed", exc_info=True)

        is_darwin_arm64 = platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")
        hardware = "apple_silicon_metal" if is_darwin_arm64 else ("cuda" if shutil.which("nvidia-smi") else "cpu")
        runtime = "llama.cpp"

        return AdapterEnvironmentStatus(
            adapter_id=self.id,
            ready=ready,
            execution_mode="host_supervisor",
            reason=reason,
            available_models=available_models,
            details={
                "runtime": runtime,
                "hardware": hardware,
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
        if "main_gpu" in config:
            errors.append("main_gpu(다중 GPU 지정)는 HostGpu 실행기에서 미지원됩니다 (device='default' 또는 'none'만 지원).")
        if "quant_method" in config:
            errors.append("quant_method(런타임 양자화 조절)는 미지원됩니다. 사전 양자화 모델(GGUF)을 model 또는 지원되는 quant_type('Q4_K_M', 'Q2_K')으로 지정하십시오.")
        if "temperature" in config and config["temperature"] is not None:
            try:
                temp_val = float(config["temperature"])
                if temp_val != 0.0:
                    errors.append("HostGpu.bench는 재현성 보장을 위해 temperature=0으로 고정되어 있으며 다른 값은 미지원됩니다.")
            except (ValueError, TypeError):
                errors.append("temperature는 숫자여야 합니다.")

        # Resolve model
        model = config.get("model")
        quant_type = config.get("quant_type")
        quant_model = None
        if quant_type is not None:
            quant_model = QUANT_TYPE_MODELS.get(quant_type) if isinstance(quant_type, str) else None
            if quant_model is None:
                errors.append(
                    f"지원되지 않는 quant_type: {quant_type!r}. 허용 값(정확히 일치): {', '.join(QUANT_TYPE_MODELS)}"
                )
        if not model:
            if quant_model:
                model = quant_model
            elif quant_type is None:
                errors.append("model 또는 지원되는 quant_type('Q4_K_M', 'Q2_K') 설정이 필수입니다.")
        elif quant_model and quant_model != model:
            errors.append(
                f"quant_type '{quant_type}'은 모델 '{quant_model}'에 대응하지만 model은 '{model}'입니다. 하나만 지정하거나 일치시키십시오."
            )

        try:
            from ttakkari.config import get_settings
            s = get_settings()
            models_dict = getattr(s, "host_gpu_models", {}) or {}
            if model and model not in models_dict:
                errors.append(
                    f"허용되지 않은 모델 별칭: '{model}'. 허용 목록: {sorted(models_dict.keys())}"
                )
        except Exception:
            logger.warning("LLM model inventory inspection failed", exc_info=True)

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
        from ttakkari.host_gpu import Busy, GpuError, HostGpu, StartRequest

        s = get_settings()
        host_gpu = HostGpu(s)

        # validate_config guarantees model or an exact quant_type is present
        model = config.get("model") or QUANT_TYPE_MODELS[config["quant_type"]]

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
        launch: dict[str, Any] = {}
        start_ownership_error: str | None = None
        intent_committed = False
        cleanup_info: dict[str, Any] = {
            "attempted": False, "success": True, "stopped": False, "skipped_reason": "server_not_started",
        }
        run_result: AdapterRunResult | None = None

        async def start_owned_launch() -> None:
            nonlocal server_started, launch, start_ownership_error, intent_committed
            planned_id = uuid4().hex
            await checkpoint_resource({"kind": "host_gpu", "launch": {"launch_id": planned_id}, "state": "starting"})
            intent_committed = True
            launch = {"launch_id": planned_id}
            try:
                await asyncio.to_thread(host_gpu.start, start_req, launch_id=planned_id)
                fresh = await asyncio.to_thread(self._read_launch, host_gpu)
                if fresh.get("launch_id") != planned_id:
                    raise GpuError("ownership_unverified: started launch identity is missing or replaced")
                launch = fresh
                server_started = True
                await checkpoint_resource({"kind": "host_gpu", "launch": launch, "state": "owned"})
            except BaseException as start_exc:
                if not isinstance(start_exc, Busy):
                    start_ownership_error = f"{type(start_exc).__name__}: {start_exc}"
                    # A timeout or failed post-start commit can leave our supervisor alive.
                    # The precommitted UUID fences this observation; never adopt a replacement.
                    try:
                        fresh = await asyncio.to_thread(self._read_launch, host_gpu)
                        if fresh.get("launch_id") == planned_id:
                            launch, server_started = fresh, True
                    except Exception:
                        logger.warning("LLM startup identity observation failed", exc_info=True)
                raise

        try:
            if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
                run_result = AdapterRunResult(
                    status="cancelled",
                    raw_output={"reason": "cancelled_before_server_start"},
                    environment_snapshot={"scope": scope},
                    wall_seconds=time.monotonic() - started_wall,
                )
                return run_result

            startup = asyncio.create_task(start_owned_launch())
            try:
                await asyncio.shield(startup)
            except asyncio.CancelledError:
                # Cancelling to_thread cannot stop its thread. Observe the
                # launch identity before cleanup, even after a budget expires.
                await startup
                raise

            deadline = time.monotonic() + 45.0
            ready = False
            while time.monotonic() < deadline:
                if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
                    run_result = AdapterRunResult(
                        status="cancelled",
                        raw_output={"reason": "cancelled_while_waiting_readiness"},
                        environment_snapshot={"scope": scope},
                        wall_seconds=time.monotonic() - started_wall,
                    )
                    return run_result
                st = await asyncio.to_thread(host_gpu.status)
                if launch.get("launch_id") and launch.get("server_pid") is None:
                    # server pid/start are recorded by the supervisor after it spawns llama-server; they are
                    # the evidence that our server is gone if our launch is later replaced.
                    with contextlib.suppress(Exception):
                        fresh = await asyncio.to_thread(self._read_launch, host_gpu)
                        if fresh.get("launch_id") == launch["launch_id"]:
                            launch = fresh
                            await checkpoint_resource({"kind": "host_gpu", "launch": launch, "state": "owned"})
                if st.get("ready"):
                    ready = True
                    break
                await asyncio.sleep(0.5)

            if not ready:
                run_result = AdapterRunResult(
                    status="failed",
                    raw_output={"status": host_gpu.status()},
                    environment_snapshot={"scope": scope},
                    error_code="server_not_ready",
                    error_message="llama-server가 45초 내에 준비 상태(ready)가 되지 못했습니다.",
                    wall_seconds=time.monotonic() - started_wall,
                )
                return run_result

            if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
                run_result = AdapterRunResult(
                    status="cancelled",
                    raw_output={"reason": "cancelled_before_bench"},
                    environment_snapshot={"scope": scope},
                    wall_seconds=time.monotonic() - started_wall,
                )
                return run_result

            bench_result = await asyncio.to_thread(
                host_gpu.bench,
                prompt=bench_prompt,
                n_predict=n_predict,
                runs=repetitions,
            )

            total_wall = time.monotonic() - started_wall
            run_result = AdapterRunResult(
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
            return run_result

        except asyncio.CancelledError:
            run_result = AdapterRunResult(
                status="cancelled",
                raw_output={"reason": "task_cancelled"},
                environment_snapshot={"scope": scope},
                error_code="cancelled",
                error_message="LLM execution task cancelled",
                wall_seconds=round(time.monotonic() - started_wall, 3),
            )
            return run_result
        except Exception as exc:
            logger.warning("LLM execution failed", exc_info=True)
            total_wall = time.monotonic() - started_wall
            run_result = AdapterRunResult(
                status="failed",
                raw_output={"error": str(exc), "config": config},
                environment_snapshot={"scope": scope},
                error_code=type(exc).__name__,
                error_message=str(exc),
                wall_seconds=round(total_wall, 3),
            )
            return run_result

        finally:
            if server_started:
                cleanup_info = await asyncio.to_thread(self._cleanup_owned_launch, host_gpu, s.data_dir, launch)
                try:
                    await checkpoint_resource({"kind": "host_gpu", "launch": launch,
                                               "state": "released" if cleanup_info["success"] else "cleanup_unverified",
                                               "cleanup": cleanup_info})
                except Exception:
                    logger.warning("LLM cleanup checkpoint was not persisted", exc_info=True)
                    cleanup_info["checkpoint_persisted"] = False
            elif start_ownership_error:
                cleanup_info = {
                    "attempted": False, "success": False, "stopped": False, "ownership": "unverified",
                    "error": f"{start_ownership_error} — 기록된 시작 의도와 일치하는 실행 신원은 미확인입니다.",
                }
            if intent_committed and not server_started:
                try:
                    await checkpoint_resource({"kind": "host_gpu", "launch": launch,
                                               "state": "cleanup_unverified" if start_ownership_error else "released",
                                               "cleanup": cleanup_info})
                except Exception:
                    logger.warning("LLM startup cleanup checkpoint was not persisted", exc_info=True)
                    cleanup_info["checkpoint_persisted"] = False
            if run_result is not None and isinstance(run_result.raw_output, dict):
                run_result.raw_output["cleanup"] = cleanup_info

    @staticmethod
    def _read_launch(host_gpu: Any) -> dict[str, Any]:
        """launch id and supervisor/server process identity (pid + create_time) of the current state."""
        state = host_gpu._current() or {}
        keys = ("launch_id", "supervisor_pid", "supervisor_start", "server_pid", "server_start")
        launch = {key: state.get(key) for key in keys}
        if launch["launch_id"]:
            launch["launch_id"] = str(launch["launch_id"])
        return launch

    @staticmethod
    def _launch_processes_absent(launch: dict[str, Any]) -> tuple[bool, dict[str, str]]:
        """Is every recorded process of our launch proven gone? Only ABSENT counts, as in HostGpu's own checks."""
        from ttakkari.host_gpu_guard import ProcState, process_state

        evidence = {
            "supervisor": process_state(launch.get("supervisor_pid"), launch.get("supervisor_start")).value,
            "server": process_state(launch.get("server_pid"), launch.get("server_start")).value,
        }
        return all(v == ProcState.ABSENT.value for v in evidence.values()), evidence

    @staticmethod
    def _cleanup_owned_launch(host_gpu: Any, data_dir: Path, launch: dict[str, Any]) -> dict[str, Any]:
        """Stop the server only if it is the launch this run started, and judge the stop result.

        Ownership is enforced inside HostGpu.stop(expected_launch_id=...) on the single state it reads,
        so there is no check-then-stop window here. If HostGpu refuses because another launch is now
        current, that proves nothing about our processes, so success requires the recorded supervisor and
        server identities (pid + start time) to be observed absent.
        """
        from ttakkari.host_gpu import LaunchMismatch, _lock_held, state_dir

        launch_id = launch.get("launch_id")
        info: dict[str, Any] = {"attempted": True, "success": False, "launch_id": launch_id, "stopped": False}
        if launch_id is None:
            info["error"] = "ownership_unverified: start() 직후 상태에서 launch_id를 읽지 못해 stop을 호출하지 않았습니다."
            return info
        try:
            stop_res = host_gpu.stop(expected_launch_id=launch_id)
            info["result"] = stop_res
            if not isinstance(stop_res, dict):
                info["error"] = f"unexpected_stop_result: {type(stop_res).__name__}"
            elif stop_res.get("running") or stop_res.get("phase") in _LIVE_PHASES:
                info["error"] = f"still_running: phase={stop_res.get('phase')}"
            elif _lock_held(state_dir(data_dir)):
                info["error"] = "lock_still_held: 감독자 종료 후에도 호스트 자원 잠금이 해제되지 않았습니다."
            elif stop_res.get("stopped_now"):
                info.update(success=True, stopped=True)
            else:
                info.update(success=True, skipped_reason="already_stopped")
        except LaunchMismatch as exc:
            info.update(skipped_reason="launch_replaced", detail=str(exc))
            try:
                absent, evidence = LlmInferenceAdapter._launch_processes_absent(launch)
            except Exception as probe_exc:
                logger.warning("LLM launch identity probe failed during cleanup", exc_info=True)
                info["error"] = f"termination_unverified: {type(probe_exc).__name__}: {probe_exc}"
                return info
            info["process_evidence"] = evidence
            if absent:
                info["success"] = True
            elif "present" in evidence.values():
                info["error"] = f"launch_replaced_but_process_present: {evidence} — 다른 launch가 현재라 stop하지 않았습니다."
            else:
                info["error"] = f"termination_unverified: {evidence} — 종료를 관측하지 못했습니다."
        except Exception as exc:
            logger.warning("LLM owned launch cleanup failed", exc_info=True)
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info

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

        # HostGpu bench() returns 'swap' at top-level. Read top-level swap first, fallback to status.swap
        status = raw.get("status") or {}
        swap = raw.get("swap") or status.get("swap") or {}
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
