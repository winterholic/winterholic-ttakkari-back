"""Supertonic 3 어댑터(부모 측). 모델 import 없이 파일 존재·설정만 본다."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ttakkari.config import Settings, get_settings
from ttakkari.tts import runner
from ttakkari.tts.providers import (
    ProviderAvailability,
    ProviderState,
    TtsCapabilities,
    TtsChunkResult,
    TtsResult,
    TtsVoice,
)

WORKER_MODULE = "ttakkari.tts.worker_supertonic"
_VOICE_FILE = re.compile(r"^([MF])(\d{1,2})\.json$")
_REQUIRED_ONNX = ("duration_predictor.onnx", "text_encoder.onnx", "vector_estimator.onnx", "vocoder.onnx",
                  "tts.json", "unicode_indexer.json")  # fmt: skip


class SupertonicProvider:
    id = "supertonic"
    label = "Supertonic 3 (ONNX, CPU)"
    # 샘플레이트·WAV 는 tts.json(ae.sample_rate=44100)과 워커 출력으로 확인했다. 속도 범위는 실행으로 확인한 값이다.
    capabilities = TtsCapabilities(chunked=True, speed_range=(0.5, 2.0), sample_rate=44100, output_format="wav")

    def __init__(self, settings: Settings | None = None):
        self._settings = settings

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def _model_dir(self) -> Path:
        return Path(self.settings.local_tts_model_path)

    def availability(self) -> ProviderAvailability:
        s = self.settings
        if not s.local_tts_enabled:
            return ProviderAvailability(ProviderState.disabled, "로컬 음성 합성이 꺼져 있어요.")
        model = self._model_dir()
        if not all((model / "onnx" / name).is_file() for name in _REQUIRED_ONNX):
            return ProviderAvailability(ProviderState.unconfigured, "로컬 Supertonic 모델 설치가 필요해요.")
        if not Path(s.local_tts_python).is_file():
            return ProviderAvailability(ProviderState.unconfigured, "로컬 음성 합성 실행 환경이 필요해요.")
        if not self.voices_sync():
            return ProviderAvailability(ProviderState.unconfigured, "사용할 수 있는 음성이 없어요.")
        return ProviderAvailability(ProviderState.ready, "이 Mac에서 Supertonic 3로 합성해요.")

    def model_revision(self) -> str:
        """모델 버전과 확산 단계 수. 어느 쪽이 바뀌어도 소리가 달라지므로 캐시 키에 들어간다."""
        version = "unknown"
        try:
            cfg = json.loads((self._model_dir() / "onnx" / "tts.json").read_text(encoding="utf-8"))
            version = str(cfg.get("tts_version", "unknown"))
        except (OSError, ValueError):
            pass
        return f"supertonic3-{version}-s{self.settings.local_tts_steps}"

    def voices_sync(self) -> list[TtsVoice]:
        folder = self._model_dir() / "voice_styles"
        voices = []
        if folder.is_dir():
            for path in folder.iterdir():
                m = _VOICE_FILE.match(path.name)
                if not m:
                    continue
                female = m.group(1) == "F"
                voices.append(
                    TtsVoice(
                        id=f"{m.group(1)}{int(m.group(2))}",
                        name=f"{'여성' if female else '남성'} {int(m.group(2))}",
                        lang=("ko", "en"),
                        gender="female" if female else "male",
                    )
                )
        return sorted(voices, key=lambda v: (v.gender or "", int(v.id[1:])))

    async def voices(self) -> list[TtsVoice]:
        return self.voices_sync()

    async def synthesize(
        self, chunks: list[str], *, voice: str, speed: float, lang: str, out_dir: str
    ) -> TtsResult:
        s = self.settings
        request = {
            "model_dir": str(self._model_dir()),
            "out_dir": out_dir,
            "voice": voice,
            "speed": speed,
            "steps": s.local_tts_steps,
            "lang": lang,
            "cpu_threads": s.local_tts_cpu_threads,
            "chunks": chunks,
        }
        result = await runner.run_worker(module=WORKER_MODULE, request=request, settings=s)
        items = result.get("chunks", [])
        if len(items) != len(chunks):
            raise RuntimeError("tts_worker_chunk_mismatch")
        base = Path(out_dir)
        out = []
        for item in items:
            path = base / item["file"]
            if not path.is_file():
                raise RuntimeError("tts_worker_chunk_missing")
            out.append(
                TtsChunkResult(
                    index=int(item["index"]), path=str(path), duration_seconds=float(item["duration_seconds"]),
                    sample_rate=int(item["sample_rate"]), bytes=int(item["bytes"]),
                )
            )
        return TtsResult(chunks=out, metrics=dict(result.get("metrics", {})))
