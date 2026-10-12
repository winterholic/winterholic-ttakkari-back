"""전사(STT) Provider 인터페이스(ADR 0010). 분석은 전사 텍스트만 받으므로 Provider 를 바꿔도 분석 로직은 그대로다.

지금 등록된 것은 클로바노트 하나이고 구현되지 않았다. 네이버 화면 구성·자동화 가능 범위·업로드 제한은 가정하지 않는다.
실제 자동화는 기존 브라우저 제어 기능과 합쳐 `ClovaNoteProvider` 의 submit/poll 을 채우는 별도 작업이다.
"""

from __future__ import annotations

import enum
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
    """클로바노트(네이버) 어댑터 자리. 인증은 사람이 브라우저 프로필에 한 번 로그인해 두는 방식을 염두에 두고,
    따까리는 비밀번호·쿠키를 코드·DB·응답 어디에도 두지 않는다. 설정은 clovanote_profile_dir 뿐이다."""

    id = "clovanote"
    label = "클로바노트"

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(ProviderState.not_implemented, "클로바노트 연동 준비 중이에요. 전사 텍스트를 직접 넣으면 분석할 수 있어요.")

    @property
    def profile_configured(self) -> bool:
        return get_settings().clovanote_profile_dir is not None

    async def submit(self, audio: AudioInput) -> str:
        raise ProviderNotAvailable(self.availability().message)

    async def poll(self, job_id: str) -> TranscriptionPoll:
        raise ProviderNotAvailable(self.availability().message)


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
