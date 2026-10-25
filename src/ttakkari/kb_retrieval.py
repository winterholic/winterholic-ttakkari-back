"""Scoped lexical/dense/entity/graph fusion for KB v3 (KB3-401).

점수는 랭킹 신호(ranking signals)이며 결코 사실성(factuality)이나 보정된 신뢰도(calibrated confidence)가 아니다.
5대 후보 생성 경로(어휘, E5 벡터, 엔터티 직접 매칭, 1-hop 온톨로지 확장, 메타·최신성 필터)를 분리하고,
RRF(Reciprocal Rank Fusion, k=60)와 문서 다양성 캡(Document Diversity Cap) 및 결정적 정렬(Deterministic Ties)을 적용한다.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import uuid
from typing import Any

from sqlalchemy import func, not_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ttakkari import kb_library as lib
from ttakkari.common.lexical import terms
from ttakkari.config import get_settings
from ttakkari.context import embedder
from ttakkari.db import sessionmaker
from ttakkari.kb_access import (
    KbAccessContext,
    assertion_access_filter,
    entity_access_filter,
    from_legacy_scope,
    passage_access_filter,
)
from ttakkari.kb_domains import entity_domains_for_topic
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.kb_ontology.retrieval import (
    expand_1hop_relations,
    fetch_1hop_passages,
    fetch_canonical_cards,
    fetch_entity_passages,
    find_matched_entities,
)
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
)
from ttakkari.kb_vector_models import KbPassageVector as V
from ttakkari.model_process import run_json_worker
from ttakkari.models import EMBED_DIM

_lock = asyncio.Lock()
_last_error: str | None = None
_failed_passages: dict[str, str] = {}

# RRF 상수 및 가중치
RRF_K = 60
WEIGHT_LEXICAL = 1.0
WEIGHT_VECTOR = 1.0
WEIGHT_ENTITY = 1.15
WEIGHT_GRAPH_1HOP = 0.55
# 후보(candidate) 관계에서 온 1-hop 패시지는 확정 관계보다 훨씬 낮게만 반영한다(요청 옵션 include_candidates).
WEIGHT_GRAPH_1HOP_CANDIDATE = 0.15

# 품질 신호 가산점
BETA_CANONICAL = 0.008
BETA_FRESH = 0.004
BETA_TITLE = 0.005

# 문서 다양성 감점 계수 (3번째 이후 패시지)
DIVERSITY_PENALTY = 0.5


def _metadata_filter_predicates(
    filters: dict[str, Any], ctx: KbAccessContext
) -> list[Any]:
    """Build bounded, lineage-aware metadata predicates for a revision query."""
    predicates: list[Any] = []
    source_conditions: list[Any] = [KbSource.id == KbRevision.source_id]

    source_kind = filters.get("source_kind", filters.get("type"))
    if source_kind:
        source_conditions.append(KbSource.kind == str(source_kind))
    source_key = filters.get("source")
    if source_key:
        source_conditions.append(KbSource.source_key == str(source_key))
    source_id = filters.get("source_id")
    if source_id:
        try:
            source_conditions.append(KbSource.id == uuid.UUID(str(source_id)))
        except ValueError as exc:
            raise ValueError("kb_filter_source_id_invalid") from exc
    domain = filters.get("domain")
    domain_predicate = None
    if domain:
        # 사용자가 등록 때 고른 주제가 우선이고, 없으면 문서 엔터티의 domain 으로 자동 분류한 값과 맞춘다.
        explicit = select(KbSource.id).where(
            KbSource.id == KbRevision.source_id, KbSource.source_meta["domain"].astext == str(domain)
        ).correlate(KbRevision).exists()
        auto_ent = aliased(KbEntity)
        auto_doc = aliased(KbDocumentEntity)
        derived = (
            select(auto_doc.entity_id)
            .join(auto_ent, auto_ent.id == auto_doc.entity_id)
            .where(
                auto_doc.revision_id == KbRevision.id,
                auto_ent.domain.in_(entity_domains_for_topic(str(domain))),
                entity_access_filter(ctx, auto_ent),
            )
            .correlate(KbRevision)
            .exists()
        )
        domain_predicate = or_(explicit, derived)
    entity_id = filters.get("entity_id")
    if entity_id:
        try:
            wanted_entity = uuid.UUID(str(entity_id))
        except ValueError as exc:
            raise ValueError("kb_filter_entity_id_invalid") from exc
        link = aliased(KbDocumentEntity)
        link_entity = aliased(KbEntity)
        predicates.append(
            select(link.entity_id)
            .join(link_entity, link_entity.id == link.entity_id)
            .where(link.revision_id == KbRevision.id, link.entity_id == wanted_entity, entity_access_filter(ctx, link_entity))
            .correlate(KbRevision)
            .exists()
        )

    for key, is_lower_bound in (("collected_after", True), ("collected_before", False)):
        value = filters.get(key)
        if not value:
            continue
        if isinstance(value, str):
            try:
                value = datetime.datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"kb_filter_{key}_invalid") from exc
        if not isinstance(value, datetime.datetime):
            raise TypeError(f"kb_filter_{key}_invalid")
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.UTC)
        source_conditions.append(
            KbSource.collected_at >= value
            if is_lower_bound
            else KbSource.collected_at <= value
        )
    if len(source_conditions) > 1:
        predicates.append(
            select(KbSource.id).where(*source_conditions).correlate(KbRevision).exists()
        )
    if domain_predicate is not None:
        predicates.append(domain_predicate)

    concept = filters.get("concept")
    if concept:
        doc_entity = aliased(KbDocumentEntity)
        concept_entity = aliased(KbEntity)
        predicates.append(
            select(doc_entity.entity_id)
            .join(concept_entity, concept_entity.id == doc_entity.entity_id)
            .where(
                doc_entity.document_id == KbRevision.document_id,
                concept_entity.canonical_name.ilike(f"%{concept}%"),
                entity_access_filter(ctx, concept_entity),
            )
            .correlate(KbRevision)
            .exists()
        )

    status_value = filters.get("status")
    if status_value:
        if status_value == "current":
            predicates.append(lib._current_revision_filter())
        elif status_value == "historical":
            predicates.append(not_(lib._current_revision_filter()))
        else:
            raise ValueError("kb_filter_status_invalid")
    return predicates


def model_key() -> str:
    snapshot = (
        get_settings().kb_embedding_snapshot
        if get_settings().context_embedder == "e5"
        else "test"
    )
    return embedder.model_name() + ":kb-full-v1:" + snapshot


def windows(text: str) -> list[str]:
    # Deterministic lexical/hash test backend; the real model worker splits by exact token counts.
    return [text[i : i + 384] for i in range(0, max(1, len(text) - 64), 320)]


async def embed_complete(texts: list[str], *, query: bool = False) -> list[list[float]]:
    if not embedder.enabled():
        raise RuntimeError("kb_semantic_disabled")
    if any(len(text) > 50000 for text in texts):
        raise ValueError("passage_too_long_for_semantic_projection")
    s = get_settings()
    if s.context_embedder == "e5":
        result = await run_json_worker(
            module="ttakkari.kb_embedding_worker",
            request={
                "texts": texts,
                "query": query,
                "model": s.context_model,
                "cache_dir": str(s.models_dir),
                "threads": s.context_embedding_cpu_threads,
                "snapshot": s.kb_embedding_snapshot,
            },
            timeout=s.context_embedding_timeout_seconds,
            error_prefix="kb_embedding",
        )
        if result.get("coverage") != "full":
            raise ValueError("kb_embedding_incomplete")
        vectors = result["vectors"]
    else:
        vectors = await embedder.embed(texts, query=query)
    if len(vectors) != len(texts) or any(
        len(v) != EMBED_DIM or not all(math.isfinite(x) for x in v) for v in vectors
    ):
        raise ValueError("kb_embedding_invalid")
    return vectors


async def index_pending(*, limit: int = 16) -> int:
    global _last_error
    async with _lock:
        async with sessionmaker()() as db:
            indexed = (
                select(V.passage_id)
                .where(V.passage_id == KbPassage.id, V.model_key == model_key())
                .exists()
            )
            query = (
                select(KbPassage)
                .where(~indexed)
                .order_by(KbPassage.created_at, KbPassage.id)
            )
            if _failed_passages:
                query = query.where(KbPassage.id.not_in(_failed_passages.keys()))
            passages = list(await db.scalars(query.limit(limit)))
        if not passages:
            return 0
        try:
            vectors = await embed_complete([p.body for p in passages])
        except Exception as exc:
            if "passage_too_long_for_semantic_projection" not in str(exc):
                _last_error = type(exc).__name__ + ":" + str(exc)[:200]
                raise
            vectors, usable = [], []
            for passage in passages:
                try:
                    [vector] = await embed_complete([passage.body])
                except ValueError as item_error:
                    if "passage_too_long_for_semantic_projection" not in str(item_error):
                        _last_error = type(item_error).__name__ + ":" + str(item_error)[:200]
                        raise
                    _failed_passages[str(passage.id)] = str(item_error)[:200]
                else:
                    usable.append(passage)
                    vectors.append(vector)
            passages = usable
            if not passages:
                _last_error = "ValueError:passage_too_long_for_semantic_projection"
                return 0
        async with sessionmaker()() as db:
            for passage, vector in zip(passages, vectors, strict=True):
                await db.execute(
                    insert(V)
                    .values(
                        passage_id=passage.id,
                        model_key=model_key(),
                        text_sha256=passage.sha256,
                        vector=vector,
                    )
                    .on_conflict_do_nothing()
                )
            await db.commit()
        _last_error = (
            None
            if not _failed_passages
            else "ValueError:passage_too_long_for_semantic_projection"
        )
        return len(passages)


async def status(
    db: AsyncSession,
    scope: str = "personal",
    access_context: KbAccessContext | None = None,
) -> dict[str, Any]:
    ctx = access_context or from_legacy_scope(scope)
    total = (
        await db.scalar(
            select(func.count())
            .select_from(KbPassage)
            .where(passage_access_filter(ctx, KbPassage))
        )
        or 0
    )
    indexed = (
        await db.scalar(
            select(func.count())
            .select_from(V)
            .join(KbPassage, V.passage_id == KbPassage.id)
            .where(
                V.model_key == model_key(),
                V.text_sha256 == KbPassage.sha256,
                passage_access_filter(ctx, KbPassage),
            )
        )
        or 0
    )
    passage_ids = {
        str(pid)
        for pid in await db.scalars(
            select(KbPassage.id).where(passage_access_filter(ctx, KbPassage))
        )
    }
    failed = len(passage_ids.intersection(_failed_passages))
    return {
        "model": embedder.model_name(),
        "model_key": model_key(),
        "indexed": indexed,
        "total": total,
        "failed": failed,
        "enabled": get_settings().kb_semantic_enabled,
        "last_error": _last_error,
        "quality_gate": "unverified",
        "engine": "postgresql_exact_cosine",
        "tenant_id": ctx.tenant_id,
        "allowed_visibilities": ctx.allowed_visibilities,
    }


_MATCH_PATH_LABEL = {
    "lexical": "lexical",
    "vector": "vector",
    "entity": "canonical",
    "graph_1hop": "hop1",
    "graph_1hop_candidate": "hop1",
}
_MATCH_PATH_ORDER = ("lexical", "vector", "canonical", "hop1")


def match_paths_for(ranks: dict[str, int]) -> list[str]:
    """내부 후보 경로 이름을 API 계약의 매칭 경로 라벨(lexical|vector|canonical|hop1)로 바꾼다."""
    labels = {_MATCH_PATH_LABEL[name] for name in ranks if name in _MATCH_PATH_LABEL}
    return [label for label in _MATCH_PATH_ORDER if label in labels]


def canonical_facts_for_passage(
    pid: str,
    passage_entity_map: dict[str, set[str]],
    canonical_cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """이 패시지에 나타난 질의 일치 엔터티의 정본 핵심 사실(설계 §2.6). 정본 카드에 있는 값만 옮긴다."""
    facts: list[dict[str, Any]] = []
    for card in canonical_cards:
        if card["entity_id"] not in passage_entity_map.get(pid, set()):
            continue
        for fact in card.get("key_facts") or []:
            if isinstance(fact, dict):
                facts.append({"entity_id": card["entity_id"], "entity": card["canonical_name"], **fact})
        if not card.get("key_facts") and card.get("canonical_definition"):
            facts.append({"entity_id": card["entity_id"], "entity": card["canonical_name"],
                          "predicate": "definition", "value": card["canonical_definition"]})
    return facts[:5]


def compute_coverage(
    ranked_passages: list[dict[str, Any]],
    matched_entities: list[dict[str, Any]],
    ranks_map: dict[str, dict[str, int]],
) -> str:
    """검색 결과의 coverage 를 'strong' | 'weak' | 'none' 으로 판정한다."""
    if not ranked_passages and not matched_entities:
        return "none"

    if ranked_passages:
        top_score = ranked_passages[0].get("score", 0.0)
        top_pid = ranked_passages[0]["passage_id"]
        path_count = len(ranks_map.get(top_pid, {}))

        # 상위 패시지가 높은 점수를 받거나 2개 이상의 독립 경로에서 일치한 경우 strong
        if top_score >= 0.03 or path_count >= 2:
            return "strong"
        if top_score >= 0.01 or matched_entities:
            return "weak"

    if matched_entities:
        return "weak"

    return "none"


def fuse_and_rerank(
    candidate_ranks: dict[str, dict[str, int]],
    passage_metadata: dict[str, dict[str, Any]],
    query_terms: list[str],
) -> list[tuple[str, float]]:
    """RRF 및 품질 신호 가산, 문서 다양성 캡을 적용하여 패시지 순위를 결정한다.

    Score(p) = sum_{m}(w_m / (60 + rank_m(p))) + beta_canonical + beta_fresh + beta_title
    동일 문서에서 상위 2개 패시지를 초과하는 3번째 이후 패시지는 0.5배 감점한다.
    동점 시 (-score, doc_id, passage_id, ord) 기준 완전 결정적 정렬(Deterministic Ties).
    """
    now = datetime.datetime.now(datetime.UTC)
    fresh_cutoff = now - datetime.timedelta(days=180)

    # 1. 1차 RRF 점수 계산
    raw_scores: dict[str, float] = {}
    weights = {
        "lexical": WEIGHT_LEXICAL,
        "vector": WEIGHT_VECTOR,
        "entity": WEIGHT_ENTITY,
        "graph_1hop": WEIGHT_GRAPH_1HOP,
        "graph_1hop_candidate": WEIGHT_GRAPH_1HOP_CANDIDATE,
    }

    for pid, ranks in candidate_ranks.items():
        score = 0.0
        for path_name, rank in ranks.items():
            w = weights.get(path_name, 1.0)
            score += w / (RRF_K + rank)

        meta = passage_metadata.get(pid, {})

        # 품질 신호 1: 정본 주장 근거 포함 여부
        if meta.get("is_canonical_evidence"):
            score += BETA_CANONICAL

        # 품질 신호 2: 최신성 (180일 이내 발행/수집 및 현재 활성 리비전)
        collected_at = meta.get("collected_at")
        if collected_at and meta.get("is_current_revision"):
            if isinstance(collected_at, str):
                try:
                    collected_at = datetime.datetime.fromisoformat(collected_at)
                except ValueError:
                    collected_at = None
            if collected_at and collected_at.tzinfo is None:
                collected_at = collected_at.replace(tzinfo=datetime.UTC)
            if collected_at and collected_at >= fresh_cutoff:
                score += BETA_FRESH

        # 품질 신호 3: 제목/헤딩 질의 일치
        heading = (meta.get("heading") or "").lower()
        title = (meta.get("title") or "").lower()
        if any(t.lower() in heading or t.lower() in title for t in query_terms if len(t) >= 2):
            score += BETA_TITLE

        raw_scores[pid] = score

    # 1차 정렬 (점수 내림차순, 문서 ID, 패시지 ID, ord)
    sorted_passages = sorted(
        raw_scores.keys(),
        key=lambda pid: (
            -raw_scores[pid],
            passage_metadata.get(pid, {}).get("document_id", ""),
            pid,
            passage_metadata.get(pid, {}).get("ord", 0),
        ),
    )

    # 2. 문서 다양성 캡 (Document Diversity Cap) 적용
    # 동일 document_id 에서 상위 2개를 초과하는 패시지는 점수 * 0.5 감점
    doc_appearance_count: dict[str, int] = {}
    final_scores: dict[str, float] = {}

    for pid in sorted_passages:
        doc_id = passage_metadata.get(pid, {}).get("document_id", "")
        count = doc_appearance_count.get(doc_id, 0)
        score = raw_scores[pid]

        if count >= 2:
            score *= DIVERSITY_PENALTY

        doc_appearance_count[doc_id] = count + 1
        final_scores[pid] = score

    # 3. 최종 결정적 정렬
    final_ranked = sorted(
        final_scores.keys(),
        key=lambda pid: (
            -final_scores[pid],
            passage_metadata.get(pid, {}).get("document_id", ""),
            pid,
            passage_metadata.get(pid, {}).get("ord", 0),
        ),
    )

    return [(pid, final_scores[pid]) for pid in final_ranked]


async def search(
    db: AsyncSession,
    *,
    query: str,
    limit: int = 10,
    offset: int = 0,
    document_id: uuid.UUID | None = None,
    current_only: bool = True,
    scope: str = "personal",
    access_context: KbAccessContext | None = None,
    filters: dict[str, Any] | None = None,
    mode: str = "hybrid",
    include_candidates: bool = False,
) -> dict[str, Any]:
    """KB v3 5경로 그래프 통합 하이브리드 검색.

    경로:
    1. 한국어·영문 어휘 검색 (lexical_rank)
    2. 로컬 E5 dense vector 검색 (vector_rank)
    3. 엔터티·별칭 직접 매칭 및 정본/주장 역색인 (entity_direct_rank)
    4. 1-hop 온톨로지 그래프 확장 (graph_1hop_rank)
    5. 메타데이터 및 최신성 필터링 (filters, current_only)
    """
    if offset > 200:
        raise ValueError("kb_hybrid_offset_limit")

    ctx = access_context or from_legacy_scope(scope)
    words = terms(query)
    filter_dict = filters or {}
    metadata_predicates = _metadata_filter_predicates(filter_dict, ctx)
    if filter_dict.get("status") == "historical":
        current_only = False

    # 후보 경로별 랭킹: {passage_id: {path_name: rank_1_based}}
    candidate_ranks: dict[str, dict[str, int]] = {}
    passage_metadata: dict[str, dict[str, Any]] = {}
    loaded_passages: dict[str, KbPassage] = {}
    loaded_revisions: dict[str, KbRevision] = {}
    loaded_documents: dict[str, KbDocument] = {}
    loaded_sources: dict[str, KbSource] = {}

    # -------------------------------------------------------------
    # 경로 1: 한국어·영문 어휘 검색 (lexical)
    # -------------------------------------------------------------
    if mode in ("hybrid", "lexical") and words:
        lex_predicate, lex_score = lib.lexical(KbPassage.body, query, words)
        lex_stmt = (
            select(KbPassage, KbRevision, KbDocument, lex_score.label("score"))
            .join(KbRevision, KbRevision.id == KbPassage.revision_id)
            .join(KbDocument, KbDocument.id == KbRevision.document_id)
            .where(
                lex_predicate,
                passage_access_filter(ctx, KbPassage),
            )
        )
        if current_only:
            lex_stmt = lex_stmt.where(lib._current_revision_filter())
        if document_id is not None:
            lex_stmt = lex_stmt.where(KbDocument.id == document_id)
        if metadata_predicates:
            lex_stmt = lex_stmt.where(*metadata_predicates)
        if filter_dict.get("lang"):
            lex_stmt = lex_stmt.where(KbDocument.lang == filter_dict["lang"])

        lex_rows = (
            await db.execute(
                lex_stmt.order_by(lex_score.desc(), KbPassage.id).limit(40)
            )
        ).all()

        for rank, (passage, rev, doc, _) in enumerate(lex_rows, 1):
            pid = str(passage.id)
            candidate_ranks.setdefault(pid, {})["lexical"] = rank
            loaded_passages[pid] = passage
            loaded_revisions[str(rev.id)] = rev
            loaded_documents[str(doc.id)] = doc

    # -------------------------------------------------------------
    # 경로 2: 로컬 E5 Dense Vector 검색 (vector)
    # -------------------------------------------------------------
    if mode in ("hybrid", "vector") and get_settings().kb_semantic_enabled:
        [query_vec] = await embed_complete([query], query=True)
        cosine = V.vector.cosine_distance(query_vec).label("distance")
        vec_stmt = (
            select(KbPassage, KbRevision, KbDocument, cosine)
            .join(V, V.passage_id == KbPassage.id)
            .join(KbRevision, KbPassage.revision_id == KbRevision.id)
            .join(KbDocument, KbRevision.document_id == KbDocument.id)
            .where(
                V.model_key == model_key(),
                V.text_sha256 == KbPassage.sha256,
                passage_access_filter(ctx, KbPassage),
            )
        )
        if current_only:
            vec_stmt = vec_stmt.where(lib._current_revision_filter())
        if document_id is not None:
            vec_stmt = vec_stmt.where(KbDocument.id == document_id)
        if metadata_predicates:
            vec_stmt = vec_stmt.where(*metadata_predicates)
        if filter_dict.get("lang"):
            vec_stmt = vec_stmt.where(KbDocument.lang == filter_dict["lang"])

        vec_rows = (
            await db.execute(vec_stmt.order_by(cosine, KbPassage.id).limit(40))
        ).all()

        for rank, (passage, rev, doc, _) in enumerate(vec_rows, 1):
            pid = str(passage.id)
            candidate_ranks.setdefault(pid, {})["vector"] = rank
            loaded_passages[pid] = passage
            loaded_revisions[str(rev.id)] = rev
            loaded_documents[str(doc.id)] = doc
    # -------------------------------------------------------------
    # 경로 3: 엔터티·별칭 직접 매칭 및 정본/주장 역색인 (entity_direct)
    # -------------------------------------------------------------
    matched_entities_raw = []
    canonical_cards = []
    expanded_relations = []

    if mode in ("hybrid", "graph") and words:
        matched_entities_raw = await find_matched_entities(
            db, query, words, ctx, limit=10
        )
        if matched_entities_raw:
            matched_ent_ids = [e["entity_id"] for e in matched_entities_raw]

            # 3-1. 엔터티 연관 패시지 수집 (salience 기반)
            ent_passages = await fetch_entity_passages(
                db, matched_ent_ids, ctx, limit=30
            )
            for rank, (p_uuid, _) in enumerate(ent_passages, 1):
                pid = str(p_uuid)
                candidate_ranks.setdefault(pid, {})["entity"] = rank

            # -------------------------------------------------------------
            # 경로 4: 1-hop 온톨로지 그래프 확장 (graph_1hop)
            # -------------------------------------------------------------
            expanded_relations = await expand_1hop_relations(
                db, matched_ent_ids, ctx, max_per_entity=5, include_candidates=include_candidates
            )
            active_relations = [r for r in expanded_relations if not r.get("candidate")]
            hop1_passages = await fetch_1hop_passages(db, active_relations, ctx, limit=15)
            for rank, p_uuid in enumerate(hop1_passages, 1):
                pid = str(p_uuid)
                candidate_ranks.setdefault(pid, {})["graph_1hop"] = rank
            if include_candidates:
                candidate_relations = [r for r in expanded_relations if r.get("candidate")]
                for rank, p_uuid in enumerate(await fetch_1hop_passages(db, candidate_relations, ctx, limit=15), 1):
                    pid = str(p_uuid)
                    if "graph_1hop" not in candidate_ranks.get(pid, {}):
                        candidate_ranks.setdefault(pid, {})["graph_1hop_candidate"] = rank

            # 매칭된 엔티티의 실제 정본 카드 로드 (가짜 데이터 없음)
            canonical_cards = await fetch_canonical_cards(
                db, matched_ent_ids, ctx, expanded_relations
            )

    # -------------------------------------------------------------
    # 경로 3 & 4에서 추가된 패시지들의 메타데이터 일괄 로드
    # -------------------------------------------------------------
    missing_pids = [
        uuid.UUID(pid)
        for pid in candidate_ranks
        if pid not in loaded_passages
    ]
    if missing_pids:
        missing_stmt = (
            select(KbPassage, KbRevision, KbDocument)
            .join(KbRevision, KbPassage.revision_id == KbRevision.id)
            .join(KbDocument, KbRevision.document_id == KbDocument.id)
            .where(
                KbPassage.id.in_(missing_pids),
                passage_access_filter(ctx, KbPassage),
            )
        )
        if current_only:
            missing_stmt = missing_stmt.where(lib._current_revision_filter())
        if document_id is not None:
            missing_stmt = missing_stmt.where(KbDocument.id == document_id)
        if metadata_predicates:
            missing_stmt = missing_stmt.where(*metadata_predicates)
        if filter_dict.get("lang"):
            missing_stmt = missing_stmt.where(KbDocument.lang == filter_dict["lang"])

        for passage, rev, doc in (await db.execute(missing_stmt)).all():
            pid = str(passage.id)
            loaded_passages[pid] = passage
            loaded_revisions[str(rev.id)] = rev
            loaded_documents[str(doc.id)] = doc

    # 유효한 패시지만 필터링 (필터/접근 제어 탈락 항목 제거)
    valid_pids = set(loaded_passages.keys())
    candidate_ranks = {
        pid: ranks for pid, ranks in candidate_ranks.items() if pid in valid_pids
    }

    # -------------------------------------------------------------
    # 소스(KbSource) 및 최신 리비전, 정본 근거 여부, 충돌 여부 일괄 조회
    # -------------------------------------------------------------
    needed_source_ids = {
        rev.source_id for rev in loaded_revisions.values() if rev.source_id
    }
    if needed_source_ids:
        src_stmt = select(KbSource).where(KbSource.id.in_(needed_source_ids))
        for s in (await db.execute(src_stmt)).scalars().all():
            loaded_sources[str(s.id)] = s

    # 최신 리비전 ID 확인
    doc_ids = {doc.id for doc in loaded_documents.values()}
    latest_rev_ids: dict[uuid.UUID, uuid.UUID] = {}
    for d_id in doc_ids:
        latest = await lib._latest_revision(db, d_id)
        if latest:
            latest_rev_ids[d_id] = latest.id

    # 패시지별 정본 주장 근거(KbAssertionEvidence -> is_canonical) 조회
    valid_pid_uuids = [uuid.UUID(pid) for pid in valid_pids]
    canonical_evidence_pids: set[str] = set()
    passage_assertions_map: dict[str, list[dict[str, Any]]] = {}
    passage_conflict_map: dict[str, bool] = {}

    if valid_pid_uuids:
        # 주장 및 정본 근거 조회 (KbAssertionEvidence -> KbEvidence -> passage_id)
        assert_stmt = (
            select(
                KbEvidence.passage_id,
                KbAssertion.id,
                KbAssertion.predicate,
                KbAssertion.object_value,
                KbAssertion.is_canonical,
                KbAssertion.status,
                KbEntity.canonical_name,
            )
            .join(KbAssertionEvidence, KbAssertionEvidence.evidence_id == KbEvidence.id)
            .join(KbAssertion, KbAssertion.id == KbAssertionEvidence.assertion_id)
            .join(KbEntity, KbEntity.id == KbAssertion.subject_entity_id)
            .where(
                KbEvidence.passage_id.in_(valid_pid_uuids),
                KbAssertion.status == "active",
                assertion_access_filter(ctx, KbAssertion),
            )
        )
        assert_rows = (await db.execute(assert_stmt)).all()
        for p_id, a_id, pred, obj, is_can, status_val, subject_name in assert_rows:
            pid_str = str(p_id)
            if is_can:
                canonical_evidence_pids.add(pid_str)
            passage_assertions_map.setdefault(pid_str, []).append({
                "assertion_id": str(a_id),
                "predicate": pred,
                "object": obj,
                "is_canonical": is_can,
                "status": status_val,
                # v3 추가 필드: 화면이 바로 쓰는 id·문장(주어 — 술어: 값)
                "id": str(a_id),
                "subject": subject_name,
                "text": f"{subject_name} — {pred}: {obj}",
            })

        # 열린 충돌(KbConflict) 연관 여부 조회
        from sqlalchemy.orm import aliased

        conflict_a = aliased(KbAssertion)
        conflict_b = aliased(KbAssertion)
        conflict_stmt = (
            select(KbDocumentEntity.passage_id)
            .join(KbConflict, KbConflict.entity_id == KbDocumentEntity.entity_id)
            .join(conflict_a, conflict_a.id == KbConflict.assertion_a_id)
            .join(conflict_b, conflict_b.id == KbConflict.assertion_b_id)
            .where(
                KbDocumentEntity.passage_id.in_(valid_pid_uuids),
                KbConflict.status == "open",
                KbConflict.visibility.in_(ctx.allowed_visibilities),
                assertion_access_filter(ctx, conflict_a),
                assertion_access_filter(ctx, conflict_b),
            )
        )
        conflict_pids = (await db.execute(conflict_stmt)).scalars().all()
        for cp_id in conflict_pids:
            passage_conflict_map[str(cp_id)] = True

    # 패시지 메타데이터 구성 (RRF 랭킹 계산용)
    for pid in valid_pids:
        passage = loaded_passages[pid]
        rev = loaded_revisions[str(passage.revision_id)]
        doc = loaded_documents[str(rev.document_id)]
        src = loaded_sources.get(str(rev.source_id))

        is_current = latest_rev_ids.get(doc.id) == rev.id
        passage_metadata[pid] = {
            "passage_id": pid,
            "document_id": str(doc.id),
            "ord": passage.ord,
            "heading": passage.heading,
            "title": doc.title,
            "collected_at": src.collected_at if src else rev.created_at,
            "is_current_revision": is_current,
            "is_canonical_evidence": pid in canonical_evidence_pids,
        }

    # -------------------------------------------------------------
    # 순위 융합 (RRF) 및 재순위 (Re-ranking)
    # -------------------------------------------------------------
    ranked_tuples = fuse_and_rerank(candidate_ranks, passage_metadata, words)

    # 페이지네이션 슬라이스
    sliced_tuples = ranked_tuples[offset : offset + limit]
    has_more = len(ranked_tuples) > offset + limit

    # 질의 일치 엔터티가 이 페이지의 어느 패시지에 나타나는가(항목별 정본 사실 연결용)
    passage_entity_map: dict[str, set[str]] = {}
    if canonical_cards and sliced_tuples:
        for p_id, e_id in await db.execute(
            select(KbDocumentEntity.passage_id, KbDocumentEntity.entity_id).where(
                KbDocumentEntity.passage_id.in_([uuid.UUID(pid) for pid, _ in sliced_tuples]),
                KbDocumentEntity.entity_id.in_([uuid.UUID(c["entity_id"]) for c in canonical_cards]),
            )
        ):
            passage_entity_map.setdefault(str(p_id), set()).add(str(e_id))

    # 응답 항목 생성
    items = []
    for pid, score in sliced_tuples:
        passage = loaded_passages[pid]
        rev = loaded_revisions[str(passage.revision_id)]
        doc = loaded_documents[str(rev.document_id)]
        src = loaded_sources.get(str(rev.source_id))

        snippet, marks = lib.snippet_with_highlights(passage.body, words)
        is_current = latest_rev_ids.get(doc.id) == rev.id

        items.append({
            "passage_id": pid,
            "ord": passage.ord,
            "heading": passage.heading,
            "snippet": snippet,
            "highlights": marks,
            "score": round(float(score), 4),
            "char_start": passage.char_start,
            "char_end": passage.char_end,
            "section_path": getattr(passage, "section_path", None) or [],
            "locator_meta": {
                "page_no": getattr(passage, "page_no", None),
                "slide_no": getattr(passage, "slide_no", None),
                "time_start_ms": getattr(passage, "time_start_ms", None),
                "time_end_ms": getattr(passage, "time_end_ms", None),
            },
            "document": {
                "id": str(doc.id),
                "title": doc.title,
                "lang": doc.lang,
                "domain": getattr(doc, "domain", None),
            },
            "revision": {
                "id": str(rev.id),
                "rev_no": rev.rev_no,
                "current": is_current,
                "char_count": rev.char_count,
            },
            "source": lib._source_out(src) if src else None,
            "assertions": passage_assertions_map.get(pid, []),
            "has_conflict": passage_conflict_map.get(pid, False),
            # 아래 필드는 v3 추가분이다(기존 필드는 그대로). 어느 경로가 이 패시지를 후보로 올렸는가.
            "match_paths": match_paths_for(candidate_ranks.get(pid, {})),
            "conflict_warning": passage_conflict_map.get(pid, False),
            "canonical_facts": canonical_facts_for_passage(pid, passage_entity_map, canonical_cards),
        })

    coverage = compute_coverage(items, canonical_cards, candidate_ranks)

    return {
        "query": query,
        "terms": words,
        "items": items,
        "matched_entities": canonical_cards,
        "coverage": coverage,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "include_candidates": include_candidates,
        "retrieval": await status(db, scope=scope, access_context=ctx),
    }
