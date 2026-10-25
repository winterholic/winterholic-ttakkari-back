"""KB v3 자동 지식화 파이프라인 단계 처리기 레지스트리.

기본 레지스트리는 실제 처리기를 등록한다: parsing/chunking/extracting/linking/indexing.
canonicalizing(다문서 정본 통합·상충 감지)은 아직 구현이 없으므로 성공/ready 로 위장하지 않고 held(capability_missing)로 남긴다.
"""

from __future__ import annotations

import logging

from ttakkari.kb_pipeline.stages import (
    DefaultChunkingProcessor,
    DefaultIndexingProcessor,
    DefaultParsingProcessor,
    ExtractingProcessor,
    LinkingProcessor,
)
from ttakkari.kb_pipeline.types import (
    STAGE_CANONICALIZING,
    STAGE_CHUNKING,
    STAGE_EXTRACTING,
    STAGE_INDEXING,
    STAGE_LINKING,
    STAGE_PARSING,
    StageContext,
    StageProcessor,
    StageResult,
)

log = logging.getLogger(__name__)

__all__ = [
    "DefaultChunkingProcessor",
    "DefaultIndexingProcessor",
    "DefaultParsingProcessor",
    "ExtractingProcessor",
    "LinkingProcessor",
    "MissingStageProcessor",
    "StageProcessorRegistry",
]


class MissingStageProcessor:
    """아직 구현되지 않은 단계를 안전하게 보류(held) 또는 성능 저하(degraded)로 처리하는 프로세서."""

    def __init__(self, stage: str, as_degraded: bool = False) -> None:
        self.stage = stage
        self.as_degraded = as_degraded

    async def __call__(self, ctx: StageContext) -> StageResult:
        reason = f"capability_missing: no processor registered for stage '{self.stage}'"
        detail = {
            "missing_capability": self.stage,
            "processor_implemented": False,
        }
        log.info(
            "kb pipeline job %s stage %s held: %s",
            ctx.job_id,
            self.stage,
            reason,
        )
        if self.as_degraded:
            return StageResult.degraded(reason, detail=detail)
        return StageResult.held(reason, detail=detail)


class StageProcessorRegistry:
    """단계별 프로세서 등록 및 조회를 관리하는 레지스트리."""

    def __init__(self) -> None:
        self._processors: dict[str, StageProcessor] = {}
        self._init_defaults()

    def _init_defaults(self) -> None:
        self.register(STAGE_PARSING, DefaultParsingProcessor())
        self.register(STAGE_CHUNKING, DefaultChunkingProcessor())
        self.register(STAGE_EXTRACTING, ExtractingProcessor())
        self.register(STAGE_LINKING, LinkingProcessor())
        self.register(STAGE_INDEXING, DefaultIndexingProcessor())
        # 구현이 없는 단계는 held(capability_missing)로 남긴다.
        self.register(
            STAGE_CANONICALIZING, MissingStageProcessor(STAGE_CANONICALIZING)
        )

    def register(self, stage: str, processor: StageProcessor) -> None:
        self._processors[stage] = processor

    def get(self, stage: str) -> StageProcessor:
        if stage in self._processors:
            return self._processors[stage]
        return MissingStageProcessor(stage)
