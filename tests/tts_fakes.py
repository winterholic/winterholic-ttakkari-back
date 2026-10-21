"""TTS 테스트용 가짜 엔진. 실제 모델 없이 WAV 를 만들어 큐·캐시·API 계약을 시험한다."""
from __future__ import annotations

import asyncio
import struct
import wave
from pathlib import Path

from ttakkari.tts.providers import (
    ProviderAvailability,
    ProviderState,
    TtsCapabilities,
    TtsChunkResult,
    TtsResult,
    TtsVoice,
)

RATE = 8000


def write_wav(path: Path, seconds: float = 0.5, rate: int = RATE) -> int:
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", (i % 50) * 100) for i in range(frames)))
    return frames


class FakeProvider:
    id = "fake"
    label = "Fake"
    capabilities = TtsCapabilities(chunked=True, speed_range=(0.5, 2.0), sample_rate=RATE, output_format="wav")

    def __init__(self, *, delay: float = 0.0, fail: Exception | None = None, revision: str = "fake-1"):
        self.calls: list[list[str]] = []
        self.delay = delay
        self.fail = fail
        self.revision = revision
        self.started = asyncio.Event()

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(ProviderState.ready, "fake")

    def model_revision(self) -> str:
        return self.revision

    def voices_sync(self) -> list[TtsVoice]:
        return [TtsVoice("M1", "남성 1", ("ko", "en"), "male"), TtsVoice("F1", "여성 1", ("ko", "en"), "female")]

    async def voices(self) -> list[TtsVoice]:
        return self.voices_sync()

    async def synthesize(self, chunks, *, voice, speed, lang, out_dir) -> TtsResult:
        self.calls.append(list(chunks))
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        out = []
        for index, _text in enumerate(chunks):
            path = Path(out_dir) / f"chunk-{index:04d}.wav"
            frames = write_wav(path, 0.25)
            out.append(TtsChunkResult(index, str(path), frames / RATE, RATE, path.stat().st_size))
        return TtsResult(out, {"synth_seconds": 0.1, "audio_seconds": 0.25 * len(chunks), "rtf": 0.4,
                               "worker_started_epoch": 1.0, "first_chunk_epoch": 1.5})
