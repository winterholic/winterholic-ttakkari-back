"""로컬 TTS 어댑터(Supertonic, ONNX CPU): 오디오 선행 단계의 발화 합성.

기존 `ttakkari.tts` 공급자 계약을 그대로 쓰고 새 모델을 설치하거나 내려받지 않는다. 결과는 한 줄씩 합성한 WAV를 이어 붙인 음성과,
줄마다 시작·끝 샘플을 정수로 적은 timing JSON이다(타임라인 cue의 원시 시점). 취소는 합성 호출 전후에만 협조한다.
공용 TTS 실행 예산(`tts.service.budget`)은 이 어댑터가 잡지 않는다. 운영 연결 때 호출 측이 예산을 잡아야 한다.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import wave
from typing import Any, ClassVar

from ttakkari.creative.engines.base import (
    EngineAdapter,
    EngineEmptyOutput,
    EngineError,
    EngineOutput,
    EngineRun,
    ExecutionContext,
    ProbeResult,
    ProfileSpec,
    apply_defaults,
    now_iso,
    validate_params,
)

PROFILES = {
    "supertonic-speech": ProfileSpec(
        "supertonic-speech",
        frozenset({"speech", "narration"}),
        "한 줄 한 발화로 합성해 줄별 시작·끝 샘플이 있는 하나의 음성 트랙을 만든다.",
        "audio",
        params={
            "lines": {
                "type": "str_list",
                "required": True,
                "max_items": 20,
                "max_len": 240,
            },
            "voice": {"type": "str", "default": "F2", "max_len": 8},
            "speed": {"type": "float", "default": 1.0, "min": 0.5, "max": 2.0},
            "lang": {"type": "enum", "default": "ko", "values": ["ko", "en"]},
            "gap_ms": {"type": "int", "default": 280, "min": 0, "max": 2000},
            "lead_ms": {"type": "int", "default": 0, "min": 0, "max": 3000},
        },
    )
}


class TtsAdapter(EngineAdapter):
    engine_id = "tts"
    display_name = "로컬 TTS(Supertonic CPU)"
    profiles = PROFILES
    rights: ClassVar[dict[str, str]] = {
        "license": "Supertonic 3 모델·가중치 이용 조건은 L 작업에서 원문 확인",
        "use_scope": "personal",
    }

    def __init__(self, provider=None, *, mock: bool = False) -> None:
        # mock=True는 공급자가 대역인 계약 시험용이다.
        self._provider = provider
        self.execution_mode = "mock" if mock else "real"

    @property
    def provider(self):
        if self._provider is None:
            from ttakkari.tts.supertonic import SupertonicProvider

            self._provider = SupertonicProvider()
        return self._provider

    def probe(self) -> ProbeResult:
        try:
            availability = self.provider.availability()
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                "probe_failed",
                "backend-tts-runtime",
                None,
                None,
                now_iso(),
                error=f"{type(exc).__name__}: {exc}",
            )
        if not availability.ready:
            return ProbeResult(
                "absent",
                "backend-tts-runtime",
                None,
                None,
                now_iso(),
                {"reason": availability.reason},
                error=availability.reason,
            )
        python = getattr(self.provider.settings, "local_tts_python", None)
        details: dict[str, Any] = {"provider": getattr(self.provider, "id", "unknown")}
        if python:
            # 실제로 런타임 파이썬을 실행해 합성 의존성이 import되는지 확인한다(합성은 하지 않는다).
            run = subprocess.run(
                [
                    python,
                    "-c",
                    "import onnxruntime,sys;sys.stdout.write(onnxruntime.__version__)",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if run.returncode != 0:
                return ProbeResult(
                    "probe_failed",
                    "backend-tts-runtime",
                    python,
                    None,
                    now_iso(),
                    details,
                    error="TTS 런타임에서 onnxruntime을 불러오지 못했습니다.",
                )
            details["onnxruntime"] = run.stdout.strip()
        return ProbeResult(
            "available",
            "backend-tts-runtime",
            python,
            self.provider.model_revision(),
            now_iso(),
            details,
        )

    def validate(self, spec: dict[str, Any]) -> list[str]:
        profile = self.profiles.get(spec.get("profile", ""))
        if profile is None:
            return ["등록되지 않은 프로필입니다."]
        params = spec.get("params") or {}
        errors = validate_params(profile.params, params)
        lines = params.get("lines")
        if isinstance(lines, list) and (
            not lines or any(not line.strip() for line in lines)
        ):
            errors.append("빈 줄은 합성할 수 없습니다.")
        return errors

    def execute(self, ctx: ExecutionContext) -> EngineRun:
        spec = ctx.spec
        errors = self.validate(spec)
        if errors:
            raise EngineError("invalid_spec", "; ".join(errors))
        profile = self.profiles[spec["profile"]]
        params = apply_defaults(profile.params, spec.get("params") or {})
        lines = params["lines"]
        ctx.check_cancel()
        out_dir = ctx.workdir / "tts"
        out_dir.mkdir()
        try:
            result = asyncio.run(
                self.provider.synthesize(
                    lines,
                    voice=params["voice"],
                    speed=params["speed"],
                    lang=params["lang"],
                    out_dir=str(out_dir),
                )
            )
        except Exception as exc:
            raise EngineError(
                "tts_failed", f"{type(exc).__name__}: {exc}", retryable=True
            ) from exc
        ctx.check_cancel()
        if len(result.chunks) != len(lines):
            raise EngineError(
                "incomplete_collection",
                f"음성 {len(result.chunks)}개, 기대 {len(lines)}개입니다.",
            )
        rate = result.chunks[0].sample_rate
        pcm: list[bytes] = [b"\x00\x00" * int(rate * params["lead_ms"] / 1000)]
        cursor = len(pcm[0]) // 2
        timings = []
        for index, chunk in enumerate(sorted(result.chunks, key=lambda c: c.index)):
            with wave.open(chunk.path) as source:
                if (
                    source.getframerate() != rate
                    or source.getsampwidth() != 2
                    or source.getnchannels() != 1
                ):
                    raise EngineError(
                        "format_mismatch",
                        "TTS 청크의 샘플레이트·채널·비트 깊이가 서로 다릅니다.",
                    )
                data = source.readframes(source.getnframes())
            samples = len(data) // 2
            if samples == 0:
                raise EngineEmptyOutput(f"{index}번째 발화가 무음 0샘플입니다.")
            timings.append(
                {
                    "line": index,
                    "text": lines[index],
                    "start_sample": cursor,
                    "end_sample": cursor + samples,
                }
            )
            pcm.append(data)
            cursor += samples
            gap = int(rate * params["gap_ms"] / 1000)
            if index < len(lines) - 1:
                pcm.append(b"\x00\x00" * gap)
                cursor += gap
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(b"".join(pcm))
        timing_doc = {
            "schema": "ttakkari.speech-timing.v1",
            "sample_rate": rate,
            "total_samples": cursor,
            "method": "tts-chunk-concatenation",
            "confidence": 1.0,
            "lines": timings,
        }
        return EngineRun(
            [
                EngineOutput("speech.wav", "audio/wav", buffer.getvalue(), "primary"),
                EngineOutput(
                    "speech-timing.json",
                    "application/json",
                    json.dumps(timing_doc, ensure_ascii=False).encode(),
                    "aux",
                ),
            ],
            metrics={
                "sample_rate": rate,
                "total_samples": cursor,
                "provider_metrics": dict(result.metrics),
            },
            provenance={
                "provider": getattr(self.provider, "id", "unknown"),
                "model_revision": self.provider.model_revision(),
                "voice": params["voice"],
                "speed": params["speed"],
            },
        )

    def release(self, ctx: ExecutionContext) -> None:
        shutil.rmtree(ctx.workdir, ignore_errors=True)


def read_wav_header(data: bytes) -> dict[str, int]:
    with wave.open(io.BytesIO(data)) as source:
        return {
            "sample_rate": source.getframerate(),
            "channels": source.getnchannels(),
            "samples": source.getnframes(),
            "sample_width": source.getsampwidth(),
        }
