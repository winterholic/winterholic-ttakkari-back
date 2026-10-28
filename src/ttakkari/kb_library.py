"""범용 지식 도서관(kb) 읽기·검색·등록 서비스(ADR 0063 D1: ctx_*·knowledge_* 와 테이블·검색 풀 분리).

검색은 기존 맥락 도서관과 같은 PostgreSQL 수단(simple tsvector + 부분 일치 + word_similarity)을 쓰되
kb_passages 만 본다. 새 검색 엔진·임베딩·재순위는 쓰지 않는다. 품질 gate(v2 KB-E1b/KB7)는 별도이므로
응답에 순위 점수의 의미를 보장하지 않는다.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import (
    KbDocumentAccess,
    KbDocumentAlias,
    KbObservation,
)
from ttakkari.collector.library import (
    LIBRARY_SCOPE,
    WORK_POOL,
    effective_pool,
    legacy_work_upload_key,
    narrowest_pool,
    normalize_pool,
)
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


def access_filter(pool: str = "personal"):
    """직접 ID 로 여는 문서·근거의 접근 조건. 숨김(revocation)·다른 tenant 는 닫고, 회사 엔진(work) pool 은 work 행만 연다.

    C2: personal/work 도서관 구획은 없다. 접근 행이 없는 옛 문서는 소유자와 personal pool 에만 보이고 회사 엔진에는 닫힌다.
    병합 전 옛 work 행(scope=work)도 work 로 읽어 병합 전후 결과가 같다.
    """
    closed = select(KbDocumentAccess.document_id).where(
        KbDocumentAccess.document_id == KbDocument.id,
        or_(KbDocumentAccess.hidden.is_(True), KbDocumentAccess.tenant_id != "owner"),
    ).exists()
    if normalize_pool(pool) != WORK_POOL:
        return ~closed
    work_open = select(KbDocumentAccess.document_id).where(
        KbDocumentAccess.document_id == KbDocument.id, KbDocumentAccess.tenant_id == "owner",
        or_(KbDocumentAccess.pool == WORK_POOL, KbDocumentAccess.scope == "work"),
    ).exists()
    return and_(~closed, work_open)


def _current_revision_filter():
    newer = KbRevision.__table__.alias("newer")
    obs = KbObservation.__table__.alias("current_observation")
    observed = select(obs.c.revision_id).where(obs.c.document_id == KbRevision.document_id).order_by(
        obs.c.event_time.desc(), obs.c.recorded_at.desc(), obs.c.id.desc(),
    ).limit(1).correlate(KbRevision).scalar_subquery()
    legacy = ~select(literal(1)).where(
        newer.c.document_id == KbRevision.document_id, newer.c.rev_no > KbRevision.rev_no
    ).exists()
    return or_(KbRevision.id == observed, and_(observed.is_(None), legacy))


def research_source_purpose(source: KbSource) -> str:
    """Legacy external citations stay references; unclassified local receipts stay research context.

    New research imports always carry an explicit purpose. This is placement, not a
    factuality/quality judgement, and never changes source bytes or citation access.
    """
    purpose = (source.origin_meta or {}).get("library_purpose")
    if purpose is not None:
        return purpose
    return ("knowledge_reference" if re.match(r"^https?://[^/\s;]+", source.locator or "", re.IGNORECASE)
            else "research_context")


def _research_source_is_reference(source_model=KbSource):
    """SQL expression returning True if the source is reusable knowledge/reference."""
    purpose = source_model.origin_meta["library_purpose"].astext
    legacy_external = func.coalesce(source_model.locator.op("~*")(r"^https?://[^/\s;]+"), False)
    reference = or_(purpose == "knowledge_reference", and_(purpose.is_(None), legacy_external))
    return or_(source_model.kind != "research_text", func.coalesce(reference, False))


def _source_is_research_context(source_model=KbSource):
    """SQL expression returning True if the source is research-only context (not reusable knowledge)."""
    purpose = source_model.origin_meta["library_purpose"].astext
    legacy_external = func.coalesce(source_model.locator.op("~*")(r"^https?://[^/\s;]+"), False)
    reference = or_(purpose == "knowledge_reference", and_(purpose.is_(None), legacy_external))
    return and_(source_model.kind == "research_text", ~func.coalesce(reference, False))


def research_context_revision_filter(revision_cls=KbRevision):
    """Matches revisions whose source is research-only context."""
    return select(KbSource.id).where(
        KbSource.id == revision_cls.source_id,
        _source_is_research_context(KbSource),
    ).correlate(revision_cls).exists()


def research_context_filter():
    """Matches documents whose CURRENT revision is research-only context."""
    return select(KbRevision.id).join(KbSource, KbSource.id == KbRevision.source_id).where(
        KbRevision.document_id == KbDocument.id,
        _current_revision_filter(),
        _source_is_research_context(KbSource),
    ).correlate(KbDocument).exists()


def library_filter(pool: str = "personal"):
    """목록·검색·집계 조건. access_filter 에 더해, 본문이 canonical 에 모두 있는 병합 중복(covered)은 한 번만 보이도록 뺀다.
    중복 문서와 그 근거 ID 는 지워지지 않아 ID 로는 계속 열린다."""
    covered = select(KbDocumentAlias.document_id).where(
        KbDocumentAlias.document_id == KbDocument.id, KbDocumentAlias.relation == "covered").exists()
    # Research context uses the same immutable byte/citation store, not the general
    # knowledge discovery pool. Keep direct IDs open for research resume and quotes.
    return and_(access_filter(pool), ~covered, ~research_context_filter())


async def resolve_doc_key(db, new_key: str, legacy_keys: list[str]) -> str:
    """같은 출처의 문서가 옛 scope 별 키로 이미 있으면 그 계보의 키를 쓴다(병합 전후 모두).
    여러 개면 가장 먼저 만든 문서, alias 로 합쳐졌다면 canonical 문서의 키다."""
    docs = list(await db.scalars(select(KbDocument).where(KbDocument.doc_key.in_([new_key, *legacy_keys]))
                                 .order_by(KbDocument.created_at, KbDocument.id)))
    if not docs:
        return new_key
    chosen = docs[0]
    alias = await db.get(KbDocumentAlias, chosen.id)
    if alias is not None:
        canonical = await db.get(KbDocument, alias.canonical_document_id)
        if canonical is not None:
            chosen = canonical
    return chosen.doc_key


async def require_document_access(db, document_id, pool="personal"):
    if not await db.scalar(select(KbDocument.id).where(KbDocument.id == document_id, access_filter(pool))):
        raise KbNotFound("document")


async def set_engine_access(db, document_id, *, company_engine: bool) -> str:
    """접근 pool 만 바꾼다. 문서·리비전은 건드리지 않으며 숨김(revocation) 상태도 그대로 둔다."""
    if await db.get(KbDocument, document_id) is None:
        raise KbNotFound("document")
    pool = WORK_POOL if company_engine else "personal"
    access = await db.get(KbDocumentAccess, document_id)
    if access is None:
        db.add(KbDocumentAccess(document_id=document_id, tenant_id="owner", scope=LIBRARY_SCOPE, pool=pool))
    else:
        access.pool = pool
        if access.scope == "work" and not company_engine:
            access.scope = LIBRARY_SCOPE  # 병합 전 옛 값이 work 로 다시 읽히지 않게 한다.
    return pool


async def document_aliases(db, document_id) -> list[dict]:
    """같은 출처로 합쳐진 문서(양방향). 본문이 다른 이력(variant)과 숨겨진 중복(covered)을 구분해 알린다."""
    rows = (await db.execute(select(KbDocumentAlias).where(or_(
        KbDocumentAlias.document_id == document_id, KbDocumentAlias.canonical_document_id == document_id)))).scalars()
    return [{"document_id": str(a.canonical_document_id if a.document_id == document_id else a.document_id),
             "role": "canonical" if a.document_id == document_id else "duplicate",
             "relation": a.relation, "reason": a.reason} for a in rows]


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


async def summary(db: AsyncSession, *, pool: str = "personal") -> dict:
    counts = {}
    for name, model in (("documents", KbDocument), ("revisions", KbRevision), ("passages", KbPassage), ("evidence", KbEvidence)):
        stmt = select(func.count()).select_from(model)
        if model is KbEvidence:
            stmt = stmt.join(KbPassage, KbEvidence.passage_id == KbPassage.id)
        if model in (KbPassage, KbEvidence):
            stmt = stmt.join(KbRevision, KbPassage.revision_id == KbRevision.id)
        if model is not KbDocument:
            stmt = stmt.join(KbDocument, KbRevision.document_id == KbDocument.id)
            stmt = stmt.where(~research_context_revision_filter(KbRevision))
        counts[name] = await db.scalar(stmt.where(library_filter(pool))) or 0
    src = select(KbSource.id, KbSource.collected_at).join(KbRevision, KbSource.id == KbRevision.source_id).join(
        KbDocument, KbRevision.document_id == KbDocument.id).where(
        library_filter(pool), ~_source_is_research_context(KbSource)).distinct().subquery()
    counts["sources"] = await db.scalar(select(func.count()).select_from(src)) or 0
    last = await db.scalar(select(func.max(src.c.collected_at)))
    return {"counts": counts, "last_collected_at": last.isoformat() if last else None}


async def list_documents(db: AsyncSession, *, q: str | None, limit: int, offset: int, pool: str = "personal") -> dict:
    stmt = select(KbDocument).where(library_filter(pool))
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
    observed = await db.scalar(select(KbObservation.revision_id).where(KbObservation.document_id == document_id)
                               .order_by(KbObservation.event_time.desc(), KbObservation.recorded_at.desc(), KbObservation.id.desc()).limit(1))
    if observed is not None:
        return await db.get(KbRevision, observed)
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
    passage_limit: int, passage_offset: int, pool: str = "personal",
) -> dict:
    await require_document_access(db, document_id, pool)
    doc = await db.get(KbDocument, document_id)
    if doc is None:
        raise KbNotFound("document")
    revs = list(
        await db.scalars(select(KbRevision).where(KbRevision.document_id == doc.id).order_by(KbRevision.rev_no.desc()))
    )
    if not revs:
        raise KbNotFound("revision")
    current = await _latest_revision(db, doc.id)
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
        "observations": [{"envelope_id": str(o.envelope_id), "revision_id": str(o.revision_id),
                          "event_time": o.event_time.isoformat(), "provenance": o.provenance}
                         for o in await db.scalars(select(KbObservation).where(KbObservation.document_id == doc.id)
                                                   .order_by(KbObservation.event_time.desc()).limit(100))],
        "revisions": revisions,
        "aliases": await document_aliases(db, doc.id),
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
    document_id: uuid.UUID | None, current_only: bool, pool: str = "personal",
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
        .where(predicate, library_filter(pool), ~research_context_revision_filter(KbRevision))
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
    latest = {doc_id: (await _latest_revision(db, doc_id)).id for doc_id in {d.id for _, _, d, _ in rows}}
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
                "id": str(rev.id), "rev_no": rev.rev_no, "current": rev.id == latest.get(doc.id),
                "char_count": rev.char_count,
            },
            "source": _source_out(src) if src else None,
        })
    return {"query": query, "terms": words, "items": items, "limit": limit, "offset": offset, "has_more": has_more}


def _line_number(body: str, offset: int) -> int:
    return body.count("\n", 0, offset) + 1


async def get_evidence(
    db: AsyncSession, passage_id: uuid.UUID, *, start: int | None, end: int | None, pool: str = "personal",
) -> dict:
    passage = await db.get(KbPassage, passage_id)
    if passage is None:
        raise KbNotFound("passage")
    rev = await db.get(KbRevision, passage.revision_id)
    await require_document_access(db, rev.document_id, pool)
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
    locator: str | None, pool: str = "personal",
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
    # 옛 work 업로드는 키가 달랐다. 같은 업로드가 이미 있으면 그 계보에 이어 도서관에 같은 글이 둘 생기지 않게 한다.
    key = await resolve_doc_key(db, key, [legacy_work_upload_key(key)])
    existing = await db.scalar(select(KbDocument).where(KbDocument.doc_key == key))
    if existing is not None:
        await require_document_access(db, existing.id, pool)
    result = await ingest_source(
        db, source_key=key, data=data, kind="upload", locator=locator, media_type="text/plain",
        title=title, lang=lang, origin_meta={"via": "kb_library_api"},
    )
    access = await db.get(KbDocumentAccess, result.document_id)
    if access is None:
        db.add(KbDocumentAccess(document_id=result.document_id, tenant_id="owner", scope=LIBRARY_SCOPE,
                                pool=normalize_pool(pool)))
    elif access.tenant_id != "owner":
        raise KbInputError("document_scope_mismatch")
    elif result.created:
        # 새 본문이 들어오면 접근 범위는 좁은 쪽이다. 같은 바이트 재등록은 아무것도 바꾸지 않는다.
        access.pool = narrowest_pool(effective_pool(access.pool, access.scope), pool)
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
