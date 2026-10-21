"""TTS 공용 서비스: 합성 계획, 실행 예산(직렬 1개), 캐시 미스 합성. /speak 와 큐 consumer 가 같이 쓴다."""
from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from ttakkari import generation
from ttakkari.config import Settings, get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import LocalSttJob
from ttakkari.tts import cache
from ttakkari.tts.normalize import chunk_text
from ttakkari.tts.providers import TtsProvider, TtsResult
from ttakkari.tts.supertonic import SupertonicProvider

# 세대 미사용 모드의 실행 예산. 세대 모드에서는 work-tts.lock 파일 잠금이 세대를 가로지르는 전역 1개다(설계 §6).
_execution_lock = asyncio.Lock()
_HANGUL = re.compile(r"[가-힣ㄱ-ㅣ]")
_LATIN = re.compile(r"[A-Za-z]")
BUDGET_POLL_SECONDS = 0.25


class TtsError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


def get_provider(settings: Settings | None = None) -> TtsProvider:
    s = settings or get_settings()
    if s.local_tts_engine == "supertonic":
        return SupertonicProvider(s)
    raise TtsError("tts_unavailable", "지원하지 않는 음성 엔진이에요.", 503)


def detect_lang(text: str) -> str:
    """한글이 하나라도 있으면 ko, 한글이 없고 영문이 있으면 en. 그 밖에는 ko."""
    if _HANGUL.search(text):
        return "ko"
    return "en" if _LATIN.search(text) else "ko"


@dataclass
class Plan:
    provider: TtsProvider
    voice: str
    speed: float
    lang: str
    chunks: list[str]
    keys: list[str] = field(default_factory=list)
    text_length: int = 0


def make_plan(text: str, voice: str | None, speed: float | None, *, settings: Settings | None = None) -> Plan:
    s = settings or get_settings()
    provider = get_provider(s)
    availability = provider.availability()
    if not availability.ready:
        raise TtsError("tts_unavailable", availability.reason or "음성 합성을 쓸 수 없어요.", 503)
    voice = voice or s.local_tts_default_voice
    speed = float(speed if speed is not None else s.local_tts_default_speed)
    lo, hi = provider.capabilities.speed_range
    if not lo <= speed <= hi:
        raise TtsError("invalid_speed", f"속도는 {lo}에서 {hi} 사이여야 해요.")
    if voice not in {v.id for v in provider.voices_sync()}:  # type: ignore[attr-defined]
        raise TtsError("unknown_voice", "알 수 없는 음성이에요.")
    chunks = chunk_text(text, s.local_tts_chunk_max_chars)
    if not chunks:
        raise TtsError("empty_text", "읽을 내용이 없어요.")
    lang = detect_lang(text)
    revision = provider.model_revision()
    keys = [
        cache.chunk_key(engine=provider.id, model_revision=revision, voice=voice, speed=speed, lang=lang, text=c)
        for c in chunks
    ]
    return Plan(provider, voice, speed, lang, chunks, keys, len(text))


async def _stt_running() -> bool:
    async with sessionmaker()() as db:
        return await db.scalar(select(LocalSttJob.id).where(LocalSttJob.status == "running").limit(1)) is not None


@asynccontextmanager
async def budget(wait_seconds: float):
    """실행 예산을 잡는다. 'ok' 면 얻은 것이고 'busy'/'not_active' 면 못 얻었다.

    세대 모드는 work("tts") 가 active 가 아니면 잠금을 잡지 않고 False 를 주므로(generation.work),
    draining 세대는 새 합성을 시작하지 않는다. defer_to_stt 정책은 STT 가 running 이면 예산을 주지 않는다.
    """
    s = get_settings()
    deadline = time.monotonic() + wait_seconds
    while True:
        if s.local_tts_concurrency_policy == "defer_to_stt" and await _stt_running():
            outcome = "busy"
        elif generation.current is None:
            outcome = "busy"
            if not _execution_lock.locked():
                async with _execution_lock:
                    yield "ok"
                return
        else:
            async with generation.work("tts") as acquired:
                if acquired:
                    yield "ok"
                    return
            outcome = "busy" if generation.current.state == "active" else "not_active"
        if outcome == "not_active" or time.monotonic() >= deadline:
            yield outcome
            return
        await asyncio.sleep(BUDGET_POLL_SECONDS)


def new_staging_dir() -> str:
    path = cache.staging_root() / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(path)


async def synthesize_missing(plan: Plan, missing: list[int]) -> TtsResult:
    """캐시에 없는 청크만 워커로 합성해 캐시에 넣는다. 호출자가 실행 예산을 쥐고 있어야 한다."""
    staging = new_staging_dir()
    try:
        texts = [plan.chunks[i] for i in missing]
        result = await plan.provider.synthesize(
            texts, voice=plan.voice, speed=plan.speed, lang=plan.lang, out_dir=staging
        )
        revision = plan.provider.model_revision()
        async with sessionmaker()() as db:
            for item, index in zip(result.chunks, missing, strict=True):
                await cache.store(
                    db,
                    key=plan.keys[index],
                    staged=Path(item.path),
                    engine=plan.provider.id,
                    model_revision=revision,
                    voice=plan.voice,
                    speed=plan.speed,
                    text=plan.chunks[index],
                    sample_rate=item.sample_rate,
                    duration=item.duration_seconds,
                )
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)
