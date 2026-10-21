"""로컬 TTS ORM 모델(ADR 0062). tts_jobs 에는 원문을 두지 않는다(설계 §8.1)."""
from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow


class TtsJob(Base):
    """장문 비동기 합성 작업. params 에는 음성·속도·글자 수·청크 키만, 텍스트는 없다."""

    __tablename__ = "tts_jobs"
    __table_args__ = (Index("ix_tts_jobs_status_owner_created", "status", "owner_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # 접수한 세대(GenerationRuntime.id) 또는 세대 미사용 모드의 프로세스별 UUID. runtime_generations 에 FK 를 걸지 않는다.
    owner_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), default="queued")
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class TtsCache(Base):
    """내용 주소 청크 캐시 색인. 오디오 파일은 <home>/tts-cache/<shard2>/<key>.wav 에 있다."""

    __tablename__ = "tts_cache"
    __table_args__ = (Index("ix_tts_cache_last_hit_at", "last_hit_at"),)

    chunk_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    engine: Mapped[str] = mapped_column(String(40))
    model_revision: Mapped[str] = mapped_column(String(80))
    voice: Mapped[str] = mapped_column(String(40))
    speed: Mapped[float] = mapped_column(Float)
    chunk_hash: Mapped[str] = mapped_column(String(64))
    audio_relpath: Mapped[str] = mapped_column(String(120))
    bytes: Mapped[int] = mapped_column(BigInteger)
    sample_rate: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[float] = mapped_column(Float)
    hits: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_hit_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
