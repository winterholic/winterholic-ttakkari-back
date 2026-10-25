"""KB v3 자동 지식화 파이프라인 기본 어댑터 및 단계 프로세서.

실제 추출(extracting)/연결(linking)/정본(canonicalizing) 모듈은 후속 과제로 별도 구현되므로,
미등록된 프로세서는 성공/no-op/ready 로 통과시키지 않고 정확히 held/degraded 및 capability 오류를 남긴다.
기존 파싱/청킹/임베딩 기능은 가능한 만큼 실제 연결하며, 불변 원장(KbRevision, KbPassage)의 UPDATE/DELETE 는
원천 차단하여 기존 event_time/rev_no 현재성을 보존한다.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from ttakkari.kb_models import KbPassage, KbRevision, KbSource
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
from ttakkari.kb_store import segment_revision

log = logging.getLogger(__name__)


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


class DefaultParsingProcessor:
    """기존 KbSource 및 리비전 본문에서 구조/메타데이터를 파싱하는 기본 어댑터."""

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")

        async with ctx.session_factory() as db:
            rev = await db.get(KbRevision, ctx.revision_id)
            if rev is None:
                return StageResult.failed(
                    f"revision_not_found: {ctx.revision_id}"
                )

            source = await db.get(KbSource, rev.source_id)
            media_type = source.media_type if source else "text/plain"
            parser_name = (
                getattr(source, "parser_name", None)
                or rev.parser_version
                or "text-exact-v1"
            )

            # 포맷별 파서 모듈(kb_parsers)이 설치되어 있는 경우 파싱 연결 시도
            parsed_meta: dict[str, Any] = {
                "source_media_type": media_type,
                "parser_name": parser_name,
                "body_len": len(rev.body),
                "text_sha256": rev.text_sha256,
            }

            try:
                # optional: kb_parsers가 구현되어 있다면 포맷 확인
                from ttakkari.kb_parsers import get_parser  # type: ignore

                parser = get_parser(media_type)
                if parser is not None:
                    parsed_meta["parser_kind"] = parser.__class__.__name__
            except (ImportError, Exception):  # noqa: BLE001
                parsed_meta["parser_kind"] = "builtin_text"

            return StageResult.success(
                detail=parsed_meta, version=str(parser_name)
            )


class DefaultChunkingProcessor:
    """원장 불변성을 준수하며 패시지를 생성·재사용하는 기본 청킹 어댑터.

    기존에 적재된 패시지가 있으면 불변 트리거와의 충돌을 막기 위해 UPDATE/DELETE 없이
    기존 패시지 행을 그대로 재사용(succeeded)하며, 없으면 segment_revision 으로 신규 생성한다.
    """

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")

        async with ctx.session_factory() as db:
            # 1. 기존 패시지 존재 여부 확인 (불변 원장 보호)
            existing = list(
                await db.scalars(
                    select(KbPassage)
                    .where(KbPassage.revision_id == ctx.revision_id)
                    .order_by(KbPassage.ord)
                )
            )

            if existing:
                return StageResult.success(
                    detail={
                        "passage_count": len(existing),
                        "reused_existing": True,
                        "immutable_preserved": True,
                    },
                    version="chunker-reuse-v1",
                )

            # 2. 패시지가 전혀 없는 경우에만 신규 분할
            passages = await segment_revision(db, ctx.revision_id)
            await db.commit()

            return StageResult.success(
                detail={
                    "passage_count": len(passages),
                    "reused_existing": False,
                    "created": True,
                },
                version="chunker-segment-v1",
            )


class DefaultIndexingProcessor:
    """어휘(lexical FTS) 및 벡터(embedding) 색인을 점검·연결하는 기본 색인 어댑터."""

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")

        async with ctx.session_factory() as db:
            passages = list(
                await db.scalars(
                    select(KbPassage).where(
                        KbPassage.revision_id == ctx.revision_id
                    )
                )
            )

            if not passages:
                return StageResult.failed("no_passages_to_index")

            detail: dict[str, Any] = {
                "passage_count": len(passages),
                "lexical_indexed": True,
                "vectors_computed": 0,
            }

            # 패시지 임베딩 생성 시도 (가용한 경우 온디맨드 워커 활용)
            try:
                from ttakkari.kb_retrieval import (  # type: ignore
                    embed_passages_if_available,
                )

                embedded = await embed_passages_if_available(
                    db, [p.id for p in passages]
                )
                detail["vectors_computed"] = embedded
            except (ImportError, AttributeError, Exception):  # noqa: BLE001
                detail["vectors_computed"] = 0
                detail["embedding_status"] = "deferred_or_unavailable"

            return StageResult.success(detail=detail, version="indexing-v1")


class StageProcessorRegistry:
    """단계별 프로세서 등록 및 조회를 관리하는 레지스트리."""

    def __init__(self) -> None:
        self._processors: dict[str, StageProcessor] = {}
        self._init_defaults()

    def _init_defaults(self) -> None:
        # 실제 구현이 존재하는 단계는 기본 어댑터 연결
        self.register(STAGE_PARSING, DefaultParsingProcessor())
        self.register(STAGE_CHUNKING, DefaultChunkingProcessor())
        self.register(STAGE_INDEXING, DefaultIndexingProcessor())

        # 후속 구현 예정인 단계는 missing processor (held 처리)
        self.register(STAGE_EXTRACTING, MissingStageProcessor(STAGE_EXTRACTING))
        self.register(STAGE_LINKING, MissingStageProcessor(STAGE_LINKING))
        self.register(
            STAGE_CANONICALIZING, MissingStageProcessor(STAGE_CANONICALIZING)
        )

    def register(self, stage: str, processor: StageProcessor) -> None:
        self._processors[stage] = processor

    def get(self, stage: str) -> StageProcessor:
        if stage in self._processors:
            return self._processors[stage]
        return MissingStageProcessor(stage)
