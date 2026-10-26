"""Local TTS synthesis and pronunciation evaluation adapter."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

from ttakkari.research.adapters.base import (
    AdapterEnvironmentStatus,
    AdapterRunResult,
    MeasurementRecord,
    ResourceEstimate,
)
from ttakkari.research.adapters.presets import load_tts_preset
from ttakkari.tts.providers import TtsResult

VALID_MODES = {"normalize_only", "synthesize", "synthesize_and_stt"}
logger = logging.getLogger(__name__)
VALID_VOICES = {"M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"}

# Voice preset mapping from seed to Supertonic voice IDs
VOICE_PRESET_MAP: dict[str, str] = {
    "ko_female_1": "F1",
    "ko_female_2": "F2",
    "ko_female_news": "F2",
    "ko_male_1": "M1",
    "ko_male_2": "M2",
}


def _cancel_requested(cancel_event: Any) -> bool:
    return bool(cancel_event and getattr(cancel_event, "is_set", lambda: False)())


async def _await_unless_cancelled(
    awaitable: Any, cancel_event: Any, *, poll_seconds: float = 0.1
) -> tuple[Any, bool]:
    """Await `awaitable`, cancelling it as soon as `cancel_event` is set. Returns (result, cancelled)."""
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_seconds)
            if done:
                return task.result(), False
            if _cancel_requested(cancel_event):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return None, True
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _apply_custom_dict(text: str, custom_dict: dict[str, str], *, case_sensitive: bool = False) -> str:
    """Replace domain terms with custom pronunciation entries."""
    if not custom_dict:
        return text
    result = text
    # Replace longer keys first to prevent partial match conflicts
    for k in sorted(custom_dict.keys(), key=len, reverse=True):
        val = custom_dict[k]
        if case_sensitive:
            result = result.replace(k, val)
        else:
            pattern = re.compile(re.escape(k), re.IGNORECASE)
            result = pattern.sub(val, result)
    return result


def _normalize_with_options(
    text: str,
    *,
    use_convert_numbers: bool = True,
    use_read_latin: bool = True,
    use_plain_text: bool = True,
    custom_dict: dict[str, str] | None = None,
    case_sensitive: bool = False,
) -> str:
    """Normalize text with granular component toggles."""
    from ttakkari.tts.normalize import (
        _EMOJI,
        _ZERO_WIDTH,
        _final_cleanup,
        _symbols,
        convert_numbers,
        plain_text,
        read_latin,
    )

    t = unicodedata.normalize("NFC", text)
    if custom_dict:
        t = _apply_custom_dict(t, custom_dict, case_sensitive=case_sensitive)
    t = _ZERO_WIDTH.sub("", t)
    if use_plain_text:
        t = plain_text(t)
    t = _EMOJI.sub(" ", t)
    if use_convert_numbers:
        t = convert_numbers(t)
    t = _symbols(t)
    if use_read_latin:
        t = read_latin(t)
    t = _final_cleanup(t)
    if custom_dict:
        t = _apply_custom_dict(t, custom_dict, case_sensitive=case_sensitive)
    return unicodedata.normalize("NFC", t)


class TtsSynthesisAdapter:
    """Evaluation adapter for TTS normalization, Supertonic 3 synthesis, and STT validation."""

    id: str = "tts_synthesis"
    version: str = "1.0.0"
    name_ko: str = "로컬 TTS 음성 합성 및 발음 평가 어댑터"
    domains: ClassVar[list[str]] = ["tts", "stt"]

    # Explicit whitelist: exactly match methods implemented by this adapter
    supported_methods: ClassVar[list[str]] = [
        "tts_korean_text_normalization",
        "tts_pronunciation_dictionary",
        "tts_sentence_segmentation",
    ]

    # Explicit whitelist of parameters (both catalog seed keys and adapter internal keys)
    supported_parameters: ClassVar[list[str]] = [
        # Catalog seed parameter keys (tts.json)
        "handle_measure_units",
        "english_acronym_mode",
        "case_sensitive",
        "max_chunk_length",
        "speed_ratio",
        "voice_preset",
        # Adapter internal parameter keys
        "mode",
        "engine",
        "voice",
        "speed",
        "convert_numbers",
        "read_latin",
        "plain_text",
        "custom_pronunciation_dict",
        "max_chunk_chars",
        "preset_id",
        "texts",
    ]
    required_inputs: ClassVar[list[str]] = ["preset_id"]

    def check_environment(self, *, scope: str) -> AdapterEnvironmentStatus:
        """Check Supertonic 3 model, runtime venv, and STT availability."""
        details: dict[str, Any] = {
            "scope": scope,
            "runtime": "onnxruntime",
            "hardware": "cpu",
            "normalize_only_ready": True,
            "engine": "supertonic",
            "kokoro_supported": False,
            "kokoro_note": "Kokoro는 한국어 음성 미지원으로 폐기되었으며 Supertonic 3를 사용합니다.",
        }

        try:
            from ttakkari.config import get_settings
            from ttakkari.tts.supertonic import SupertonicProvider

            s = get_settings()
            provider = SupertonicProvider(s)
            avail = provider.availability()

            voices = [v.id for v in provider.voices_sync()]
            synth_ready = avail.ready
            details["synth_ready"] = synth_ready
            details["supertonic_status"] = avail.reason
            details["available_voices"] = voices

            # Check STT availability
            stt_ready = False
            try:
                stt_py = getattr(s, "local_stt_python", "") or ""
                if Path(stt_py).is_file():
                    stt_ready = True
            except Exception:
                logger.warning("TTS STT runtime inspection failed", exc_info=True)
            details["stt_ready"] = stt_ready

            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=bool(synth_ready),
                execution_mode="subprocess_worker" if synth_ready else "in_process",
                reason=f"전처리 정규화 상시 지원; 음성 합성 엔진: {avail.reason}"
                if synth_ready
                else f"합성 엔진 준비 안 됨({avail.reason}); 텍스트 정규화(normalize_only)만 가능",
                available_models=voices if synth_ready else [],
                details=details,
            )
        except Exception as exc:
            logger.warning("TTS environment inspection failed", exc_info=True)
            return AdapterEnvironmentStatus(
                adapter_id=self.id,
                ready=False,
                execution_mode="in_process",
                reason=f"기본 정규화 전처리 지원 (엔진 확인 오류: {exc})",
                available_models=[],
                details=details,
            )

    def validate_config(self, config: dict[str, Any], *, scope: str) -> list[str]:
        errors: list[str] = []

        # Check for unsupported seed methods/parameters
        if "descriptive_lenition" in config or "phoneme_format" in config:
            errors.append(
                "음소(phoneme) 단위 G2P(tts_korean_g2p)는 미지원됩니다. Supertonic 내장 음소 파이프라인 또는 external_report를 사용하세요."
            )
        if "vocoder_type" in config:
            errors.append(
                "보코더 교체(tts_vocoder_benchmark)는 미지원됩니다 (Supertonic 3 내장 ONNX vocoder 고정)."
            )
        if "pitch_scale" in config or "energy_scale" in config:
            errors.append(
                "피치/에너지 운율 제어(tts_prosody_control)는 Supertonic 3 ONNX 모델 구조상 미지원됩니다."
            )
        if "split_on_comma" in config:
            errors.append(
                "split_on_comma는 미지원됩니다 (현재 청킹 파이프라인은 문장/절 단위 정규식 분할만 지원)."
            )
        if "total_steps" in config:
            errors.append(
                "total_steps(확산 스텝 제어)는 Supertonic 3 ONNX 고정 파이프라인에서 미지원됩니다."
            )

        # Audit english_acronym_mode
        acronym_mode = config.get("english_acronym_mode")
        if acronym_mode is not None:
            if acronym_mode in ("as_word", "spelled_out"):
                errors.append(
                    f"english_acronym_mode='{acronym_mode}'은 현재 정규화 엔진에서 미지원됩니다 "
                    "(한국어 약어 사전 기반 'korean_alphabet' 및 비변환 'raw'만 지원)."
                )
            elif acronym_mode not in ("korean_alphabet", "raw"):
                errors.append(
                    f"지원되지 않는 english_acronym_mode: '{acronym_mode}'. 허용 값: korean_alphabet, raw"
                )

        mode = config.get("mode", "normalize_only")
        if mode not in VALID_MODES:
            errors.append(f"지원되지 않는 mode: '{mode}'. 허용 값: {sorted(VALID_MODES)}")

        engine = config.get("engine")
        if engine and engine.lower() == "kokoro":
            # Kokoro is deprecated. We will map to supertonic in execute_run, but flag in warning
            pass

        # Check speed / speed_ratio
        speed_val = config.get("speed_ratio") if "speed_ratio" in config else config.get("speed", 1.0)
        if speed_val is not None and (not isinstance(speed_val, (int, float)) or speed_val < 0.5 or speed_val > 2.0):
            errors.append("속도(speed / speed_ratio)는 0.5 이상 2.0 이하의 숫자여야 합니다.")

        # Check voice / voice_preset
        voice_val = config.get("voice_preset") or config.get("voice", "M1")
        resolved_voice = VOICE_PRESET_MAP.get(voice_val, voice_val)
        if resolved_voice not in VALID_VOICES:
            errors.append(
                f"지원되지 않는 voice: '{voice_val}'. 허용 값: {sorted(VALID_VOICES)} 또는 {list(VOICE_PRESET_MAP.keys())}"
            )

        preset_id = config.get("preset_id")
        texts = config.get("texts")
        if not preset_id and not texts:
            errors.append("preset_id 또는 texts 설정이 필수입니다.")

        if preset_id:
            try:
                load_tts_preset(str(preset_id))
            except FileNotFoundError:
                errors.append(f"존재하지 않는 TTS 프리셋 ID: '{preset_id}'.")

        custom_dict = config.get("custom_pronunciation_dict")
        if custom_dict is not None and not isinstance(custom_dict, dict):
            errors.append("custom_pronunciation_dict는 키-값 문자열 딕셔너리여야 합니다.")

        return errors

    def estimate_resources(self, config: dict[str, Any]) -> ResourceEstimate:
        mode = config.get("mode", "normalize_only")
        if mode == "normalize_only":
            return ResourceEstimate(
                estimated_seconds=2,
                estimated_memory_mb=64,
                requires_gpu_lease=False,
                requires_tts_lock=False,
                requires_cpu_lock=False,
            )
        return ResourceEstimate(
            estimated_seconds=20,
            estimated_memory_mb=1024,
            requires_gpu_lease=False,
            requires_tts_lock=True,
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

        started_wall = time.monotonic()

        # Load texts
        preset_id = config.get("preset_id")
        custom_texts = config.get("texts")
        if custom_texts and isinstance(custom_texts, list) and len(custom_texts) > 0:
            raw_texts = custom_texts
            dataset_name = f"{preset_id} (selected subset)" if preset_id else "custom_texts"
        elif preset_id:
            preset_data = load_tts_preset(str(preset_id))
            raw_texts = preset_data.get("sentences", [])
            dataset_name = preset_data.get("name", preset_id)
        else:
            raw_texts = []
            dataset_name = "custom_texts"

        # Parameter Mapping (Seed -> Adapter)
        mode = config.get("mode", "normalize_only")
        engine_req = config.get("engine", "supertonic")

        # Map voice
        voice_param = config.get("voice_preset") or config.get("voice", "M1")
        voice = VOICE_PRESET_MAP.get(voice_param, voice_param)

        # Map speed
        speed = float(config.get("speed_ratio") if "speed_ratio" in config else config.get("speed", 1.0))

        # Map max chunk chars
        max_chunk_chars = int(
            config.get("max_chunk_length") if "max_chunk_length" in config else config.get("max_chunk_chars", 220)
        )

        # Map normalization toggles
        convert_numbers = bool(
            config.get("handle_measure_units") if "handle_measure_units" in config else config.get("convert_numbers", True)
        )

        # Map english acronym mode
        english_mode = config.get("english_acronym_mode")
        if english_mode == "raw":
            read_latin = False
        elif english_mode in ("korean_alphabet", "as_word", "spelled_out"):
            read_latin = True
        else:
            read_latin = bool(config.get("read_latin", True))

        plain_text_flag = bool(config.get("plain_text", True))
        custom_dict = config.get("custom_pronunciation_dict") or {}
        case_sensitive = bool(config.get("case_sensitive", False))

        warnings: list[str] = []
        if engine_req.lower() == "kokoro":
            warnings.append("Kokoro는 한국어 음성 미지원으로 폐기되어 Supertonic 3 엔진으로 실행되었습니다.")

        # 1. Text Normalization Pipeline
        from ttakkari.tts.normalize import chunk_text

        sentence_reports: list[dict[str, Any]] = []
        all_chunks: list[str] = []
        chunk_sentence_map: list[int] = []
        raw_char_count = 0
        normalized_char_count = 0

        for s_idx, raw_s in enumerate(raw_texts):
            raw_char_count += len(raw_s)
            norm_s = _normalize_with_options(
                raw_s,
                use_convert_numbers=convert_numbers,
                use_read_latin=read_latin,
                use_plain_text=plain_text_flag,
                custom_dict=custom_dict,
                case_sensitive=case_sensitive,
            )
            normalized_char_count += len(norm_s)
            chunks = chunk_text(norm_s, max_chars=max_chunk_chars, normalize=False)
            all_chunks.extend(chunks)
            chunk_sentence_map.extend([s_idx] * len(chunks))

            sentence_reports.append({
                "index": s_idx + 1,
                "raw_text": raw_s,
                "normalized_text": norm_s,
                "char_delta": len(norm_s) - len(raw_s),
                "chunks": chunks,
                "chunk_count": len(chunks),
            })

        char_delta_total = normalized_char_count - raw_char_count

        # If mode is normalize_only, return results immediately
        if mode == "normalize_only":
            total_wall = time.monotonic() - started_wall
            return AdapterRunResult(
                status="succeeded",
                raw_output={
                    "mode": mode,
                    "engine": "supertonic",
                    "engine_requested": engine_req,
                    "warnings": warnings,
                    "dataset_name": dataset_name,
                    "sentence_count": len(raw_texts),
                    "chunk_count": len(all_chunks),
                    "raw_char_count": raw_char_count,
                    "normalized_char_count": normalized_char_count,
                    "char_delta_total": char_delta_total,
                    "sentences": sentence_reports,
                    "wall_seconds": round(total_wall, 3),
                },
                environment_snapshot={"scope": scope, "mode": mode},
                wall_seconds=round(total_wall, 3),
            )

        # 2. Synthesis Mode (Supertonic 3)
        from ttakkari.config import get_settings
        from ttakkari.tts.service import _execution_lock
        from ttakkari.tts.supertonic import SupertonicProvider

        s = get_settings()
        provider = SupertonicProvider(s)
        avail = provider.availability()

        if not avail.ready:
            return AdapterRunResult(
                status="failed",
                raw_output={
                    "mode": mode,
                    "error": avail.reason,
                    "sentences": sentence_reports,
                },
                environment_snapshot={"scope": scope, "engine": "supertonic"},
                error_code="tts_engine_unavailable",
                error_message=f"Supertonic 3 음성 엔진 사용 불가: {avail.reason}",
                wall_seconds=time.monotonic() - started_wall,
            )

        if _cancel_requested(cancel_event):
            return AdapterRunResult(
                status="cancelled",
                raw_output={"reason": "cancelled_before_synthesis"},
                environment_snapshot={"scope": scope},
                wall_seconds=time.monotonic() - started_wall,
            )

        temp_dir = tempfile.mkdtemp(prefix=f"research_tts_{run_id}_")
        artifacts_to_register: list[dict[str, Any]] = []

        def _cancelled_result(reason: str, *, artifacts: list[dict[str, Any]] | None = None) -> AdapterRunResult:
            return AdapterRunResult(
                status="cancelled",
                raw_output={"reason": reason},
                environment_snapshot={"scope": scope},
                wall_seconds=round(time.monotonic() - started_wall, 3),
                artifacts=artifacts or [],
            )

        async def _synthesize_under_budget() -> TtsResult | None:
            async with _execution_lock:
                if _cancel_requested(cancel_event):  # cancelled while queued for the lock
                    return None
                return await provider.synthesize(
                    all_chunks,
                    voice=voice,
                    speed=speed,
                    lang="ko",
                    out_dir=temp_dir,
                )

        try:
            # Both the lock wait and the synthesis itself are cancelled when cancel_event is set.
            tts_res, cancelled = await _await_unless_cancelled(_synthesize_under_budget(), cancel_event)
            if cancelled or tts_res is None:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return _cancelled_result("cancelled_during_synthesis")

            metrics = tts_res.metrics or {}
            synth_seconds = float(metrics.get("synth_seconds", 0.0))
            audio_seconds = float(metrics.get("audio_seconds", 0.0))
            load_seconds = float(metrics.get("load_seconds", 0.0))
            peak_rss = int(metrics.get("peak_rss_bytes", 0))
            rtf = (synth_seconds / audio_seconds) if audio_seconds > 0 else 0.0

            # Collect audio chunk artifacts
            audio_chunks_info = []
            for c_idx, cr in enumerate(tts_res.chunks):
                chunk_file = Path(cr.path)
                artifacts_to_register.append({
                    "kind": "audio",
                    "role": "audio_sample",
                    "category": "research_tts_sample",
                    "path": str(chunk_file),
                    "filename": chunk_file.name,
                    "duration_seconds": cr.duration_seconds,
                    "sample_rate": cr.sample_rate,
                    "bytes": cr.bytes,
                })
                audio_chunks_info.append({
                    "chunk_index": c_idx,
                    "path": str(chunk_file),
                    "filename": chunk_file.name,
                    "duration_seconds": cr.duration_seconds,
                    "sample_rate": cr.sample_rate,
                    "bytes": cr.bytes,
                })

            stt_eval_results: list[dict[str, Any]] = []

            # 3. Optional STT Recognition and CER/WER Evaluation
            if mode == "synthesize_and_stt":
                from ttakkari.meetings.local_stt import core as stt_core
                from ttakkari.meetings.local_stt import runner as stt_runner

                stt_py = getattr(s, "local_stt_python", "") or ""
                if not Path(stt_py).is_file():
                    total_wall = time.monotonic() - started_wall
                    return AdapterRunResult(
                        status="failed",
                        raw_output={
                            "error": f"STT 런타임 환경(local_stt_python={stt_py}) 부재로 음성 재인식 평가 불가",
                            "artifacts": artifacts_to_register,
                "audio_chunks": audio_chunks_info,
                        },
                        environment_snapshot={"scope": scope, "engine": "supertonic"},
                        error_code="stt_runtime_missing",
                        error_message="STT 파이썬 실행기가 존재하지 않아 발음 검증을 수행할 수 없습니다.",
                        wall_seconds=round(total_wall, 3),
                        artifacts=artifacts_to_register,
                    )

                stt_failures = 0
                for idx, chunk_info in enumerate(audio_chunks_info):
                    if _cancel_requested(cancel_event):
                        return _cancelled_result("cancelled_during_stt_eval", artifacts=artifacts_to_register)

                    wav_path = Path(chunk_info["path"])
                    stt_params = {"language": "ko"}
                    try:
                        stt_out, stt_cancelled = await _await_unless_cancelled(
                            stt_runner.run_worker(wav_path, stt_params, s, production=True),
                            cancel_event,
                        )
                        if stt_cancelled:
                            return _cancelled_result("cancelled_during_stt_eval", artifacts=artifacts_to_register)
                        transcript = str(
                            stt_out.get("original_text")
                            or stt_out.get("text")
                            or stt_out.get("transcript")
                            or ""
                        ).strip()
                        if transcript.startswith("[") and "]" in transcript:
                            transcript = re.sub(r"^\[[\d\.\:\–\- ]+\]\s*(화자\s*\d+:)?\s*", "", transcript).strip()
                        norm_ref = all_chunks[idx] if idx < len(all_chunks) else ""
                        sent_idx = chunk_sentence_map[idx] if idx < len(chunk_sentence_map) else 0
                        raw_ref = raw_texts[sent_idx] if sent_idx < len(raw_texts) else norm_ref

                        rates_norm = stt_core.error_rates(norm_ref, transcript)
                        rates_raw = stt_core.error_rates(raw_ref, transcript)

                        stt_eval_results.append({
                            "chunk_index": idx,
                            "recognized_text": transcript,
                            "normalized_reference": norm_ref,
                            "raw_reference": raw_ref,
                            "cer_vs_normalized": rates_norm.get("cer"),
                            "wer_vs_normalized": rates_norm.get("wer"),
                            "cer_vs_raw": rates_raw.get("cer"),
                            "wer_vs_raw": rates_raw.get("wer"),
                        })
                    except Exception as stt_exc:
                        logger.warning("TTS pronunciation transcription chunk failed", exc_info=True)
                        stt_failures += 1
                        stt_eval_results.append({
                            "chunk_index": idx,
                            "stt_error": f"{type(stt_exc).__name__}: {stt_exc}",
                        })

                # AdapterRunResult has no partial status, and a mean over the surviving chunks would
                # read as the whole dataset, so one failed chunk fails the run.
                if stt_failures > 0:
                    total_wall = time.monotonic() - started_wall
                    failed_chunks = [e["chunk_index"] for e in stt_eval_results if "stt_error" in e]
                    return AdapterRunResult(
                        status="failed",
                        raw_output={
                            "error": "STT 재인식 워커 실행 실패",
                            "failed_chunks": failed_chunks,
                            "stt_chunk_count": len(audio_chunks_info),
                            "stt_eval": stt_eval_results,
                            "artifacts": artifacts_to_register,
                "audio_chunks": audio_chunks_info,
                        },
                        environment_snapshot={"scope": scope, "engine": "supertonic"},
                        error_code="stt_worker_failed",
                        error_message=(
                            f"STT 재인식이 {stt_failures}/{len(audio_chunks_info)}개 청크에서 실패해 "
                            f"평가 표본이 불완전합니다 (실패 chunk: {failed_chunks})"
                        ),
                        wall_seconds=round(total_wall, 3),
                        artifacts=artifacts_to_register,
                    )

            total_wall = time.monotonic() - started_wall

            raw_out = {
                "mode": mode,
                "engine": "supertonic",
                "engine_requested": engine_req,
                "warnings": warnings,
                "voice": voice,
                "speed": speed,
                "dataset_name": dataset_name,
                "sentence_count": len(raw_texts),
                "chunk_count": len(all_chunks),
                "raw_char_count": raw_char_count,
                "normalized_char_count": normalized_char_count,
                "char_delta_total": char_delta_total,
                "metrics": {
                    "rtf": round(rtf, 4),
                    "synth_seconds": round(synth_seconds, 3),
                    "audio_seconds": round(audio_seconds, 3),
                    "load_seconds": round(load_seconds, 3),
                    "peak_rss_mb": round(peak_rss / 1048576.0, 2),
                    "wall_seconds": round(total_wall, 3),
                },
                "artifacts": artifacts_to_register,
                "audio_chunks": audio_chunks_info,
                "stt_eval": stt_eval_results,
                "sentences": sentence_reports,
            }

            return AdapterRunResult(
                status="succeeded",
                raw_output=raw_out,
                environment_snapshot={
                    "scope": scope,
                    "engine": "supertonic",
                    "voice": voice,
                    "temp_dir": temp_dir,
                },
                wall_seconds=round(total_wall, 3),
                artifacts=artifacts_to_register,
            )

        except asyncio.CancelledError:
            # The awaited provider/STT child has completed its teardown first.
            if not artifacts_to_register:
                shutil.rmtree(temp_dir, ignore_errors=True)
            result = _cancelled_result("task_cancelled", artifacts=artifacts_to_register)
            if artifacts_to_register:
                result.raw_output["artifacts"] = artifacts_to_register
                result.environment_snapshot["temp_dir"] = temp_dir
            return result
        except Exception as exc:
            logger.warning("TTS execution failed", exc_info=True)
            total_wall = time.monotonic() - started_wall
            return AdapterRunResult(
                status="failed",
                raw_output={"error": str(exc), "mode": mode},
                environment_snapshot={"scope": scope, "engine": "supertonic"},
                error_code=type(exc).__name__,
                error_message=str(exc),
                wall_seconds=round(total_wall, 3),
            )

    def to_measurements(
        self, result: AdapterRunResult, config: dict[str, Any]
    ) -> list[MeasurementRecord]:
        if result.status != "succeeded":
            return []

        raw = result.raw_output
        metrics = raw.get("metrics") or {}
        records: list[MeasurementRecord] = []

        chunk_count = raw.get("chunk_count", 0)
        char_delta = raw.get("char_delta_total", 0)

        records.append(
            MeasurementRecord(
                metric_key="chunk_count",
                metric_label="분할된 총 청크 수",
                value=float(chunk_count),
                unit="count",
                direction="lower_is_better",
                aggregator="single",
            )
        )

        records.append(
            MeasurementRecord(
                metric_key="normalized_char_delta",
                metric_label="정규화 후 글자 수 증감",
                value=float(char_delta),
                unit="count",
                direction="lower_is_better",
                aggregator="single",
            )
        )

        if "rtf" in metrics:
            records.append(
                MeasurementRecord(
                    metric_key="rtf",
                    metric_label="실시간 계수 (RTF)",
                    value=float(metrics["rtf"]),
                    unit="ratio",
                    direction="lower_is_better",
                    aggregator="single",
                )
            )

        if "synth_seconds" in metrics:
            records.append(
                MeasurementRecord(
                    metric_key="synth_seconds",
                    metric_label="음성 합성 소요 시간",
                    value=float(metrics["synth_seconds"]),
                    unit="s",
                    direction="lower_is_better",
                    aggregator="single",
                )
            )

        if "audio_seconds" in metrics:
            records.append(
                MeasurementRecord(
                    metric_key="audio_seconds",
                    metric_label="생성된 오디오 총 길이",
                    value=float(metrics["audio_seconds"]),
                    unit="s",
                    direction="higher_is_better",
                    aggregator="single",
                )
            )

        if "load_seconds" in metrics:
            records.append(
                MeasurementRecord(
                    metric_key="load_seconds",
                    metric_label="모델 로드 시간",
                    value=float(metrics["load_seconds"]),
                    unit="s",
                    direction="lower_is_better",
                    aggregator="single",
                )
            )

        if "peak_rss_mb" in metrics:
            records.append(
                MeasurementRecord(
                    metric_key="peak_rss_mb",
                    metric_label="최대 RSS 메모리",
                    value=float(metrics["peak_rss_mb"]),
                    unit="MB",
                    direction="lower_is_better",
                    aggregator="single",
                )
            )

        stt_eval = raw.get("stt_eval") or []
        cer_values = [item["cer_vs_normalized"] for item in stt_eval if item.get("cer_vs_normalized") is not None]
        wer_values = [item["wer_vs_normalized"] for item in stt_eval if item.get("wer_vs_normalized") is not None]
        cer_raw_values = [item["cer_vs_raw"] for item in stt_eval if item.get("cer_vs_raw") is not None]
        wer_raw_values = [item["wer_vs_raw"] for item in stt_eval if item.get("wer_vs_raw") is not None]

        if cer_values:
            mean_cer = sum(cer_values) / len(cer_values)
            records.append(
                MeasurementRecord(
                    metric_key="stt_cer_vs_normalized",
                    metric_label="STT 재인식 음절 오류율 (정규화문 대리 지표)",
                    value=round(mean_cer, 4),
                    unit="ratio",
                    direction="lower_is_better",
                    is_proxy=True,
                    aggregator="mean",
                    meta={"explanation": "Whisper 재인식을 통한 발음 명료도 간접 대리 지표이며 사람 청취 품질(MOS)이 아님"},
                )
            )

        if cer_raw_values:
            mean_cer_raw = sum(cer_raw_values) / len(cer_raw_values)
            records.append(
                MeasurementRecord(
                    metric_key="stt_cer_vs_raw",
                    metric_label="STT 재인식 음절 오류율 (원문 대리 지표)",
                    value=round(mean_cer_raw, 4),
                    unit="ratio",
                    direction="lower_is_better",
                    is_proxy=True,
                    aggregator="mean",
                    meta={"explanation": "원문 기준 Whisper 재인식 음절 오류율(CER) 대리 지표"},
                )
            )

        if wer_values:
            mean_wer = sum(wer_values) / len(wer_values)
            records.append(
                MeasurementRecord(
                    metric_key="stt_wer_vs_normalized",
                    metric_label="STT 재인식 어절 오류율 (정규화문 대리 지표)",
                    value=round(mean_wer, 4),
                    unit="ratio",
                    direction="lower_is_better",
                    is_proxy=True,
                    aggregator="mean",
                    meta={"explanation": "Whisper 재인식을 통한 발음 명료도 간접 대리 지표이며 사람 청취 품질(MOS)이 아님"},
                )
            )

        if wer_raw_values:
            mean_wer_raw = sum(wer_raw_values) / len(wer_raw_values)
            records.append(
                MeasurementRecord(
                    metric_key="stt_wer_vs_raw",
                    metric_label="STT 재인식 어절 오류율 (원문 대리 지표)",
                    value=round(mean_wer_raw, 4),
                    unit="ratio",
                    direction="lower_is_better",
                    is_proxy=True,
                    aggregator="mean",
                    meta={"explanation": "원문 기준 Whisper 재인식 어절 오류율(WER) 대리 지표"},
                )
            )

        records.append(
            MeasurementRecord(
                metric_key="wall_seconds",
                metric_label="전체 소요 시간",
                value=float(metrics.get("wall_seconds", result.wall_seconds)),
                unit="s",
                direction="lower_is_better",
                aggregator="single",
            )
        )

        return records
