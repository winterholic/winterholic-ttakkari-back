"""전사(STT) Provider 인터페이스(ADR 0010·0021). 분석은 전사 텍스트만 받으므로 Provider 를 바꿔도 분석 로직은 그대로다.

등록된 것은 클로바노트 하나다. 실제 브라우저 자동화는 meetings/clovanote.py, 작업 큐는 meetings/clova_jobs.py 에 있다.
submit 은 업로드 작업을 큐에 넣고 작업 id 를 돌려주며, poll 은 그 작업 행을 읽는다(빠르게 돌아온다).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ttakkari.config import get_settings


class ProviderState(enum.StrEnum):
    ready = "ready"
    unconfigured = "unconfigured"  # 구현은 있으나 설정(예: 브라우저 프로필 경로)이 없다
    not_implemented = "not_implemented"  # 자리만 있고 구현이 없다


@dataclass(frozen=True)
class ProviderAvailability:
    state: ProviderState
    message: str  # 사용자에게 그대로 보여 주는 문구


@dataclass(frozen=True)
class AudioInput:
    """Provider 에 넘기는 녹음. 관리 영역 복사본이 아니라 검증을 마친 로컬 경로다."""

    path: Path
    filename: str
    mime_type: str
    meeting_id: uuid.UUID | None = None
    auto_analyze: bool = True


@dataclass(frozen=True)
class TranscriptionPoll:
    state: Literal["pending", "done", "failed"]
    text: str | None = None
    error: str | None = None


class ProviderNotAvailable(Exception):
    """아직 쓸 수 없는 Provider 를 불렀다. 메시지는 사용자에게 보인다."""


class TranscriptionProvider(Protocol):
    id: str
    label: str

    def availability(self) -> ProviderAvailability: ...

    async def submit(self, audio: AudioInput) -> str:
        """전사를 요청하고 Provider 쪽 작업 식별자를 돌려준다."""
        ...

    async def poll(self, job_id: str) -> TranscriptionPoll: ...


class ClovaNoteProvider:
    """클로바노트. 인증은 사람이 전용 Chrome 프로필에 한 번 로그인해 두는 방식이고(clovanote.py 주석),
    따까리는 비밀번호를 어디에도 두지 않는다. 로그인 상태는 마지막 확인 결과(status_cache)로 판단한다."""

    id = "clovanote"
    label = "클로바노트"

    def availability(self) -> ProviderAvailability:
        from ttakkari.meetings.clovanote import client

        if not get_settings().clovanote_enabled:
            return ProviderAvailability(ProviderState.unconfigured, "클로바노트 연동이 꺼져 있어요.")
        st = client.status_cache
        if st is not None and st.state == "login_required":
            return ProviderAvailability(ProviderState.unconfigured, "클로바노트 로그인이 필요해요. 회의록 화면 위의 '재로그인'을 눌러 주세요.")
        return ProviderAvailability(ProviderState.ready, "클로바노트로 전사해요.")

    @property
    def profile_configured(self) -> bool:
        return get_settings().clovanote_profile.exists()

    async def submit(self, audio: AudioInput) -> str:
        from ttakkari.db import sessionmaker
        from ttakkari.meetings import clova_jobs

        if audio.meeting_id is None:
            raise ProviderNotAvailable("회의록 없이 클로바노트에 올릴 수 없어요.")
        async with sessionmaker()() as db:
            job = await clova_jobs.enqueue(db, "upload", meeting_id=audio.meeting_id, params={"auto_analyze": audio.auto_analyze})
            await db.commit()
            job_id = str(job.id)
        clova_jobs.kick()
        return job_id

    async def poll(self, job_id: str) -> TranscriptionPoll:
        from sqlalchemy import select

        from ttakkari.db import sessionmaker
        from ttakkari.models import ClovaJob, ClovaNote

        async with sessionmaker()() as db:
            job = await db.get(ClovaJob, uuid.UUID(job_id))
            if job is None:
                return TranscriptionPoll("failed", error="클로바노트 작업을 찾지 못했어요.")
            if job.status == "failed":
                return TranscriptionPoll("failed", error=job.error or "클로바노트 처리에 실패했어요.")
            if job.status != "succeeded":
                return TranscriptionPoll("pending")
            note = await db.scalar(select(ClovaNote).where(ClovaNote.note_id == job.note_id)) if job.note_id else None
        if note is None or not note.transcript:
            return TranscriptionPoll("failed", error="클로바노트 전사를 받지 못했어요.")
        return TranscriptionPoll("done", text=note.transcript)


# 새 Provider 는 여기에 등록한다. 테스트는 가짜 Provider 를 끼워 파이프라인을 확인한다.
_REGISTRY: dict[str, TranscriptionProvider] = {"clovanote": ClovaNoteProvider()}
DEFAULT_PROVIDER = "clovanote"


def get_provider(provider_id: str | None = None) -> TranscriptionProvider:
    p = _REGISTRY.get(provider_id or DEFAULT_PROVIDER)
    if p is None:
        raise ProviderNotAvailable("알 수 없는 전사 Provider 예요.")
    return p


def register_provider(p: TranscriptionProvider) -> None:
    _REGISTRY[p.id] = p


def unregister_provider(provider_id: str) -> None:
    _REGISTRY.pop(provider_id, None)


def describe_providers() -> list[dict[str, object]]:
    """API 로 내보내는 요약. 경로·인증 정보는 내보내지 않고 설정 여부만 알린다."""
    out = []
    for p in _REGISTRY.values():
        a = p.availability()
        out.append({"id": p.id, "label": p.label, "state": a.state.value, "message": a.message,
                    "profile_configured": bool(getattr(p, "profile_configured", False))})
    return out
