"""TTS 테스트 헬퍼(DB 행 삽입·조회). 픽스처는 conftest_tts.py 에 있다."""
from __future__ import annotations

import uuid
from datetime import timedelta

from tts_fakes import FakeProvider

from ttakkari.db import sessionmaker
from ttakkari.models import utcnow
from ttakkari.tts import service
from ttakkari.tts_models import TtsJob


async def insert_job(
    *, owner: uuid.UUID, status: str = "queued", age_seconds: float = 0, keys: list[str] | None = None,
    error_code: str | None = None,
) -> uuid.UUID:  # fmt: skip
    job_id = uuid.uuid4()
    keys = keys if keys is not None else [uuid.uuid4().hex]
    async with sessionmaker()() as db:
        db.add(
            TtsJob(
                id=job_id, owner_id=owner, status=status, error_code=error_code,
                params={"voice": "M1", "speed": 1.0, "lang": "ko", "text_length": 3, "chunk_count": len(keys),
                        "chunk_keys": keys, "cached_chunks": 0},
                created_at=utcnow() - timedelta(seconds=age_seconds),
            )
        )  # fmt: skip
        await db.commit()
    return job_id


async def job_row(job_id: uuid.UUID) -> TtsJob:
    async with sessionmaker()() as db:
        return await db.get(TtsJob, job_id, populate_existing=True)


def plan_for(provider: FakeProvider, text: str, *, voice: str = "M1", speed: float = 1.0) -> service.Plan:
    return service.make_plan(text, voice, speed)
