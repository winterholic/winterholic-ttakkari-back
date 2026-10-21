"""조회용 조립: job·청크·재생 링크. 재생 링크는 <audio> 가 Authorization 헤더를 못 싣는 점을 위한 단기 서명 토큰이다."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.security.auth import ALGO, ISSUER, decode
from ttakkari.tts import cache
from ttakkari.tts_models import TtsJob

AUDIENCE = "tts-audio"
# 같은 입력을 다시 보내면 해결될 수 있는 실패. worker_failed 는 입력 문제일 수 있어 재시도가 무의미할 수 있다.
RETRYABLE = frozenset(
    {"input_ttl_expired", "generation_draining", "owner_shutdown", "owner_gone", "server_restarted", "input_missing",
     "worker_timeout"}
)  # fmt: skip


def issue_token(sub: str) -> tuple[str, datetime]:
    s = get_settings()
    exp = datetime.now(UTC) + timedelta(seconds=s.local_tts_token_ttl_seconds)
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "exp": exp, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, s.jwt_secret, algorithm=ALGO), exp


def read_token(token: str) -> str:
    """유효하면 sub 를, 아니면 jwt.PyJWTError 를 낸다."""
    return str(decode(token, AUDIENCE)["sub"])


def chunk_url(key: str) -> str:
    return f"/api/tts/audio/{issue_token('chunk:' + key)[0]}"


def job_url(job_id: uuid.UUID) -> str:
    return f"/api/tts/audio/{issue_token('job:' + str(job_id))[0]}"


def full_audio_path(keys: list[str]):
    digest = hashlib.blake2b("\0".join(keys).encode(), digest_size=16).hexdigest()
    return cache.full_root() / f"{digest}.wav"


async def job_out(job: TtsJob) -> dict:
    params, meta = job.params or {}, job.result_metadata or {}
    keys = list(params.get("chunk_keys", []))
    async with sessionmaker()() as db:
        rows = await cache.lookup_many(db, keys, touch=False)
    chunks = []
    for index, key in enumerate(keys):
        item: dict = {"index": index, "text_hash": key}
        if key in rows:
            item["audio_url"] = chunk_url(key)
            item["duration"] = rows[key].duration_seconds
        chunks.append(item)
    done = sum(1 for k in keys if k in rows)
    out: dict = {
        "id": str(job.id),
        "status": job.status,
        "voice": params.get("voice"),
        "speed": params.get("speed"),
        "progress": {"done": done, "total": len(keys)},
        "chunks": chunks,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if job.status == "failed":
        out["error_code"] = job.error_code
        out["retryable"] = job.error_code in RETRYABLE
    if job.status == "succeeded":
        if keys and done == len(keys):
            out["audio_full_url"] = job_url(job.id)
            out["duration"] = sum(rows[k].duration_seconds for k in keys)
        if meta.get("metrics"):
            out["metrics"] = meta["metrics"]
        out["cached_chunks"] = meta.get("cached_chunks", params.get("cached_chunks"))
        out["synthesized_chunks"] = meta.get("synthesized_chunks")
    return out
