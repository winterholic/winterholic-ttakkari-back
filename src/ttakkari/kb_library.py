"""범용 지식 도서관(kb) 읽기·검색·등록 서비스(ADR 0063 D1: ctx_*·knowledge_* 와 테이블·검색 풀 분리).

검색은 기존 맥락 도서관과 같은 PostgreSQL 수단(simple tsvector + 부분 일치 + word_similarity)을 쓰되
kb_passages 만 본다. 새 검색 엔진·임베딩·재순위는 쓰지 않는다. 품질 gate(v2 KB-E1b/KB7)는 별도이므로
응답에 순위 점수의 의미를 보장하지 않는다.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context.search import lexical, terms
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.kb_store import (
    KbInputError,
    ingest_source,
    normalize_text,
    segment_revision,
    sha256_hex,
    span_digest,
)

SNIPPET_CHARS = 240
EVIDENCE_CONTEXT_CHARS = 200
MAX_TEXT_CHARS = 2_000_000


class KbNotFound(LookupError):
    """요청한 문서·리비전·패시지가 없다."""


def _current_revision_filter():
    """문서별 최대 rev_no 만 현재 리비전이다(kb_models 의 현재성 규칙)."""
    newer = KbRevision.__table__.alias("newer")
    return ~select(literal(1)).where(
        newer.c.document_id == KbRevision.document_id, newer.c.rev_no > KbRevision.rev_no
    ).exists()


def snippet_with_highlights(body: str, words: list[str]) -> tuple[str, list[list[int]]]:
    """첫 일치 주변의 짧은 발췌와 그 안의 강조 구간([start, end), 발췌 기준 코드포인트)을 만든다."""
    lowered = body.casefold()
    hits: list[tuple[int, int]] = []
    for word in words:
        pos = lowered.find(word)
        while pos != -1 and len(hits) < 64:
            hits.append((pos, pos + len(word)))
            pos = lowered.find(word, pos + len(word))
    # casefold 가 길이를 바꾸는 문자(예: ß)는 발췌 좌표가 어긋날 수 있어 길이가 같을 때만 신뢰한다.
    if len(lowered) != len(body):
        hits = []
    hits.sort()
    start = max(hits[0][0] - SNIPPET_CHARS // 4, 0) if hits else 0
    end = min(start + SNIPPET_CHARS, len(body))
    text = body[start:end]
    marks: list[list[int]] = []
    last_end = -1
    for h_start, h_end in hits:
        if h_start < start or h_end > end or h_start < last_end:
            continue
        marks.append([h_start - start, h_end - start])
        last_end = h_end
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    if prefix:
        marks = [[a + 1, b + 1] for a, b in marks]
    return prefix + text + suffix, marks


def _source_out(src: KbSource) -> dict:
    return {
        "id": str(src.id),
        "kind": src.kind,
        "source_key": src.source_key,
        "locator": src.locator,
        "sha256": src.sha256,
        "byte_size": src.byte_size,
        "media_type": src.media_type,
        "collected_at": src.collected_at.isoformat(),
    }


async def summary(db: AsyncSession) -> dict:
    counts = {}
    for name, model in (("sources", KbSource), ("documents", KbDocument), ("revisions", KbRevision),
                        ("passages", KbPassage), ("evidence", KbEvidence)):
        counts[name] = await db.scalar(select(func.count()).select_from(model)) or 0
    last = await db.scalar(select(func.max(KbSource.collected_at)))
    return {"counts": counts, "last_collected_at": last.isoformat() if last else None}


async def list_documents(db: AsyncSession, *, q: str | None, limit: int, offset: int) -> dict:
    stmt = select(KbDocument)
    if q and q.strip():
        pattern = q.strip()
        stmt = stmt.where(
            KbDocument.title.icontains(pattern, autoescape=True)
            | KbDocument.doc_key.icontains(pattern, autoescape=True)
        )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    docs = list(
        await db.scalars(stmt.order_by(KbDocument.updated_at.desc(), KbDocument.id).offset(offset).limit(limit))
    )
    items = [await _document_row(db, doc) for doc in docs]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def _latest_revision(db: AsyncSession, document_id: uuid.UUID) -> KbRevision | None:
    return await db.scalar(
        select(KbRevision).where(KbRevision.document_id == document_id).order_by(KbRevision.rev_no.desc()).limit(1)
    )


async def _document_row(db: AsyncSession, doc: KbDocument) -> dict:
    rev = await _latest_revision(db, doc.id)
    row = {
        "id": str(doc.id),
        "doc_key": doc.doc_key,
        "title": doc.title,
        "lang": doc.lang,
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "current_revision": None,
    }
    if rev is not None:
        src = await db.get(KbSource, rev.source_id)
        passages = await db.scalar(select(func.count()).select_from(KbPassage).where(KbPassage.revision_id == rev.id))
        row["current_revision"] = {
            "id": str(rev.id),
            "rev_no": rev.rev_no,
            "char_count": rev.char_count,
            "passage_count": passages or 0,
            "created_at": rev.created_at.isoformat(),
            "source": _source_out(src) if src else None,
        }
    return row


async def get_document(
    db: AsyncSession, document_id: uuid.UUID, *, revision_id: uuid.UUID | None,
    passage_limit: int, passage_offset: int,
) -> dict:
    doc = await db.get(KbDocument, document_id)
    if doc is None:
        raise KbNotFound("document")
    revs = list(
        await db.scalars(select(KbRevision).where(KbRevision.document_id == doc.id).order_by(KbRevision.rev_no.desc()))
    )
    if not revs:
        raise KbNotFound("revision")
    current = revs[0]
    selected = current
    if revision_id is not None:
        selected = next((r for r in revs if r.id == revision_id), None)
        if selected is None:
            raise KbNotFound("revision")
    counts = dict(
        (await db.execute(
            select(KbPassage.revision_id, func.count()).where(KbPassage.revision_id.in_([r.id for r in revs]))
            .group_by(KbPassage.revision_id)
        )).all()
    )
    sources = {s.id: s for s in await db.scalars(select(KbSource).where(KbSource.id.in_([r.source_id for r in revs])))}
    revisions = [
        {
            "id": str(r.id),
            "rev_no": r.rev_no,
            "parser_version": r.parser_version,
            "char_count": r.char_count,
            "text_sha256": r.text_sha256,
            "passage_count": counts.get(r.id, 0),
            "current": r.id == current.id,
            "supersedes_id": str(r.supersedes_id) if r.supersedes_id else None,
            "created_at": r.created_at.isoformat(),
            "source": _source_out(sources[r.source_id]) if r.source_id in sources else None,
        }
        for r in revs
    ]
    passages = list(
        await db.scalars(
            select(KbPassage).where(KbPassage.revision_id == selected.id)
            .order_by(KbPassage.ord).offset(passage_offset).limit(passage_limit)
        )
    )
    return {
        "id": str(doc.id),
        "doc_key": doc.doc_key,
        "title": doc.title,
        "lang": doc.lang,
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "revisions": revisions,
        "selected_revision_id": str(selected.id),
        "passages": {
            "items": [
                {"id": str(p.id), "ord": p.ord, "heading": p.heading, "body": p.body,
                 "char_start": p.char_start, "char_end": p.char_end, "sha256": p.sha256}
                for p in passages
            ],
            "total": counts.get(selected.id, 0),
            "limit": passage_limit,
            "offset": passage_offset,
        },
    }


async def search_passages(
    db: AsyncSession, *, query: str, limit: int, offset: int,
    document_id: uuid.UUID | None, current_only: bool,
) -> dict:
    words = terms(query)
    if not words:
        return {"query": query, "terms": [], "items": [], "limit": limit, "offset": offset, "has_more": False}
    predicate, score = lexical(KbPassage.body, query, words)
    score = score.label("score")
    stmt = (
        select(KbPassage, KbRevision, KbDocument, score)
        .join(KbRevision, KbRevision.id == KbPassage.revision_id)
        .join(KbDocument, KbDocument.id == KbRevision.document_id)
        .where(predicate)
    )
    if current_only:
        stmt = stmt.where(_current_revision_filter())
    if document_id is not None:
        stmt = stmt.where(KbDocument.id == document_id)
    rows = (
        await db.execute(
            stmt.order_by(score.desc(), KbDocument.id, KbPassage.ord).offset(offset).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    sources = {
        s.id: s
        for s in await db.scalars(select(KbSource).where(KbSource.id.in_({rev.source_id for _, rev, _, _ in rows})))
    }
    latest = {
        doc_id: n
        for doc_id, n in (
            await db.execute(
                select(KbRevision.document_id, func.max(KbRevision.rev_no))
                .where(KbRevision.document_id.in_({doc.id for _, _, doc, _ in rows}))
                .group_by(KbRevision.document_id)
            )
        ).all()
    }
    items = []
    for passage, rev, doc, raw_score in rows[:limit]:
        text, marks = snippet_with_highlights(passage.body, words)
        src = sources.get(rev.source_id)
        items.append({
            "passage_id": str(passage.id),
            "ord": passage.ord,
            "heading": passage.heading,
            "snippet": text,
            "highlights": marks,
            "score": round(float(raw_score), 4),
            "char_start": passage.char_start,
            "char_end": passage.char_end,
            "document": {"id": str(doc.id), "title": doc.title, "lang": doc.lang},
            "revision": {
                "id": str(rev.id), "rev_no": rev.rev_no, "current": rev.rev_no == latest.get(doc.id),
                "char_count": rev.char_count,
            },
            "source": _source_out(src) if src else None,
        })
    return {"query": query, "terms": words, "items": items, "limit": limit, "offset": offset, "has_more": has_more}


def _line_number(body: str, offset: int) -> int:
    return body.count("\n", 0, offset) + 1


async def get_evidence(
    db: AsyncSession, passage_id: uuid.UUID, *, start: int | None, end: int | None,
) -> dict:
    passage = await db.get(KbPassage, passage_id)
    if passage is None:
        raise KbNotFound("passage")
    rev = await db.get(KbRevision, passage.revision_id)
    doc = await db.get(KbDocument, rev.document_id)
    src = await db.get(KbSource, rev.source_id)
    span_start = 0 if start is None else start
    span_end = len(passage.body) if end is None else end
    if not (0 <= span_start < span_end <= len(passage.body)):
        raise KbInputError("span_out_of_passage")
    abs_start = passage.char_start + span_start
    abs_end = passage.char_start + span_end
    body = rev.body
    registered = list(await db.scalars(select(KbEvidence).where(KbEvidence.passage_id == passage.id)))
    return {
        "passage_id": str(passage.id),
        "document": {"id": str(doc.id), "title": doc.title},
        "revision": {"id": str(rev.id), "rev_no": rev.rev_no, "text_sha256": rev.text_sha256},
        "source": _source_out(src) if src else None,
        "span": {
            "passage_start": span_start,
            "passage_end": span_end,
            "revision_start": abs_start,
            "revision_end": abs_end,
            "line_start": _line_number(body, abs_start),
            "line_end": _line_number(body, abs_end - 1),
            "sha256": span_digest(passage.body, span_start, span_end),
            # 저장 본문 슬라이스의 해시가 패시지·리비전 양쪽에서 일치하는지 대조한다. 명제 지지 확인은 아니다.
            "hash_verified": body[abs_start:abs_end] == passage.body[span_start:span_end]
            and sha256_hex(passage.body.encode("utf-8")) == passage.sha256,
        },
        "quote": passage.body[span_start:span_end],
        "before": body[max(abs_start - EVIDENCE_CONTEXT_CHARS, 0):abs_start],
        "after": body[abs_end:abs_end + EVIDENCE_CONTEXT_CHARS],
        "registered_evidence": [
            {"id": str(e.id), "span_start": e.span_start, "span_end": e.span_end, "sha256": e.sha256, "note": e.note}
            for e in registered
        ],
    }


@dataclass
class RegisterResult:
    document_id: uuid.UUID
    revision_id: uuid.UUID
    rev_no: int
    created: bool
    passage_count: int


async def register_text(
    db: AsyncSession, *, title: str, text: str, source_key: str | None, lang: str | None,
    locator: str | None,
) -> RegisterResult:
    """사용자가 붙여넣거나 올린 텍스트를 기존 수집 경로(ingest_source)로 등록하고 패시지로 나눈다.

    같은 source_key·같은 바이트는 기존 행을 돌려주고(멱등), 같은 source_key 에 다른 바이트는 새 리비전이다.
    source_key 를 생략하면 본문 해시로 정해 같은 글을 두 번 올려도 문서가 늘지 않는다.
    """
    # 저장 정규화가 CRLF 만 LF 로 바꾸므로 같은 NFKC 정규화 결과의 길이를 미리 본다.
    if len(normalize_text(text.encode("utf-8"))) > MAX_TEXT_CHARS:
        raise KbInputError("text_too_long")
    data = text.encode("utf-8")
    key = source_key or f"upload:{hashlib.sha256(data).hexdigest()[:16]}"
    result = await ingest_source(
        db, source_key=key, data=data, kind="upload", locator=locator, media_type="text/plain",
        title=title, lang=lang, origin_meta={"via": "kb_library_api"},
    )
    passages = await segment_revision(db, result.revision_id)
    return RegisterResult(
        document_id=result.document_id, revision_id=result.revision_id, rev_no=result.rev_no,
        created=result.created, passage_count=len(passages),
    )


async def segment_unsegmented(db: AsyncSession) -> dict:
    """패시지가 하나도 없는 리비전을 나눈다. 수집 경로가 나누기를 빼먹은 기존 문서를 검색 가능하게 한다(멱등)."""
    has_passage = select(KbPassage.id).where(KbPassage.revision_id == KbRevision.id).exists()
    ids = list(await db.scalars(select(KbRevision.id).where(~has_passage).order_by(KbRevision.created_at)))
    created = 0
    for revision_id in ids:
        created += len(await segment_revision(db, revision_id))
    return {"revisions_checked": len(ids), "passages": created}
