"""비서 맥락 저장·불러오기(ADR 0007). 온톨로지(엔터티·관계·기억) + RAG 조각을 하이브리드로 찾는다."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.context.embedder import embed
from ttakkari.models import (
    CtxChunk,
    CtxDocument,
    CtxEntity,
    CtxMemory,
    CtxRelation,
    utcnow,
)

ENTITY_TYPES = ("person", "project", "org", "place", "tool", "topic", "preference", "other")
MEMORY_KINDS = ("fact", "preference", "decision", "event", "instruction", "note")
# 벡터와 글자 유사도를 섞는 비율. 한국어 고유명사(프로젝트 이름 등)는 글자 쪽이 잘 잡는다.
W_VEC, W_TRGM = 0.75, 0.25
MIN_SCORE = 0.25
ENTITY_MIN_SCORE = 0.5


class ContextError(ValueError):
    pass


# ---- 엔터티·관계

async def find_entity(db: AsyncSession, name: str, type_: str | None = None) -> CtxEntity | None:
    n = name.strip().lower()
    q = select(CtxEntity).where(or_(func.lower(CtxEntity.name) == n, CtxEntity.aliases.op("@>")([name.strip()])))
    if type_:
        q = q.where(CtxEntity.type == type_)
    return (await db.scalars(q.limit(1))).first()


async def upsert_entity(db: AsyncSession, name: str, type_: str = "other", *, summary: str | None = None,
                        aliases: list[str] | None = None, attributes: dict[str, Any] | None = None,
                        pinned: bool | None = None) -> CtxEntity:
    name = name.strip()
    if not name:
        raise ContextError("이름이 비었어요.")
    if type_ not in ENTITY_TYPES:
        type_ = "other"
    e = await find_entity(db, name)
    if e is None:
        e = CtxEntity(type=type_, name=name[:200], aliases=[], attributes={})
        db.add(e)
    if summary is not None:
        e.summary = summary
    if aliases:
        e.aliases = sorted({*e.aliases, *[a.strip() for a in aliases if a.strip()]})
    if attributes:
        e.attributes = {**e.attributes, **attributes}
    if pinned is not None:
        e.pinned = pinned
    (e.embedding,) = await embed([_entity_text(e)])
    e.updated_at = utcnow()
    await db.flush()
    return e


def _entity_text(e: CtxEntity) -> str:
    return " ".join(x for x in [e.name, *e.aliases, e.summary or ""] if x)


async def link(db: AsyncSession, subject: str, predicate: str, obj: str, *, note: str | None = None,
               source: str = "manual") -> CtxRelation:
    predicate = predicate.strip().lower().replace(" ", "_")[:60]
    if not predicate:
        raise ContextError("관계 이름이 비었어요.")
    s = await find_entity(db, subject) or await upsert_entity(db, subject)
    o = await find_entity(db, obj) or await upsert_entity(db, obj)
    rel = await db.scalar(select(CtxRelation).where(CtxRelation.subject_id == s.id, CtxRelation.predicate == predicate,
                                                    CtxRelation.object_id == o.id))
    if rel is None:
        rel = CtxRelation(subject_id=s.id, predicate=predicate, object_id=o.id, note=note, source=source)
        db.add(rel)
        await db.flush()
    return rel


async def entity_detail(db: AsyncSession, e: CtxEntity) -> dict[str, Any]:
    rels = list(await db.scalars(select(CtxRelation).where(or_(CtxRelation.subject_id == e.id, CtxRelation.object_id == e.id))))
    ids = {r.subject_id for r in rels} | {r.object_id for r in rels}
    names = {x.id: x.name for x in await db.scalars(select(CtxEntity).where(CtxEntity.id.in_(ids)))} if ids else {}
    mems = list(await db.scalars(select(CtxMemory).where(CtxMemory.archived_at.is_(None),
                                                         CtxMemory.entity_ids.op("@>")([str(e.id)])).limit(20)))
    return {
        "id": str(e.id), "type": e.type, "name": e.name, "aliases": e.aliases, "summary": e.summary,
        "attributes": e.attributes, "pinned": e.pinned,
        "relations": [{"subject": names.get(r.subject_id), "predicate": r.predicate, "object": names.get(r.object_id),
                       "note": r.note} for r in rels],
        "memories": [{"id": str(m.id), "kind": m.kind, "content": m.content} for m in mems],
    }


# ---- 기억

async def remember(db: AsyncSession, content: str, *, kind: str = "fact", entities: list[str] | None = None,
                   pinned: bool = False, source: str = "manual", source_ref: str | None = None) -> CtxMemory:
    content = content.strip()
    if not content:
        raise ContextError("내용이 비었어요.")
    if kind not in MEMORY_KINDS:
        kind = "note"
    ids = []
    for n in entities or []:
        if n.strip():
            e = await find_entity(db, n) or await upsert_entity(db, n)
            ids.append(str(e.id))
    m = CtxMemory(kind=kind, content=content[:4000], entity_ids=ids, pinned=pinned, source=source, source_ref=source_ref)
    (m.embedding,) = await embed([content])
    db.add(m)
    await db.flush()
    return m


async def forget(db: AsyncSession, memory_id: uuid.UUID) -> bool:
    m = await db.get(CtxMemory, memory_id)
    if m is None or m.archived_at is not None:
        return False
    m.archived_at = utcnow()
    return True


async def pinned_brief(db: AsyncSession) -> str:
    """모든 Run 에 넣는 짧은 맥락: 고정 기억 + 고정 엔터티 요약. 길면 자른다."""
    lines: list[str] = []
    for m in await db.scalars(select(CtxMemory).where(CtxMemory.pinned.is_(True), CtxMemory.archived_at.is_(None))
                              .order_by(CtxMemory.created_at)):
        lines.append(f"- ({m.kind}) {m.content}")
    for e in await db.scalars(select(CtxEntity).where(CtxEntity.pinned.is_(True)).order_by(CtxEntity.name)):
        lines.append(f"- [{e.type}] {e.name}" + (f": {e.summary}" if e.summary else ""))
    text = "\n".join(lines)
    limit = get_settings().context_brief_chars
    return text if len(text) <= limit else text[:limit] + "\n… (더 있음: recall 도구로 찾기)"


# ---- 불러오기

@dataclass
class Hit:
    kind: str  # memory | chunk | entity
    id: str
    score: float
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


async def recall(db: AsyncSession, query: str, k: int = 8, *, kinds: tuple[str, ...] = ("memory", "chunk", "entity")) -> list[Hit]:
    """벡터(코사인) + 글자(trigram) 하이브리드. 걸린 엔터티는 1단계 관계를 붙인다."""
    query = query.strip()
    if not query:
        return []
    (qv,) = await embed([query], query=True)
    hits: list[Hit] = []
    pool = max(k * 3, 12)

    if "memory" in kinds:
        vec = 1 - CtxMemory.embedding.cosine_distance(qv)
        trg = func.similarity(CtxMemory.content, query)
        score = (W_VEC * vec + W_TRGM * trg).label("score")
        rows = await db.execute(select(CtxMemory, score).where(CtxMemory.archived_at.is_(None), CtxMemory.embedding.is_not(None))
                                .order_by(score.desc()).limit(pool))
        hits += [Hit("memory", str(m.id), float(sc), m.content, {"kind": m.kind, "pinned": m.pinned, "source": m.source})
                 for m, sc in rows]

    if "chunk" in kinds:
        vec = 1 - CtxChunk.embedding.cosine_distance(qv)
        trg = func.similarity(CtxChunk.content, query)
        score = (W_VEC * vec + W_TRGM * trg).label("score")
        rows = await db.execute(select(CtxChunk, CtxDocument.path, CtxDocument.title, score)
                                .join(CtxDocument, CtxDocument.id == CtxChunk.document_id)
                                .where(CtxChunk.embedding.is_not(None)).order_by(score.desc()).limit(pool))
        hits += [Hit("chunk", str(c.id), float(sc), c.content[:1200], {"path": path, "title": title, "heading": c.heading})
                 for c, path, title, sc in rows]

    if "entity" in kinds:
        vec = 1 - CtxEntity.embedding.cosine_distance(qv)
        trg = func.word_similarity(CtxEntity.name, query)
        score = (W_VEC * vec + 0.5 * trg).label("score")
        rows = await db.execute(select(CtxEntity, score).where(CtxEntity.embedding.is_not(None)).order_by(score.desc()).limit(5))
        for e, sc in rows:
            # 엔터티는 이름이 질의에 실제로 걸릴 때만 붙인다(관계까지 딸려 와 길어지므로).
            if float(sc) < ENTITY_MIN_SCORE:
                continue
            d = await entity_detail(db, e)
            rel = "; ".join(f"{r['subject']} {r['predicate']} {r['object']}" for r in d["relations"][:8])
            text = e.name + (f": {e.summary}" if e.summary else "") + (f" | 관계: {rel}" if rel else "")
            hits.append(Hit("entity", str(e.id), float(sc), text, {"entity_type": e.type, "name": e.name}))

    hits = [h for h in hits if h.score >= MIN_SCORE]
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]
