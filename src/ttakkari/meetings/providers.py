"""Local transcription providers. No model is loaded in the application process."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


class ProviderState(enum.StrEnum):
    ready = "ready"
    unconfigured = "unconfigured"  # 구현은 있으나 설정(예: 로컬 모델 경로)이 없다
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


# No model import/load here; the local adapter only supervises a short-lived subprocess.
from ttakkari.meetings.local_stt.jobs import LocalWhisperProvider

_REGISTRY: dict[str, TranscriptionProvider] = {"local_whisper": LocalWhisperProvider()}
DEFAULT_PROVIDER = "local_whisper"


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
        out.append(
            {
                "id": p.id,
                "label": p.label,
                "state": a.state.value,
                "message": a.message,
                "is_default": p.id == DEFAULT_PROVIDER,
            }
        )
    return out
