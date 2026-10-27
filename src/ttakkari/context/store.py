"""비서 맥락 저장·불러오기(ADR 0007). 온톨로지(엔터티·관계·기억) + RAG 조각을 하이브리드로 찾는다."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Text, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.context.search import lexical, scope_clause, terms
from ttakkari.models import (
    CtxChunk,
    CtxDocument,
    CtxEntity,
    CtxMemory,
    CtxRelation,
    CtxSource,
    utcnow,
)

ENTITY_TYPES = ("person", "project", "org", "place", "tool", "topic", "preference", "other")
MEMORY_KINDS = ("fact", "preference", "decision", "event", "instruction", "note")


class ContextError(ValueError):
    pass


# ---- 엔터티·관계

async def find_entity(db: AsyncSession, name: str, type_: str | None = None, *, scope: str = "all") -> CtxEntity | None:
    n = name.strip().lower()
    aliases = func.jsonb_array_elements_text(CtxEntity.aliases).table_valued("value").alias("alias")
    alias_match = select(1).select_from(aliases).where(func.lower(aliases.c.value) == n).exists()
    q = select(CtxEntity).where(or_(func.lower(CtxEntity.name) == n, alias_match), scope_clause(CtxEntity.scope, scope))
    if type_:
        q = q.where(CtxEntity.type == type_)
    return (await db.scalars(q.limit(1))).first()


async def upsert_entity(db: AsyncSession, name: str, type_: str = "other", *, summary: str | None = None,
                        aliases: list[str] | None = None, attributes: dict[str, Any] | None = None,
                        pinned: bool | None = None, scope: str = "personal") -> CtxEntity:
    name = name.strip()
    if not name:
        raise ContextError("이름이 비었어요.")
    if type_ not in ENTITY_TYPES:
        type_ = "other"
    e = await find_entity(db, name)
    # 같은 소유자의 개인/회사 분류는 표시용이라 기존 항목을 찾으면 그 분류를 유지하고 접근을 허용한다.
    if e is None:
        e = CtxEntity(type=type_, name=name[:200], aliases=[], attributes={}, scope=scope)
        db.add(e)
    elif type_ != "other":
        e.type = type_
    if summary is not None:
        e.summary = summary
    if aliases:
        e.aliases = sorted({*e.aliases, *[a.strip() for a in aliases if a.strip()]})
    if attributes:
        e.attributes = {**e.attributes, **attributes}
    if pinned is not None:
        e.pinned = pinned
    e.updated_at = utcnow()
    await db.flush()
    return e


async def link(db: AsyncSession, subject: str, predicate: str, obj: str, *, note: str | None = None,
               source: str = "manual", scope: str = "all") -> CtxRelation:
    predicate = predicate.strip().lower().replace(" ", "_")[:60]
    if not predicate:
        raise ContextError("관계 이름이 비었어요.")
    target_scope = "work" if scope == "work" else "personal"
    s = await find_entity(db, subject) or await upsert_entity(db, subject, scope=target_scope)
    o = await find_entity(db, obj) or await upsert_entity(db, obj, scope=target_scope)
    rel = await db.scalar(select(CtxRelation).where(CtxRelation.subject_id == s.id, CtxRelation.predicate == predicate,
                                                    CtxRelation.object_id == o.id))
    if rel is None:
        rel = CtxRelation(subject_id=s.id, predicate=predicate, object_id=o.id, note=note, source=source)
        db.add(rel)
        await db.flush()
    return rel


async def entity_detail(db: AsyncSession, e: CtxEntity, *, scope: str = "all") -> dict[str, Any]:
    rels = list(await db.scalars(select(CtxRelation).where(or_(CtxRelation.subject_id == e.id, CtxRelation.object_id == e.id))))
    ids = {r.subject_id for r in rels} | {r.object_id for r in rels}
    names = {x.id: x.name for x in await db.scalars(select(CtxEntity).where(CtxEntity.id.in_(ids),
                                                                          scope_clause(CtxEntity.scope, scope)))} if ids else {}
    rels = [r for r in rels if r.subject_id in names and r.object_id in names]
    mems = list(await db.scalars(select(CtxMemory).where(CtxMemory.archived_at.is_(None),
                                                         CtxMemory.entity_ids.op("@>")([str(e.id)]),
                                                         scope_clause(CtxMemory.scope, scope)).limit(20)))
    return {
        "id": str(e.id), "type": e.type, "name": e.name, "aliases": e.aliases, "summary": e.summary,
        "attributes": e.attributes, "pinned": e.pinned, "scope": e.scope,
        "relations": [{"subject": names.get(r.subject_id), "predicate": r.predicate, "object": names.get(r.object_id),
                       "note": r.note} for r in rels],
        "memories": [{"id": str(m.id), "kind": m.kind, "content": m.content} for m in mems],
    }


# ---- 기억

async def remember(db: AsyncSession, content: str, *, kind: str = "fact", entities: list[str] | None = None,
                   pinned: bool = False, source: str = "manual", source_ref: str | None = None,
                   scope: str = "personal") -> CtxMemory:
    content = content.strip()
    if not content:
        raise ContextError("내용이 비었어요.")
    if kind not in MEMORY_KINDS:
        kind = "note"
    ids = []
    for n in entities or []:
        if n.strip():
            e = await find_entity(db, n) or await upsert_entity(db, n, scope=scope)
            ids.append(str(e.id))
    m = CtxMemory(kind=kind, content=content[:4000], entity_ids=ids, pinned=pinned, source=source, source_ref=source_ref, scope=scope)
    db.add(m)
    await db.flush()
    return m


async def forget(db: AsyncSession, memory_id: uuid.UUID) -> bool:
    m = await db.get(CtxMemory, memory_id)
    if m is None or m.archived_at is not None:
        return False
    m.archived_at = utcnow()
    return True


async def pinned_brief(db: AsyncSession, *, scope: str = "all") -> str:
    """모든 Run 에 넣는 짧은 맥락: 고정 기억 + 고정 엔터티 요약. 길면 자른다."""
    lines: list[str] = []
    for m in await db.scalars(select(CtxMemory).where(CtxMemory.pinned.is_(True), CtxMemory.archived_at.is_(None),
                                                   scope_clause(CtxMemory.scope, scope))
                              .order_by(CtxMemory.created_at)):
        lines.append(f"- ({m.kind}) {m.content}")
    for e in await db.scalars(select(CtxEntity).where(CtxEntity.pinned.is_(True), scope_clause(CtxEntity.scope, scope)).order_by(CtxEntity.name)):
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


async def recall(db: AsyncSession, query: str, k: int = 8, *,
                 kinds: tuple[str, ...] = ("memory", "chunk", "entity", "meeting"), scope: str = "all",
                 max_k: int = 30) -> list[Hit]:
    """Lexical candidates + explicit entity aliases and one-hop context. 회의록 조각만 벡터 유사도를 더한다(ADR 0021)."""
    query = query.strip()
    words = terms(query)
    if not words:
        return []
    k = min(max(k, 1), max_k)
    pool = max(k * 4, 20)
    hits: list[Hit] = []
    entity_text = func.concat_ws(" ", CtxEntity.name, cast(CtxEntity.aliases, Text), CtxEntity.summary)
    pred, score = lexical(entity_text, query, words)
    entities = list((await db.execute(select(CtxEntity, score.label("score"))
                                    .where(scope_clause(CtxEntity.scope, scope), pred)
                                    .order_by(score.desc(), CtxEntity.name).limit(8))).all())
    # Only names/aliases present in the query anchor graph expansion; summary matches do not.
    anchors = [e for e, _ in entities if any(
        name.casefold() in query.casefold() for name in (e.name, *e.aliases) if len(name) > 1)]
    anchor_ids = [e.id for e in anchors]
    memory_link = or_(*[CtxMemory.entity_ids.op("@>")([str(eid)]) for eid in anchor_ids]) if anchor_ids else False

    if "memory" in kinds:
        pred, score = lexical(CtxMemory.content, query, words)
        score = (score + case((memory_link, 0.65), else_=0.0)).label("score")
        rows = await db.execute(select(CtxMemory, score).where(
            CtxMemory.archived_at.is_(None), scope_clause(CtxMemory.scope, scope), or_(pred, memory_link))
            .order_by(score.desc(), CtxMemory.updated_at.desc()).limit(pool))
        hits += [Hit("memory", str(m.id), float(sc), m.content,
                     {"kind": m.kind, "pinned": m.pinned, "source": m.source, "source_ref": m.source_ref,
                      "scope": m.scope, "updated_at": m.updated_at.isoformat()}) for m, sc in rows]

    if "chunk" in kinds:
        pred, score = lexical(CtxChunk.content, query, words)
        hp, hs = lexical(func.concat_ws(" ", CtxDocument.title, CtxChunk.heading), query, words)
        linked = CtxSource.entity_id.in_(anchor_ids)
        score = (score + 0.35 * hs + case((linked, 0.65), else_=0.0)).label("score")
        rows = await db.execute(select(CtxChunk, CtxDocument, CtxSource, CtxEntity.name, score)
            .join(CtxDocument, CtxDocument.id == CtxChunk.document_id)
            .join(CtxSource, CtxSource.id == CtxDocument.source_id)
            .outerjoin(CtxEntity, CtxEntity.id == CtxSource.entity_id)
            .where(CtxSource.enabled.is_(True), scope_clause(CtxSource.scope, scope), or_(pred, hp, linked))
            .order_by(score.desc(), CtxDocument.indexed_at.desc(), CtxChunk.ord).limit(pool))
        hits += [Hit("chunk", str(c.id), float(sc), c.content[:1200],
                     {"path": d.path, "title": d.title, "heading": c.heading, "scope": src.scope,
                      "category": src.category, "project": name, "indexed_at": d.indexed_at.isoformat(),
                      "source_id": str(src.id), "ord": c.ord}) for c, d, src, name, sc in rows]

    if "entity" in kinds:
        for e, sc in entities:
            d = await entity_detail(db, e, scope=scope)
            rels = d["relations"][:8]
            rel = "; ".join(f"{r['subject']} {r['predicate']} {r['object']}" for r in rels)
            text = e.name + (f": {e.summary}" if e.summary else "") + (f" | 관계: {rel}" if rel else "")
            hits.append(Hit("entity", str(e.id), float(sc) + (0.8 if e in anchors else 0), text,
                            {"entity_type": e.type, "name": e.name, "aliases": e.aliases, "scope": e.scope,
                             "attributes": e.attributes, "updated_at": e.updated_at.isoformat()}))

    if "meeting" in kinds:
        from ttakkari.meetings import vectors

        for h in await vectors.search(db, query, pool, scope=scope):
            out = vectors.hit_out(h)
            hits.append(Hit("meeting", str(h.chunk.id), h.score, out.pop("text"), {**out, "path": f"meeting:{h.meeting.id}"}))

    hits.sort(key=lambda h: h.score, reverse=True)
    # Prevent one long document from consuming the entire context budget.
    counts: dict[str, int] = {}
    selected = []
    for h in hits:
        path = h.meta.get("path")
        if path and counts.get(path, 0) >= 2:
            continue
        if path:
            counts[path] = counts.get(path, 0) + 1
        selected.append(h)
        if len(selected) == k:
            break
    return selected
