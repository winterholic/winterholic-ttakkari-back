"""청크 단위 내용 주소 캐시(설계 §8.4). 키·조회·저장·정리는 부모 프로세스에서만 한다.

키: blake2b("tts2" ‖ engine ‖ model_revision ‖ voice ‖ speed.hex() ‖ lang ‖ normalize(chunk_text)).
설계 키에 lang 을 더했다. 같은 문장이라도 언어 태그가 다르면 다른 소리이기 때문이다.
오디오는 <home>/tts-cache/<shard2>/<key>.wav, 작업 중인 임시 파일은 <home>/tts-cache/tmp/ 에 둔다.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import wave
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.models import utcnow
from ttakkari.tts_models import TtsCache

KEY_VERSION = "tts2"
STAGING_STALE_SECONDS = 3600


@dataclass
class CacheCounters:
    hits: int = 0
    misses: int = 0
    since: float = 0.0

    def reset(self) -> None:
        self.hits = self.misses = 0
        self.since = time.time()


counters = CacheCounters(since=time.time())


def cache_root() -> Path:
    return get_settings().home / "tts-cache"


def staging_root() -> Path:
    return cache_root() / "tmp"


def full_root() -> Path:
    return cache_root() / "full"


def text_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def chunk_key(*, engine: str, model_revision: str, voice: str, speed: float, lang: str, text: str) -> str:
    """NUL 로 필드를 구분해 필드 경계가 달라도 같은 이어붙임이 되는 충돌을 막는다. text 는 정규화를 마친 청크다."""
    material = "\0".join((KEY_VERSION, engine, model_revision, voice, float(speed).hex(), lang, text))
    return hashlib.blake2b(material.encode("utf-8"), digest_size=16).hexdigest()


def relpath(key: str) -> str:
    return f"{key[:2]}/{key}.wav"


def path_for(key: str) -> Path:
    return cache_root() / relpath(key)


def wav_duration(path: Path) -> tuple[float, int]:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate(), handle.getframerate()


async def lookup_many(db: AsyncSession, keys: list[str], *, touch: bool = True) -> dict[str, TtsCache]:
    """색인과 파일이 모두 있는 키만 돌려준다. 파일이 사라진 색인은 지운다(캐시는 언제든 재생성 가능)."""
    if not keys:
        return {}
    rows = list(await db.scalars(select(TtsCache).where(TtsCache.chunk_key.in_(set(keys)))))
    found: dict[str, TtsCache] = {}
    stale: list[str] = []
    for row in rows:
        if path_for(row.chunk_key).is_file():
            found[row.chunk_key] = row
        else:
            stale.append(row.chunk_key)
    if stale:
        await db.execute(delete(TtsCache).where(TtsCache.chunk_key.in_(stale)))
    if touch and found:
        await db.execute(
            update(TtsCache)
            .where(TtsCache.chunk_key.in_(list(found)))
            .values(hits=TtsCache.hits + 1, last_hit_at=utcnow())
        )
    await db.commit()
    return found


def count_lookup(requested: int, found: int) -> None:
    counters.hits += found
    counters.misses += requested - found


async def store(
    db: AsyncSession,
    *,
    key: str,
    staged: Path,
    engine: str,
    model_revision: str,
    voice: str,
    speed: float,
    text: str,
    sample_rate: int,
    duration: float,
) -> None:
    """임시 파일을 같은 파일시스템 안에서 rename 해 넣고 색인을 쓴다. 같은 키가 이미 있으면 기존 것을 유지한다."""
    dest = path_for(key)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.replace(staged, dest)
    now = utcnow()
    await db.execute(
        insert(TtsCache)
        .values(
            chunk_key=key, engine=engine, model_revision=model_revision, voice=voice, speed=speed,
            chunk_hash=text_hash(text), audio_relpath=relpath(key), bytes=dest.stat().st_size,
            sample_rate=sample_rate, duration_seconds=duration, hits=0, created_at=now, last_hit_at=now,
        )
        .on_conflict_do_update(
            index_elements=[TtsCache.chunk_key],
            set_={"bytes": dest.stat().st_size, "sample_rate": sample_rate, "duration_seconds": duration},
        )
    )
    await db.commit()


def concat_wavs(paths: list[Path], dest: Path) -> float:
    """같은 샘플레이트·형식의 WAV 청크를 이어 붙여 dest 에 쓴다. 청크 끝에 무음이 이미 들어 있다."""
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = dest.with_suffix(f".{os.getpid()}.part")
    frames = 0
    with wave.open(str(tmp), "wb") as out:
        params = None
        for path in paths:
            with wave.open(str(path), "rb") as src:
                current = (src.getnchannels(), src.getsampwidth(), src.getframerate())
                if params is None:
                    params = current
                    out.setnchannels(current[0])
                    out.setsampwidth(current[1])
                    out.setframerate(current[2])
                elif current != params:
                    raise ValueError("wav_format_mismatch")
                data = src.readframes(src.getnframes())
                frames += src.getnframes()
                out.writeframes(data)
    os.replace(tmp, dest)
    return frames / params[2] if params else 0.0


async def stats(db: AsyncSession) -> dict:
    entries, size = (
        await db.execute(select(func.count(TtsCache.chunk_key), func.coalesce(func.sum(TtsCache.bytes), 0)))
    ).one()
    return {
        "entries": int(entries),
        "bytes": int(size),
        "hits": counters.hits,
        "misses": counters.misses,
        "counters_since": counters.since,
        "max_bytes": int(get_settings().local_tts_cache_max_gb * 1024**3),
    }


async def prune(db: AsyncSession, *, now=None) -> dict:
    """TTL 경과분과 상한 초과분(LRU)을 지운다. 색인이 없는 고아 파일과 오래된 임시·전체 파일도 치운다."""
    s = get_settings()
    now = now or utcnow()
    removed = freed = 0

    async def drop(rows) -> tuple[int, int]:
        n = size = 0
        keys = []
        for key, nbytes in rows:
            path = path_for(key)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            keys.append(key)
            n += 1
            size += int(nbytes)
        if keys:
            await db.execute(delete(TtsCache).where(TtsCache.chunk_key.in_(keys)))
            await db.commit()
        return n, size

    expired = (
        await db.execute(
            select(TtsCache.chunk_key, TtsCache.bytes).where(
                TtsCache.last_hit_at < now - timedelta(days=s.local_tts_cache_ttl_days)
            )
        )
    ).all()
    n, size = await drop(expired)
    removed, freed = removed + n, freed + size

    limit = int(s.local_tts_cache_max_gb * 1024**3)
    total = int((await db.scalar(select(func.coalesce(func.sum(TtsCache.bytes), 0)))) or 0)
    if total > limit:
        victims = []
        for key, nbytes in (
            await db.execute(select(TtsCache.chunk_key, TtsCache.bytes).order_by(TtsCache.last_hit_at, TtsCache.created_at))
        ).all():
            if total <= limit:
                break
            victims.append((key, nbytes))
            total -= int(nbytes)
        n, size = await drop(victims)
        removed, freed = removed + n, freed + size

    freed += _sweep_files(now)
    return {"removed": removed, "freed_bytes": freed}


def _sweep_files(now) -> int:
    """오래된 작업 임시 디렉터리와 이어 붙인 전체 오디오를 지운다. 캐시 청크 자체는 색인 기준으로만 지운다."""
    freed = 0
    ttl_full = get_settings().local_tts_cache_ttl_days * 86400
    for root, ttl in ((staging_root(), STAGING_STALE_SECONDS), (full_root(), ttl_full)):
        if not root.is_dir():
            continue
        limit = now.timestamp() - ttl
        for child in root.iterdir():
            try:
                if child.stat().st_mtime >= limit:
                    continue
                if child.is_dir():
                    freed += sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    freed += child.stat().st_size
                    child.unlink(missing_ok=True)
            except OSError:
                continue
    return freed
