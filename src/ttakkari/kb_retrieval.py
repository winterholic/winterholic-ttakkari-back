"""Scoped lexical/dense fusion. Scores are ranking signals, never factuality or calibrated confidence.

PG exact cosine is a bounded-corpus operational path, not the million-passage engine selection.
That separate E0.5 quality/scale gate remains unverified. Failed projections are visible and retriable.
"""
import asyncio
import math

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from ttakkari import kb_library as lib
from ttakkari.config import get_settings
from ttakkari.context import embedder
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument, KbPassage, KbRevision, KbSource
from ttakkari.kb_vector_models import KbPassageVector as V
from ttakkari.model_process import run_json_worker
from ttakkari.models import EMBED_DIM

_lock = asyncio.Lock()
_last_error: str | None = None
_failed_passages: dict[str, str] = {}


def model_key():
    snapshot = get_settings().kb_embedding_snapshot if get_settings().context_embedder == "e5" else "test"
    return embedder.model_name() + ":kb-full-v1:" + snapshot


def windows(text):
    # Deterministic lexical/hash test backend; the real model worker splits by exact token counts.
    return [text[i:i + 384] for i in range(0, max(1, len(text) - 64), 320)]


async def embed_complete(texts, *, query=False):
    if not embedder.enabled():
        raise RuntimeError("kb_semantic_disabled")
    if any(len(text) > 50000 for text in texts):
        raise ValueError("passage_too_long_for_semantic_projection")
    s = get_settings()
    if s.context_embedder == "e5":
        result = await run_json_worker(module="ttakkari.kb_embedding_worker",
            request={"texts": texts, "query": query, "model": s.context_model,
                     "cache_dir": str(s.models_dir), "threads": s.context_embedding_cpu_threads,
                     "snapshot": s.kb_embedding_snapshot},
            timeout=s.context_embedding_timeout_seconds, error_prefix="kb_embedding")
        if result.get("coverage") != "full":
            raise ValueError("kb_embedding_incomplete")
        vectors = result["vectors"]
    else:
        vectors = await embedder.embed(texts, query=query)
    if len(vectors) != len(texts) or any(len(v) != EMBED_DIM or not all(math.isfinite(x) for x in v) for v in vectors):
        raise ValueError("kb_embedding_invalid")
    return vectors


async def index_pending(*, limit=16):
    global _last_error
    async with _lock:
        async with sessionmaker()() as db:
            indexed = select(V.passage_id).where(V.passage_id == KbPassage.id, V.model_key == model_key()).exists()
            query = select(KbPassage).join(KbRevision).join(KbDocument).where(
                ~indexed, lib.library_filter(),
            ).order_by(KbPassage.created_at, KbPassage.id)
            if _failed_passages:
                query = query.where(KbPassage.id.not_in(_failed_passages.keys()))
            passages = list(await db.scalars(query.limit(limit)))
        if not passages:
            return 0
        try:
            vectors = await embed_complete([p.body for p in passages])
        except Exception as exc:
            # Isolate permanent per-passage failures so one bad document cannot block
            # later rows. Transient/model-wide failures still abort and remain visible.
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
                await db.execute(insert(V).values(passage_id=passage.id, model_key=model_key(),
                    text_sha256=passage.sha256, vector=vector).on_conflict_do_nothing())
            await db.commit()
        _last_error = None if not _failed_passages else "ValueError:passage_too_long_for_semantic_projection"
        return len(passages)


async def status(db, pool="personal"):
    total = await db.scalar(select(func.count()).select_from(KbPassage).join(KbRevision).join(KbDocument)
                            .where(lib.library_filter(pool))) or 0
    indexed = await db.scalar(select(func.count()).select_from(V).join(KbPassage).join(KbRevision).join(KbDocument)
                              .where(V.model_key == model_key(), V.text_sha256 == KbPassage.sha256,
                                     lib.library_filter(pool))) or 0
    passage_ids = {str(passage_id) for passage_id in await db.scalars(
        select(KbPassage.id).join(KbRevision).join(KbDocument).where(lib.library_filter(pool)))}
    failed = len(passage_ids.intersection(_failed_passages))
    return {"model": embedder.model_name(), "model_key": model_key(), "indexed": indexed, "total": total, "failed": failed,
            "enabled": get_settings().kb_semantic_enabled, "last_error": _last_error,
            "quality_gate": "unverified", "engine": "postgresql_exact_cosine"}


async def search(db, *, query, limit=10, offset=0, document_id=None, current_only=True, pool="personal"):
    if not get_settings().kb_semantic_enabled:
        raise RuntimeError("kb_semantic_disabled")
    if offset > 200:
        raise ValueError("kb_hybrid_offset_limit")
    [vector] = await embed_complete([query], query=True)
    lexical = await lib.search_passages(db, query=query, limit=200, offset=0,
                                        document_id=document_id, current_only=current_only, pool=pool)
    cosine = V.vector.cosine_distance(vector).label("distance")
    stmt = select(KbPassage, KbRevision, KbDocument, cosine).join(V, V.passage_id == KbPassage.id).join(
        KbRevision, KbPassage.revision_id == KbRevision.id).join(KbDocument, KbRevision.document_id == KbDocument.id).where(
            V.model_key == model_key(), V.text_sha256 == KbPassage.sha256, lib.library_filter(pool))
    if current_only:
        stmt = stmt.where(lib._current_revision_filter())
    if document_id is not None:
        stmt = stmt.where(KbDocument.id == document_id)
    dense = (await db.execute(stmt.order_by(cosine, KbPassage.id).limit(200))).all()
    scores, items = {}, {}
    for rank, item in enumerate(lexical["items"], 1):
        pid = item["passage_id"]
        scores[pid], items[pid] = 1 / (60 + rank), item
    for rank, (passage, revision, document, distance) in enumerate(dense, 1):
        pid = str(passage.id)
        scores[pid] = scores.get(pid, 0) + 1 / (60 + rank)
        if pid not in items:
            source = await db.get(KbSource, revision.source_id)
            snippet, marks = lib.snippet_with_highlights(passage.body, lexical["terms"])
            current = await lib._latest_revision(db, document.id)
            items[pid] = {"passage_id": pid, "ord": passage.ord, "heading": passage.heading,
                          "snippet": snippet, "highlights": marks, "char_start": passage.char_start,
                          "char_end": passage.char_end,
                          "document": {"id": str(document.id), "title": document.title, "lang": document.lang},
                          "revision": {"id": str(revision.id), "rev_no": revision.rev_no,
                                       "current": current.id == revision.id, "char_count": revision.char_count},
                          "source": lib._source_out(source)}
    ranked = sorted(items, key=lambda pid: (-scores[pid], pid))
    hits = [{**items[pid], "score": scores[pid]} for pid in ranked[offset:offset + limit]]
    return {**lexical, "items": hits, "limit": limit, "offset": offset,
            "has_more": len(ranked) > offset + limit, "retrieval": await status(db, pool)}
