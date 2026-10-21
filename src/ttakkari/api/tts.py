"""로컬 TTS API(ADR 0062). 모두 Bearer 인증(owner)이며 재생 링크(/audio/{token})만 서명 토큰으로 연다."""
from __future__ import annotations

import asyncio
from pathlib import Path

import jwt
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ttakkari import generation
from ttakkari.api.deps import Db
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.security.auth import User
from ttakkari.tts import cache, jobs, read_model, service
from ttakkari.tts.service import TtsError
from ttakkari.tts_models import TtsJob

router = APIRouter(prefix="/api/tts", tags=["tts"])
_AUDIO_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class SpeakIn(BaseModel):
    # 길이 상한은 설정(local_tts_*_max_chars)을 핸들러가 보고, 모델은 터무니없는 크기만 막는다.
    text: str = Field(min_length=1, max_length=100_000)
    voice: str | None = Field(default=None, max_length=40)
    speed: float | None = None


def _http(exc: TtsError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message}, headers=headers)


def _plan(body: SpeakIn, limit: int) -> service.Plan:
    if len(body.text) > limit:
        raise HTTPException(
            422,
            {"code": "text_too_long", "message": f"글자 수는 {limit}자까지예요."},
        )
    try:
        return service.make_plan(body.text, body.voice, body.speed)
    except TtsError as exc:
        raise _http(exc) from exc


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "음성 작업을 찾지 못했어요."})


@router.get("/status")
async def tts_status(_: User) -> dict:
    s = get_settings()
    provider = service.get_provider(s)
    availability = provider.availability()
    runtime = generation.current
    return {
        "enabled": s.local_tts_enabled,
        "ready": availability.ready,
        "reason": availability.reason,
        "engine": provider.id,
        "model": provider.model_revision(),
        "voice_count": len(provider.voices_sync()),  # type: ignore[attr-defined]
        "default_voice": s.local_tts_default_voice,
        "default_speed": s.local_tts_default_speed,
        "speed_range": list(provider.capabilities.speed_range),
        "sample_rate": provider.capabilities.sample_rate,
        "output_format": provider.capabilities.output_format,
        "max_text_chars": s.local_tts_max_text_chars,
        "speak_max_chars": s.local_tts_speak_max_chars,
        "accepting": runtime is None or runtime.state == "active",
    }


@router.get("/voices")
async def tts_voices(_: User) -> dict:
    provider = service.get_provider()
    voices = await provider.voices()
    return {
        "default": get_settings().local_tts_default_voice,
        "voices": [
            {"id": v.id, "name": v.name, "lang": list(v.lang), "gender": v.gender, "sample_url": None} for v in voices
        ],
    }


def _assemble(keys: list[str]) -> tuple[Path, float]:
    """청크 파일을 이어 붙인 전체 오디오 경로와 길이. 한 청크면 캐시 파일을 그대로 쓴다."""
    paths = [cache.path_for(k) for k in keys]
    if len(paths) == 1:
        return paths[0], cache.wav_duration(paths[0])[0]
    dest = read_model.full_audio_path(keys)
    if not dest.is_file():
        return dest, cache.concat_wavs(paths, dest)
    return dest, cache.wav_duration(dest)[0]


@router.post("/speak", response_model=None)
async def speak(body: SpeakIn, _: User) -> FileResponse:
    """단문 동기 합성. 캐시에 모두 있으면 워커를 띄우지 않고, 없으면 실행 예산을 최대 15초 기다린다."""
    s = get_settings()
    plan = _plan(body, s.local_tts_speak_max_chars)
    async with sessionmaker()() as db:
        found = await cache.lookup_many(db, plan.keys)
    cache.count_lookup(len(plan.keys), sum(1 for k in plan.keys if k in found))
    synthesized = 0
    if any(k not in found for k in plan.keys):
        async with service.budget(s.local_tts_speak_wait_seconds) as outcome:
            if outcome == "not_active":
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    {"code": "generation_not_active", "message": "배포 전환 중이에요. 잠시 후 다시 시도해 주세요."},
                    headers={"Retry-After": "5"},
                )
            if outcome != "ok":
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    {"code": "busy", "message": "다른 음성 합성이 진행 중이에요. 잠시 후 다시 시도해 주세요."},
                    headers={"Retry-After": "5"},
                )
            async with sessionmaker()() as db:
                found = await cache.lookup_many(db, plan.keys)  # 기다리는 사이 다른 요청이 채웠을 수 있다
            seen: set[str] = set()
            missing = []
            for index, key in enumerate(plan.keys):
                if key not in found and key not in seen:
                    seen.add(key)
                    missing.append(index)
            if missing:
                try:
                    await service.synthesize_missing(plan, missing)
                except asyncio.CancelledError:
                    raise
                except TimeoutError as exc:
                    raise HTTPException(
                        status.HTTP_504_GATEWAY_TIMEOUT, {"code": "worker_timeout", "message": "음성 합성이 너무 오래 걸렸어요."}
                    ) from exc
                except Exception as exc:  # 워커 오류 상세는 응답에 싣지 않는다
                    raise HTTPException(
                        status.HTTP_502_BAD_GATEWAY, {"code": "worker_failed", "message": "음성 합성에 실패했어요."}
                    ) from exc
                synthesized = len(missing)
    try:
        path, duration = await asyncio.to_thread(_assemble, plan.keys)
    except (OSError, ValueError, EOFError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, {"code": "worker_failed", "message": "합성된 오디오를 읽지 못했어요."}
        ) from exc
    headers = {
        **_AUDIO_HEADERS,
        "X-TTS-Chunks": str(len(plan.keys)),
        "X-TTS-Cached-Chunks": str(len(plan.keys) - synthesized),
        "X-TTS-Duration": f"{duration:.3f}",
        "X-TTS-Engine": plan.provider.id,
        "Content-Disposition": 'inline; filename="speech.wav"',
    }
    return FileResponse(path, media_type="audio/wav", headers=headers)


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_job(body: SpeakIn, _: User) -> dict:
    plan = _plan(body, get_settings().local_tts_max_text_chars)
    try:
        return await jobs.accept(plan)
    except TtsError as exc:
        raise _http(exc) from exc


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, _: User) -> dict:
    jid = jobs.parse_job_id(job_id)  # uuid.UUID 인자로 두면 전역 검증 핸들러가 422 를 낸다
    job = await jobs.get_job(jid) if jid else None
    if job is None:
        raise _not_found()
    return await read_model.job_out(job)


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str, _: User) -> dict:
    jid = jobs.parse_job_id(job_id)
    job = await jobs.cancel(jid) if jid else None
    if job is None:
        raise _not_found()
    return await read_model.job_out(job)


@router.get("/audio/{token}", response_model=None)
async def audio(token: str, db: Db, download: bool = False) -> FileResponse:
    """서명 토큰으로 여는 재생 링크(<audio> 용). 토큰 sub 가 청크 키 또는 job id 다."""
    try:
        sub = read_model.read_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, {"code": "link_expired", "message": "재생 링크가 만료되었거나 올바르지 않아요."}
        ) from exc
    kind, _, ref = sub.partition(":")
    if kind == "chunk":
        keys = [ref]
    elif kind == "job" and (jid := jobs.parse_job_id(ref)):
        job = await db.get(TtsJob, jid)
        if job is None or job.status != "succeeded":
            raise _not_found()
        keys = list(job.params.get("chunk_keys", []))
    else:
        raise _not_found()
    found = await cache.lookup_many(db, keys, touch=False)
    if not keys or any(k not in found for k in keys):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "audio_expired", "message": "오디오가 정리되었어요. 다시 생성해 주세요."})
    path, _duration = await asyncio.to_thread(_assemble, keys)
    disposition = 'attachment; filename="speech.wav"' if download else 'inline; filename="speech.wav"'
    return FileResponse(path, media_type="audio/wav", headers={**_AUDIO_HEADERS, "Content-Disposition": disposition})


@router.get("/cache/stats")
async def cache_stats(_: User, db: Db) -> dict:
    return await cache.stats(db)


@router.post("/cache/prune")
async def cache_prune(_: User, db: Db) -> dict:
    return await cache.prune(db)
