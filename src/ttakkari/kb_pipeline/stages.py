"""KB v3 파이프라인 단계 처리기(parsing / chunking / extracting / linking / indexing).

규칙
- 모델 호출·임베딩 같은 오래 걸리는 계산은 DB 트랜잭션 밖에서 끝낸 뒤, 쓰기는 ctx.fenced_session() 한 번으로 한다.
  (같은 트랜잭션에서 잡 행을 잠그고 attempt_token 을 확인한 뒤 커밋한다. 옛 실행자의 쓰기는 StaleWriterError 로 거부된다.)
- 구현이 없거나 환경이 못 하는 일은 성공으로 위장하지 않고 held/degraded 에 사유(capability)를 남긴다.
- 불변 원장(KbRevision, KbPassage)은 UPDATE/DELETE 하지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.kb_models import KbPassage, KbRevision, KbSource
from ttakkari.kb_pipeline.types import (
    STAGE_EXTRACTING,
    STAGE_STATUS_SUCCEEDED,
    StageContext,
    StageResult,
)
from ttakkari.kb_store import segment_revision

log = logging.getLogger(__name__)

# 원본이 바이너리 포맷인데 리비전 본문이 원시 바이트 디코딩(raw-v1)이면 본문이 쓰레기다. 파서 리비전이 먼저 있어야 한다.
_RAW_PARSER_PREFIX = "raw"


class DefaultParsingProcessor:
    """리비전 본문을 파서 계약(ParsedDocument)으로 검증하고 구조 요약을 남긴다.

    - 텍스트 계열: 본문을 마크다운 구조 파서로 파싱해 위치 스팬(locator_spans)을 계산하고 경계/슬라이스 불변식을 검증한다.
    - 바이너리 계열(PDF/Office/오디오/이미지) 원본인데 본문이 원시 디코딩이면 held('parser_revision_required'):
      파서 출력으로 만든 리비전이 아직 이 경로에 없다는 뜻이며 성공으로 취급하지 않는다.
    """

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")

        from ttakkari.kb_parsers import (
            DocxParser,
            ImageParser,
            LegacyOfficeParser,
            MarkdownParser,
            MediaAudioParser,
            PdfParser,
            PptxParser,
            XlsxParser,
            create_default_registry,
            validate_parsed_document,
        )

        async with ctx.session_factory() as db:
            rev = await db.get(KbRevision, ctx.revision_id)
            if rev is None:
                return StageResult.failed(f"revision_not_found: {ctx.revision_id}")
            source = await db.get(KbSource, rev.source_id)
            body = rev.body or ""
            parser_version = rev.parser_version or ""
            media_type = (source.media_type if source else None) or "text/plain"
            filename = (
                (getattr(source, "original_filename", None) or getattr(source, "locator", None))
                if source
                else None
            )

        if not body.strip():
            return StageResult.failed("empty_revision_body")

        # Older Collector revisions stored trafilatura plain text without heading levels. Parsing
        # that normalized text as though structure still existed would report a false success.
        if (
            media_type.split(";", 1)[0].strip().lower() in ("text/html", "application/xhtml+xml")
            and parser_version.startswith("trafilatura-")
            and not parser_version.endswith("+parse-2")
        ):
            return StageResult.held(
                "collector_html_structure_revision_required",
                detail={
                    "missing_capability": "collector_html_structure_revision_required",
                    "parser_version": parser_version,
                    "structure_recoverable_from_revision_body": False,
                    "raw_reprocess_required": True,
                },
            )

        binary_parsers = (
            PdfParser, DocxParser, PptxParser, XlsxParser, LegacyOfficeParser, MediaAudioParser, ImageParser,
        )
        found = create_default_registry().find_parser(content_type=media_type, filename=filename)
        if isinstance(found, binary_parsers) and parser_version.startswith(_RAW_PARSER_PREFIX):
            return StageResult.held(
                "parser_revision_required: binary source has only a raw-decoded revision",
                detail={
                    "missing_capability": "parser_revision_required",
                    "media_type": media_type,
                    "parser_version": parser_version,
                },
            )

        parsed = validate_parsed_document(
            MarkdownParser().parse(body.encode("utf-8"), content_type="text/markdown")
        )
        if parsed.status != "succeeded":
            return StageResult.failed(
                f"parse_{parsed.error_code or 'failed'}: {parsed.error_detail or ''}"[:500]
            )

        headings = sum(1 for sp in parsed.locator_spans if sp.chunk_kind == "heading")
        return StageResult.success(
            detail={
                "source_media_type": media_type,
                "parser_kind": "structure_markdown",
                "revision_parser_version": parser_version,
                "body_len": len(body),
                "locator_spans": len(parsed.locator_spans),
                "heading_spans": headings,
            },
            version="structure-md-v1",
        )


class DefaultChunkingProcessor:
    """원장 불변성을 지키며 구조 보존 패시지를 만들거나 재사용한다.

    기존 패시지가 있으면(등록 시점에 이미 같은 청커로 만든 경우) UPDATE/DELETE 없이 그대로 재사용하고,
    없으면 segment_revision(구조 청커)으로 만든다. 쓰기는 fenced 트랜잭션에서 한다.
    """

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")

        async with ctx.session_factory() as db:
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
                    "structured_passages": sum(1 for p in existing if p.section_path),
                },
                version="chunker-reuse-v1",
            )

        async with ctx.fenced_session() as db:
            passages = await segment_revision(db, ctx.revision_id)
        return StageResult.success(
            detail={
                "passage_count": len(passages),
                "reused_existing": False,
                "created": True,
                "structured_passages": sum(1 for p in passages if p.section_path),
            },
            version="chunker-structured-v1",
        )


def _utc_midnight() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class ExtractingProcessor:
    """L0/L1/L2 추출과 영속화. 앱 워커가 자동으로 부르며 MCP/에이전트 수동 호출에 의존하지 않는다.

    L2 는 어댑터 설정(kb_l2_adapter)을 따르고 기본은 꺼짐이다. 꺼짐/가용 안 됨/예산 소진/호출 실패는 L0·L1 결과를
    저장한 채 degraded(사유 포함)로 끝난다.
    """

    def __init__(self, adapter_factory: Callable[[], Any] | None = None) -> None:
        self._adapter_factory = adapter_factory

    def _adapter(self, ctx: StageContext) -> Any:
        if self._adapter_factory is not None:
            return self._adapter_factory()
        from ttakkari.kb_pipeline.l2_adapters import build_l2_adapter

        return build_l2_adapter(
            session_factory=ctx.session_factory,
            origin_scope=ctx.origin_scope,
            visibility=ctx.visibility,
        )

    async def _l2_calls_today(self, ctx: StageContext) -> int:
        from ttakkari.kb_pipeline_models import KbPipelineStage

        assert ctx.session_factory is not None
        async with ctx.session_factory() as db:
            rows = list(
                await db.scalars(
                    select(KbPipelineStage).where(
                        KbPipelineStage.stage == STAGE_EXTRACTING,
                        KbPipelineStage.finished_at >= _utc_midnight(),
                    )
                )
            )
        total = 0
        for row in rows:
            l2 = (row.detail or {}).get("l2") or {}
            calls = l2.get("calls_made") if isinstance(l2, dict) else None
            if isinstance(calls, int):
                total += calls
        return total

    @staticmethod
    def _budgeted_caller(adapter: Any, session_factory: Callable[[], Any], reservation_id: Any):
        from ttakkari.kb_pipeline.daily_budget import (
            begin_budgeted_call,
            finish_budgeted_call,
        )

        class BudgetedCaller:
            async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
                async with session_factory() as db:
                    usage_id = await begin_budgeted_call(db, reservation_id)
                    await db.commit()
                try:
                    result = await adapter(prompt, schema)
                except BaseException as exc:
                    consumed = getattr(exc, "consumed", True) is not False
                    async with session_factory() as db:
                        await finish_budgeted_call(
                            db,
                            usage_id,
                            consumed=consumed,
                            error_code=getattr(exc, "diagnosis", type(exc).__name__),
                        )
                        await db.commit()
                    raise
                async with session_factory() as db:
                    await finish_budgeted_call(db, usage_id, consumed=True)
                    await db.commit()
                return result

        return BudgetedCaller()

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")

        from ttakkari.kb_pipeline.alias_dictionary import load_seed_dictionary
        from ttakkari.kb_pipeline.extractor_step import (
            ExtractionOptions,
            extract_revision_knowledge,
            persist_extracted_knowledge,
        )

        async with ctx.session_factory() as db:
            rev = await db.get(KbRevision, ctx.revision_id)
            if rev is None:
                return StageResult.failed(f"revision_not_found: {ctx.revision_id}")
            passages = list(
                await db.scalars(
                    select(KbPassage)
                    .where(KbPassage.revision_id == ctx.revision_id)
                    .order_by(KbPassage.ord)
                )
            )
            db.expunge_all()
        if not passages:
            return StageResult.failed("no_passages_to_extract")

        s = get_settings()
        adapter = self._adapter(ctx)
        available, unavailable_reason = await adapter.availability()
        used_today = await self._l2_calls_today(ctx)
        from ttakkari.kb_pipeline.daily_budget import (
            finish_daily_reservation,
            reserve_daily_budget,
        )

        reserved_calls = 0
        reservation_id = None
        budget_remaining = max(0, s.kb_l2_daily_budget - used_today)
        if available:
            async with ctx.session_factory() as db:
                grant = await reserve_daily_budget(
                    db,
                    reservation_key=str(ctx.attempt_token),
                    requested_calls=min(s.kb_l2_max_calls_per_doc, len(passages)),
                    max_budget=s.kb_l2_daily_budget,
                    already_used=used_today,
                )
                await db.commit()
            reserved_calls = grant.granted_calls
            reservation_id = grant.reservation_id
            budget_remaining = grant.remaining_calls
        seeds = load_seed_dictionary()

        options = ExtractionOptions(
            llm_caller=(
                self._budgeted_caller(adapter, ctx.session_factory, reservation_id)
                if available and reservation_id is not None and reserved_calls > 0
                else None
            ),
            known_aliases=seeds.aliases,
            known_entities=seeds.entities,
            max_l2_calls_per_doc=s.kb_l2_max_calls_per_doc,
            daily_budget_remaining=reserved_calls if available else None,
            origin_scope=ctx.origin_scope,
            visibility=ctx.visibility,
        )
        cancelled = False
        try:
            extraction = await extract_revision_knowledge(rev, passages, options=options)
        except BaseException:
            cancelled = True
            raise
        finally:
            if reservation_id is not None:
                async with ctx.session_factory() as db:
                    await finish_daily_reservation(db, reservation_id, cancelled=cancelled)
                    await db.commit()

        async with ctx.fenced_session() as db:
            persisted = await persist_extracted_knowledge(
                db,
                extraction,
                document_id=ctx.document_id,
                revision_id=ctx.revision_id,
                origin_scope=ctx.origin_scope,
                visibility=ctx.visibility,
            )

        reason = extraction.degradation_reason
        if not available:
            reason = unavailable_reason or reason or "l2_unavailable"
        detail: dict[str, Any] = {
            **extraction.metrics,
            "persisted": persisted.as_dict(),
            "l2": {
                "adapter": getattr(adapter, "name", "unknown"),
                "available": available,
                "reason": reason,
                "calls_made": extraction.metrics.get("l2_calls_made", 0),
                **(
                    {"daily_budget_remaining_after_reservation": budget_remaining}
                    if available
                    else {"daily_budget_remaining_before": budget_remaining}
                ),
                **(
                    {"retry_after": adapter.retry_after}
                    if getattr(adapter, "retry_after", None) is not None
                    else {}
                ),
            },
        }
        if extraction.status == STAGE_STATUS_SUCCEEDED and available:
            return StageResult.success(detail=detail, version="extract-l012-v1")
        return StageResult.degraded(reason or "l2_degraded", detail=detail, version="extract-l012-v1")


ResolverFn = Callable[..., Awaitable[Any]]


def _load_resolver() -> ResolverFn | None:
    try:
        from ttakkari.kb_ontology import resolve_entity
    except ImportError as exc:
        log.warning("kb_ontology resolver unavailable: %s", exc)
        return None
    return resolve_entity


class LinkingProcessor:
    """추출된 엔터티를 해소기(kb_ontology.resolve_entity)로 기존 지식에 연결한다.

    해소기가 없으면 held(capability_missing)다. 연결이 held/실패여도 indexing 은 막히지 않는다(단계 의존성).
    visibility 는 입력이 아니라 문서 접근에서 해소기가 파생한다(source_access 로 접근 레코드를 넘긴다).
    """

    def __init__(self, resolver: ResolverFn | None = None, max_entities: int = 300) -> None:
        self._resolver = resolver
        self.max_entities = max_entities

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")
        resolver = self._resolver or _load_resolver()
        if resolver is None:
            return StageResult.held(
                "capability_missing: entity resolver (kb_ontology) unavailable",
                detail={"missing_capability": "kb_ontology.resolver"},
            )

        from ttakkari.collector.control_models import KbDocumentAccess
        from ttakkari.kb_ontology_models import (
            KbDocumentEntity,
            KbEntity,
            KbEntityAlias,
        )

        async with ctx.session_factory() as db:
            links = list(
                await db.scalars(
                    select(KbDocumentEntity)
                    .where(KbDocumentEntity.revision_id == ctx.revision_id)
                    .order_by(KbDocumentEntity.salience.desc(), KbDocumentEntity.passage_id)
                )
            )
            if not links:
                return StageResult.success(
                    detail={"entities_linked": 0, "note": "no_entity_mentions"}, version="link-resolver-v1"
                )
            first_link: dict[Any, KbDocumentEntity] = {}
            for link in links:
                first_link.setdefault(link.entity_id, link)
            entity_ids = list(first_link)[: self.max_entities]
            capped = len(first_link) > self.max_entities
            entities = {
                e.id: e
                for e in await db.scalars(select(KbEntity).where(KbEntity.id.in_(entity_ids)))
            }
            aliases: dict[Any, list[str]] = {}
            for a in await db.scalars(
                select(KbEntityAlias).where(KbEntityAlias.entity_id.in_(entity_ids))
            ):
                aliases.setdefault(a.entity_id, []).append(a.alias)
            passage_ids = {first_link[eid].passage_id for eid in entity_ids}
            passages = {
                p.id: p
                for p in await db.scalars(select(KbPassage).where(KbPassage.id.in_(passage_ids)))
            }
            access = await db.get(KbDocumentAccess, ctx.document_id)
            db.expunge_all()

        outcomes: dict[str, int] = {}
        resolved_elsewhere: list[str] = []
        conflicts = 0
        async with ctx.fenced_session() as db:
            for eid in entity_ids:
                ent = entities.get(eid)
                link = first_link[eid]
                passage = passages.get(link.passage_id)
                if ent is None or passage is None:
                    continue
                result = await resolver(
                    db,
                    ent.canonical_name,
                    aliases=aliases.get(eid, []),
                    entity_kind=ent.entity_kind,
                    domain=ent.domain,
                    summary=ent.summary or "",
                    passage_text=passage.body,
                    tenant_id=ent.tenant_id,
                    source_access=access,
                    document_id=ctx.document_id,
                    revision_id=ctx.revision_id,
                    passage_id=link.passage_id,
                    evidence_id=link.evidence_id,
                    role=link.role,
                    salience=link.salience,
                )
                step = getattr(getattr(result, "step", None), "name", "unknown")
                outcomes[step] = outcomes.get(step, 0) + 1
                conflicts += len(getattr(result, "conflicts", []) or [])
                if result.entity.id != eid and len(resolved_elsewhere) < 20:
                    resolved_elsewhere.append(ent.canonical_name)

        detail = {
            "entities_linked": len(entity_ids),
            "resolution_steps": outcomes,
            "ambiguity_conflicts": conflicts,
            "resolved_to_other_entity": resolved_elsewhere,
            "capped": capped,
        }
        if capped:
            return StageResult.degraded(
                f"entity_cap_reached: {self.max_entities}", detail=detail, version="link-resolver-v1"
            )
        return StageResult.success(detail=detail, version="link-resolver-v1")


class DefaultIndexingProcessor:
    """어휘(질의 시점 FTS)와 의미(pgvector 임베딩) 색인. linking 결과와 무관하게 chunking 만 있으면 진행한다.

    - 어휘 검색은 패시지 본문을 질의 시점에 훑으므로 패시지가 있으면 준비된 것이다(별도 색인 쓰기 없음).
    - 의미 색인: 임베딩이 꺼져 있으면 degraded('semantic_index_disabled'), 켜져 있으면 이 리비전의 패시지 벡터를
      만들고 fenced 트랜잭션으로 저장한다. 너무 긴 패시지는 격리해 degraded 로 남긴다. 그 외 모델 오류는 failed(재시도 대상).
    """

    BATCH = 16

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")

        async with ctx.session_factory() as db:
            passages = list(
                await db.scalars(
                    select(KbPassage)
                    .where(KbPassage.revision_id == ctx.revision_id)
                    .order_by(KbPassage.ord)
                )
            )
            db.expunge_all()
        if not passages:
            return StageResult.failed("no_passages_to_index")

        detail: dict[str, Any] = {
            "passage_count": len(passages),
            "lexical_ready": True,
            "vectors_created": 0,
            "vectors_total": 0,
        }

        from ttakkari import kb_retrieval
        from ttakkari.context import embedder
        from ttakkari.kb_vector_models import KbPassageVector as V

        s = get_settings()
        if not s.kb_semantic_enabled or not embedder.enabled():
            return StageResult.degraded(
                "semantic_index_disabled",
                detail={**detail, "semantic": "disabled", "missing_capability": "semantic_index"},
                version="indexing-v2",
            )

        model_key = kb_retrieval.model_key()
        async with ctx.session_factory() as db:
            have = set(
                await db.scalars(
                    select(V.passage_id).where(
                        V.model_key == model_key,
                        V.passage_id.in_([p.id for p in passages]),
                    )
                )
            )
        pending = [p for p in passages if p.id not in have]

        vectors: list[tuple[KbPassage, list[float]]] = []
        too_long: list[str] = []
        for i in range(0, len(pending), self.BATCH):
            batch = pending[i : i + self.BATCH]
            try:
                embedded = await kb_retrieval.embed_complete([p.body for p in batch])
                vectors.extend(zip(batch, embedded, strict=True))
            except ValueError as exc:
                if "passage_too_long_for_semantic_projection" not in str(exc):
                    raise
                for p in batch:
                    try:
                        [vec] = await kb_retrieval.embed_complete([p.body])
                        vectors.append((p, vec))
                    except ValueError as item_exc:
                        if "passage_too_long_for_semantic_projection" not in str(item_exc):
                            raise
                        too_long.append(str(p.id))

        if vectors:
            from sqlalchemy.dialects.postgresql import insert

            async with ctx.fenced_session() as db:
                for passage, vec in vectors:
                    await db.execute(
                        insert(V)
                        .values(
                            passage_id=passage.id,
                            model_key=model_key,
                            text_sha256=passage.sha256,
                            vector=vec,
                        )
                        .on_conflict_do_nothing()
                    )

        detail.update(
            {
                "vectors_created": len(vectors),
                "vectors_total": len(have) + len(vectors),
                "model_key": model_key,
                "too_long_passages": too_long,
            }
        )
        if too_long:
            return StageResult.degraded(
                "passage_too_long_for_semantic_projection", detail=detail, version="indexing-v2"
            )
        return StageResult.success(detail=detail, version="indexing-v2")
