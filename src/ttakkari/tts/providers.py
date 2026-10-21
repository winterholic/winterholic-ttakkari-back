"""TTS 엔진 추상화(설계 §4.1). 부모 프로세스는 이 Protocol 만 보고 엔진 이름을 큐·캐시·API 에 새기지 않는다."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol, runtime_checkable


class ProviderState(enum.StrEnum):
    ready = "ready"
    unconfigured = "unconfigured"
    disabled = "disabled"


@dataclass(frozen=True)
class ProviderAvailability:
    state: ProviderState
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.state == ProviderState.ready


class TtsCapabilities(NamedTuple):
    chunked: bool
    speed_range: tuple[float, float]
    sample_rate: int
    output_format: str


@dataclass(frozen=True)
class TtsVoice:
    id: str
    name: str
    lang: tuple[str, ...]
    gender: str | None = None


@dataclass
class TtsChunkResult:
    """워커가 만든 청크 한 개. 원문은 담지 않는다(DB 에 남을 수 있는 값만)."""

    index: int
    path: str
    duration_seconds: float
    sample_rate: int
    bytes: int


@dataclass
class TtsResult:
    chunks: list[TtsChunkResult]
    metrics: dict = field(default_factory=dict)


@runtime_checkable
class TtsProvider(Protocol):
    id: str
    label: str
    capabilities: TtsCapabilities

    def availability(self) -> ProviderAvailability: ...

    def model_revision(self) -> str:
        """캐시 키에 들어가는 모델·품질 파라미터 식별자. 바뀌면 캐시가 섞이지 않는다."""
        ...

    async def voices(self) -> list[TtsVoice]: ...

    async def synthesize(
        self, chunks: list[str], *, voice: str, speed: float, lang: str, out_dir: str
    ) -> TtsResult: ...
