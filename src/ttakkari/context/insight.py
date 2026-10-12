"""맥락 도서관 화면(개요·구조·그래프·품질·검색 실험)용 읽기 전용 집계. 쓰기·삭제·색인은 하지 않는다."""
from __future__ import annotations

import difflib
import re
import time
import unicodedata
import uuid
from collections import Counter
from datetime import timedelta
from statistics import median
from typing import Any

from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context import store
from ttakkari.context.indexer import CHUNK_CHARS
from ttakkari.context.search import scope_clause, terms
from ttakkari.models import (
    CtxChunk,
    CtxDocument,
    CtxEntity,
    CtxMemory,
    CtxRelation,
    CtxSource,
    utcnow,
)

SHORT_CHUNK = 80
LONG_CHUNK = int(CHUNK_CHARS * 1.25)
STALE_AFTER = timedelta(hours=48)
# 길이 분포 막대의 경계. 마지막 칸은 색인기가 만드는 최대(CHUNK_CHARS * 1.5 안팎) 이상이다.
HIST_EDGES = (0, 80, 200, 400, 700, 1000, 1300, 1600)
DUP_NAME_RATIO = 0.86
PREVIEW = 160


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def norm_name(name: str) -> str:
    """이름 비교용 정규화: 대소문자·공백·하이픈·밑줄·점을 무시한다."""
    return re.sub(r"[\s\-_.·]+", "", unicodedata.normalize("NFKC", name).casefold())


def duplicate_suspects(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """이름이 거의 같거나 한쪽 별칭이 다른 쪽 이름과 같은 엔터티 쌍. 부분 포함(Skein ⊂ Skein Design System)은 서로 다른 것으로 본다."""
    keys = [{norm_name(x["name"]), *(norm_name(a) for a in x["aliases"] if isinstance(a, str))} - {""} for x in entities]
    out: list[dict[str, Any]] = []
    for i, a in enumerate(entities):
        for j in range(i + 1, len(entities)):
            b = entities[j]
            na, nb = norm_name(a["name"]), norm_name(b["name"])
            reason, score = None, 0.0
            if na and na == nb:
                reason, score = "이름이 같음(공백·대소문자 제외)", 1.0
            elif (keys[i] & keys[j]):
                reason, score = "별칭이 다른 항목의 이름·별칭과 같음", 0.95
            else:
                ratio = difflib.SequenceMatcher(None, na, nb).ratio() if min(len(na), len(nb)) >= 4 else 0.0
                if ratio >= DUP_NAME_RATIO:
                    reason, score = "이름이 비슷함", round(ratio, 2)
            if reason:
                out.append({"a": a["id"], "b": b["id"], "a_name": a["name"], "b_name": b["name"],
                            "reason": reason, "score": round(score, 2)})
    return sorted(out, key=lambda d: -d["score"])[:30]


# ---- 개요

async def overview(db: AsyncSession, scope: str) -> dict[str, Any]:
    sscope = scope_clause(CtxSource.scope, scope)
    rows = (await db.execute(
        select(CtxSource.category, CtxSource.scope, func.count(func.distinct(CtxSource.id)),
               func.count(CtxDocument.id), func.coalesce(func.sum(CtxDocument.chunk_count), 0))
        .outerjoin(CtxDocument, CtxDocument.source_id == CtxSource.id)
        .where(sscope, CtxSource.enabled.is_(True)).group_by(CtxSource.category, CtxSource.scope))).all()
    by_category = [{"category": c, "scope": s, "sources": n, "documents": d, "chunks": int(k)} for c, s, n, d, k in rows]

    sources = list(await db.scalars(select(CtxSource).where(sscope).order_by(CtxSource.created_at)))
    errors = [{"id": str(s.id), "path": s.path, "category": s.category, "scope": s.scope,
               "last_error": s.last_error, "last_indexed_at": _iso(s.last_indexed_at)}
              for s in sources if s.last_error]
    indexed = [s.last_indexed_at for s in sources if s.last_indexed_at]

    mems = (await db.execute(select(CtxMemory.scope, CtxMemory.kind, CtxMemory.source)
                             .where(CtxMemory.archived_at.is_(None), scope_clause(CtxMemory.scope, scope)))).all()
    ents = (await db.execute(select(CtxEntity.scope, CtxEntity.type).where(scope_clause(CtxEntity.scope, scope)))).all()
    rels = await db.scalar(
        select(func.count()).select_from(CtxRelation)
        .join(CtxEntity, CtxEntity.id == CtxRelation.subject_id).where(scope_clause(CtxEntity.scope, scope)))
    return {
        "scope": scope, "retrieval": "lexical",
        "totals": {"sources": len(sources), "enabled_sources": sum(1 for s in sources if s.enabled),
                   "documents": sum(r["documents"] for r in by_category),
                   "chunks": sum(r["chunks"] for r in by_category),
                   "memories": len(mems), "entities": len(ents), "relations": rels or 0},
        "by_category": by_category,
        "memories": {"by_scope": dict(Counter(m[0] for m in mems)), "by_kind": dict(Counter(m[1] for m in mems)),
                     "by_source": dict(Counter(m[2] for m in mems))},
        "entities": {"by_scope": dict(Counter(e[0] for e in ents)), "by_type": dict(Counter(e[1] for e in ents))},
        "last_indexed_at": _iso(max(indexed)) if indexed else None,
        "never_indexed": sum(1 for s in sources if s.enabled and s.last_indexed_at is None),
        "error_sources": errors,
    }


# ---- 구조

async def tree(db: AsyncSession, scope: str) -> dict[str, Any]:
    rows = (await db.execute(
        select(CtxSource, CtxEntity.id, CtxEntity.name, CtxEntity.type,
               func.count(CtxDocument.id), func.coalesce(func.sum(CtxDocument.chunk_count), 0))
        .outerjoin(CtxEntity, CtxEntity.id == CtxSource.entity_id)
        .outerjoin(CtxDocument, CtxDocument.source_id == CtxSource.id)
        .where(scope_clause(CtxSource.scope, scope))
        .group_by(CtxSource.id, CtxEntity.id, CtxEntity.name, CtxEntity.type)
        .order_by(CtxSource.category, CtxEntity.name, CtxSource.path))).all()
    cats: dict[str, dict[str | None, dict[str, Any]]] = {}
    for src, eid, ename, etype, docs, chunks in rows:
        group = cats.setdefault(src.category, {}).setdefault(
            str(eid) if eid else None,
            {"entity_id": str(eid) if eid else None, "name": ename or "연결된 항목 없음", "entity_type": etype,
             "sources": []})
        group["sources"].append({
            "id": str(src.id), "path": src.path, "pattern": src.pattern, "scope": src.scope, "enabled": src.enabled,
            "documents": docs, "chunks": int(chunks), "last_indexed_at": _iso(src.last_indexed_at),
            "last_error": src.last_error})
    out = []
    for category, groups in cats.items():
        gl = []
        for g in groups.values():
            g["documents"] = sum(s["documents"] for s in g["sources"])
            g["chunks"] = sum(s["chunks"] for s in g["sources"])
            g["scopes"] = sorted({s["scope"] for s in g["sources"]})
            gl.append(g)
        gl.sort(key=lambda g: -g["chunks"])
        out.append({"category": category, "groups": gl, "sources": sum(len(g["sources"]) for g in gl),
                    "documents": sum(g["documents"] for g in gl), "chunks": sum(g["chunks"] for g in gl)})
    out.sort(key=lambda c: -c["chunks"])
    return {"scope": scope, "categories": out}


async def source_documents(db: AsyncSession, source: CtxSource, limit: int = 500) -> dict[str, Any]:
    docs = list(await db.scalars(select(CtxDocument).where(CtxDocument.source_id == source.id)
                                 .order_by(CtxDocument.chunk_count.desc(), CtxDocument.path).limit(limit)))
    total = await db.scalar(select(func.count()).select_from(CtxDocument).where(CtxDocument.source_id == source.id))
    return {"source_id": str(source.id), "path": source.path, "total": total or 0,
            "documents": [{"id": str(d.id), "path": d.path, "title": d.title, "chunks": d.chunk_count,
                           "indexed_at": _iso(d.indexed_at)} for d in docs]}


async def document_chunks(db: AsyncSession, doc: CtxDocument, limit: int = 12) -> dict[str, Any]:
    chunks = list(await db.scalars(select(CtxChunk).where(CtxChunk.document_id == doc.id)
                                   .order_by(CtxChunk.ord).limit(limit)))
    total = await db.scalar(select(func.count()).select_from(CtxChunk).where(CtxChunk.document_id == doc.id))
    return {"document_id": str(doc.id), "path": doc.path, "title": doc.title, "total": total or 0,
            "chunks": [{"id": str(c.id), "ord": c.ord, "heading": c.heading, "length": len(c.content),
                        "content": c.content[:700]} for c in chunks]}


# ---- 온톨로지

async def _entity_evidence(db: AsyncSession, scope: str) -> tuple[list[CtxEntity], dict[str, int], dict[str, dict[str, int]]]:
    ents = list(await db.scalars(select(CtxEntity).where(scope_clause(CtxEntity.scope, scope))
                                 .order_by(CtxEntity.name).limit(500)))
    mem_count: Counter[str] = Counter()
    for (ids,) in (await db.execute(select(CtxMemory.entity_ids).where(
            CtxMemory.archived_at.is_(None), scope_clause(CtxMemory.scope, scope)))).all():
        mem_count.update(str(i) for i in ids)
    src: dict[str, dict[str, int]] = {}
    rows = (await db.execute(
        select(CtxSource.entity_id, func.count(func.distinct(CtxSource.id)), func.count(CtxDocument.id),
               func.coalesce(func.sum(CtxDocument.chunk_count), 0))
        .outerjoin(CtxDocument, CtxDocument.source_id == CtxSource.id)
        .where(CtxSource.entity_id.is_not(None), scope_clause(CtxSource.scope, scope))
        .group_by(CtxSource.entity_id))).all()
    for eid, n, d, c in rows:
        src[str(eid)] = {"sources": n, "documents": d, "chunks": int(c)}
    return ents, dict(mem_count), src


async def graph(db: AsyncSession, scope: str) -> dict[str, Any]:
    ents, mem_count, src = await _entity_evidence(db, scope)
    ids = {e.id for e in ents}
    rels = [r for r in await db.scalars(select(CtxRelation).limit(2000)) if r.subject_id in ids and r.object_id in ids]
    degree: Counter[str] = Counter()
    for r in rels:
        degree[str(r.subject_id)] += 1
        degree[str(r.object_id)] += 1
    plain = [{"id": str(e.id), "name": e.name, "aliases": e.aliases} for e in ents]
    dups = duplicate_suspects(plain)
    dup_ids = {d["a"] for d in dups} | {d["b"] for d in dups}
    nodes = []
    for e in ents:
        i = str(e.id)
        ev = src.get(i, {"sources": 0, "documents": 0, "chunks": 0})
        nodes.append({
            "id": i, "name": e.name, "type": e.type, "scope": e.scope, "aliases": e.aliases,
            "summary": (e.summary or "")[:240], "pinned": e.pinned, "degree": degree.get(i, 0),
            "memories": mem_count.get(i, 0), **ev,
            "flags": {"isolated": degree.get(i, 0) == 0, "no_alias": not e.aliases, "no_summary": not (e.summary or "").strip(),
                      "no_evidence": mem_count.get(i, 0) == 0 and ev["sources"] == 0, "duplicate_suspect": i in dup_ids}})
    return {
        "scope": scope, "nodes": nodes,
        "edges": [{"id": str(r.id), "source": str(r.subject_id), "target": str(r.object_id), "predicate": r.predicate,
                   "note": r.note, "origin": r.source} for r in rels],
        "duplicates": dups,
        "predicates": dict(Counter(r.predicate for r in rels).most_common()),
    }


async def entity_evidence(db: AsyncSession, e: CtxEntity, scope: str) -> dict[str, Any]:
    """노드를 눌렀을 때: 연결된 관계·기억 전문과 이 엔터티에 묶인 출처(문서 수)."""
    detail = await store.entity_detail(db, e, scope=scope)
    rows = (await db.execute(
        select(CtxSource, func.count(CtxDocument.id), func.coalesce(func.sum(CtxDocument.chunk_count), 0))
        .outerjoin(CtxDocument, CtxDocument.source_id == CtxSource.id)
        .where(CtxSource.entity_id == e.id, scope_clause(CtxSource.scope, scope)).group_by(CtxSource.id)
        .order_by(CtxSource.path).limit(60))).all()
    detail["sources"] = [{"id": str(s.id), "path": s.path, "category": s.category, "scope": s.scope,
                          "documents": d, "chunks": int(c)} for s, d, c in rows]
    return detail


# ---- 품질

def _bin_label(lo: int, hi: int | None) -> str:
    return f"{lo}+" if hi is None else f"{lo}–{hi - 1}"


def histogram(lengths: list[int]) -> list[dict[str, Any]]:
    bins = []
    for i, lo in enumerate(HIST_EDGES):
        hi = HIST_EDGES[i + 1] if i + 1 < len(HIST_EDGES) else None
        n = sum(1 for x in lengths if x >= lo and (hi is None or x < hi))
        bins.append({"label": _bin_label(lo, hi), "min": lo, "max": hi, "count": n})
    return bins


def _chunk_example(c: CtxChunk, d: CtxDocument, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": str(c.id), "document_id": str(d.id), "path": d.path, "title": d.title, "heading": c.heading,
            "length": len(c.content), "preview": c.content[:PREVIEW], **(extra or {})}


async def quality(db: AsyncSession, scope: str) -> dict[str, Any]:
    sscope = scope_clause(CtxSource.scope, scope)
    base = (select(CtxChunk, CtxDocument).join(CtxDocument, CtxDocument.id == CtxChunk.document_id)
            .join(CtxSource, CtxSource.id == CtxDocument.source_id).where(sscope, CtxSource.enabled.is_(True)))
    lens = [(cid, n) for cid, n in (await db.execute(
        select(CtxChunk.id, func.length(CtxChunk.content)).join(CtxDocument, CtxDocument.id == CtxChunk.document_id)
        .join(CtxSource, CtxSource.id == CtxDocument.source_id).where(sscope, CtxSource.enabled.is_(True)))).all()]
    lengths = sorted(n for _, n in lens)
    total = len(lengths)
    chunks: dict[str, Any] = {
        "total": total, "histogram": histogram(lengths), "short_limit": SHORT_CHUNK, "long_limit": LONG_CHUNK,
        "target": CHUNK_CHARS,
        "stats": ({"min": lengths[0], "median": int(median(lengths)), "p90": lengths[int(total * 0.9) - 1 if total > 9 else -1],
                   "max": lengths[-1], "mean": round(sum(lengths) / total)} if total else None),
        "short": sum(1 for x in lengths if x < SHORT_CHUNK), "long": sum(1 for x in lengths if x > LONG_CHUNK),
    }
    short_rows = (await db.execute(base.where(func.length(CtxChunk.content) < SHORT_CHUNK)
                                   .order_by(func.length(CtxChunk.content), CtxDocument.path).limit(8))).all()
    long_rows = (await db.execute(base.where(func.length(CtxChunk.content) > LONG_CHUNK)
                                  .order_by(func.length(CtxChunk.content).desc(), CtxDocument.path).limit(8))).all()
    chunks["short_examples"] = [_chunk_example(c, d) for c, d in short_rows]
    chunks["long_examples"] = [_chunk_example(c, d) for c, d in long_rows]
    chunks["no_heading"] = await db.scalar(
        select(func.count()).select_from(CtxChunk).join(CtxDocument, CtxDocument.id == CtxChunk.document_id)
        .join(CtxSource, CtxSource.id == CtxDocument.source_id)
        .where(sscope, CtxSource.enabled.is_(True), CtxChunk.heading.is_(None))) or 0

    digest = func.md5(CtxChunk.content)
    dup = (await db.execute(
        select(digest, func.count(CtxChunk.id), func.count(func.distinct(CtxChunk.document_id)), func.max(func.length(CtxChunk.content)),
               func.min(CtxChunk.id.cast(String)))
        .join(CtxDocument, CtxDocument.id == CtxChunk.document_id).join(CtxSource, CtxSource.id == CtxDocument.source_id)
        .where(sscope, CtxSource.enabled.is_(True)).group_by(digest).having(func.count(CtxChunk.id) > 1)
        .order_by(func.count(CtxChunk.id).desc()).limit(200))).all()
    dup_examples = []
    for _, n, ndocs, length, first_id in dup[:6]:
        c = await db.get(CtxChunk, uuid.UUID(first_id))
        d = await db.get(CtxDocument, c.document_id) if c else None
        if c and d:
            dup_examples.append(_chunk_example(c, d, {"copies": n, "documents": ndocs}))
    duplicates = {"groups": len(dup), "extra_chunks": sum(n - 1 for _, n, *_ in dup),
                  "examples": dup_examples, "truncated": len(dup) == 200}

    docs = (await db.execute(
        select(CtxDocument, func.count(CtxChunk.id)).join(CtxSource, CtxSource.id == CtxDocument.source_id)
        .outerjoin(CtxChunk, CtxChunk.document_id == CtxDocument.id).where(sscope, CtxSource.enabled.is_(True))
        .group_by(CtxDocument.id))).all()
    empty_docs = [d for d, n in docs if n == 0]
    mismatch = [d for d, n in docs if n != d.chunk_count]
    biggest = sorted(docs, key=lambda r: -r[1])[:5]
    documents = {
        "total": len(docs), "empty": len(empty_docs), "count_mismatch": len(mismatch),
        "empty_examples": [{"id": str(d.id), "path": d.path} for d in empty_docs[:6]],
        "chunks_per_doc": {"mean": round(sum(n for _, n in docs) / len(docs), 1) if docs else 0,
                           "max": max((n for _, n in docs), default=0)},
        "largest": [{"id": str(d.id), "path": d.path, "title": d.title, "chunks": n} for d, n in biggest if n > 0],
    }

    srcs = list(await db.scalars(select(CtxSource).where(sscope)))
    now = utcnow()
    doc_by_source = Counter(d.source_id for d, _ in docs)
    sources = {
        "total": len(srcs), "disabled": sum(1 for s in srcs if not s.enabled),
        "errors": sum(1 for s in srcs if s.last_error),
        "never_indexed": sum(1 for s in srcs if s.enabled and not s.last_indexed_at),
        "stale": [{"id": str(s.id), "path": s.path, "last_indexed_at": _iso(s.last_indexed_at)} for s in srcs
                  if s.enabled and s.last_indexed_at and now - s.last_indexed_at > STALE_AFTER][:8],
        "empty": [{"id": str(s.id), "path": s.path} for s in srcs if s.enabled and doc_by_source[s.id] == 0][:8],
        "unlinked": sum(1 for s in srcs if s.entity_id is None),
    }

    ents, mem_count, esrc = await _entity_evidence(db, scope)
    eids = {e.id for e in ents}
    rels = [r for r in await db.scalars(select(CtxRelation).limit(2000)) if r.subject_id in eids and r.object_id in eids]
    linked = {r.subject_id for r in rels} | {r.object_id for r in rels}
    plain = [{"id": str(e.id), "name": e.name, "aliases": e.aliases} for e in ents]
    dups = duplicate_suspects(plain)
    pred_norm: dict[str, set[str]] = {}
    for r in rels:
        pred_norm.setdefault(norm_name(r.predicate), set()).add(r.predicate)
    pair_keys = Counter((r.predicate, frozenset((r.subject_id, r.object_id))) for r in rels)
    entities = {
        "total": len(ents), "no_alias": sum(1 for e in ents if not e.aliases),
        "no_alias_names": [e.name for e in ents if not e.aliases][:12],
        "no_summary": sum(1 for e in ents if not (e.summary or "").strip()),
        "isolated": sum(1 for e in ents if e.id not in linked),
        "isolated_names": [e.name for e in ents if e.id not in linked][:12],
        "no_evidence": sum(1 for e in ents if not mem_count.get(str(e.id)) and str(e.id) not in esrc),
        "no_evidence_names": [e.name for e in ents if not mem_count.get(str(e.id)) and str(e.id) not in esrc][:12],
        "duplicate_pairs": dups,
        "by_type": dict(Counter(e.type for e in ents)),
    }
    relations = {
        "total": len(rels), "predicates": dict(Counter(r.predicate for r in rels).most_common()),
        "predicate_variants": [sorted(v) for v in pred_norm.values() if len(v) > 1],
        "bidirectional_same_predicate": sum(1 for (_, k), n in pair_keys.items() if len(k) == 2 and n > 1),
    }

    mems = list(await db.scalars(select(CtxMemory).where(CtxMemory.archived_at.is_(None), scope_clause(CtxMemory.scope, scope))))
    content_norm = Counter(norm_name(m.content) for m in mems)
    memories = {
        "total": len(mems), "unlinked": sum(1 for m in mems if not m.entity_ids),
        "no_source_ref": sum(1 for m in mems if not m.source_ref), "pinned": sum(1 for m in mems if m.pinned),
        "duplicate": sum(n - 1 for n in content_norm.values() if n > 1),
        "by_kind": dict(Counter(m.kind for m in mems)), "by_source": dict(Counter(m.source for m in mems)),
        "length": {"max": max((len(m.content) for m in mems), default=0),
                   "mean": round(sum(len(m.content) for m in mems) / len(mems)) if mems else 0},
    }
    out = {"scope": scope, "chunks": chunks, "duplicates": duplicates, "documents": documents, "sources": sources,
           "entities": entities, "relations": relations, "memories": memories}
    out["findings"] = findings(out)
    return out


def findings(q: dict[str, Any]) -> list[dict[str, Any]]:
    """지표를 읽는 법까지 붙인 점검 항목. level: ok(괜찮음) / info(참고) / warn(손볼 만함)."""
    out: list[dict[str, Any]] = []

    def add(level: str, code: str, title: str, detail: str, count: int = 0) -> None:
        out.append({"level": level, "code": code, "title": title, "detail": detail, "count": count})

    c, ents, rel, mem, src, doc = q["chunks"], q["entities"], q["relations"], q["memories"], q["sources"], q["documents"]
    if c["total"]:
        short_pct = c["short"] / c["total"] * 100
        long_pct = c["long"] / c["total"] * 100
        add("warn" if short_pct >= 10 else "ok" if short_pct < 3 else "info", "short_chunks",
            f"너무 짧은 조각 {c['short']}개 ({short_pct:.1f}%)",
            f"{SHORT_CHUNK}자 미만 조각은 문맥이 부족해 검색에 걸려도 쓸모가 적어요. 10% 이상이면 자르는 규칙을 손봐야 해요.", c["short"])
        add("warn" if long_pct >= 10 else "ok" if long_pct < 3 else "info", "long_chunks",
            f"너무 긴 조각 {c['long']}개 ({long_pct:.1f}%)",
            f"{LONG_CHUNK}자를 넘는 조각은 한 조각에 여러 주제가 섞여 점수가 흐려져요.", c["long"])
        add("warn" if c["no_heading"] / c["total"] > 0.2 else "ok", "no_heading", f"제목 없는 조각 {c['no_heading']}개",
            "제목(절) 경로가 없으면 결과에서 위치를 알기 어렵고 제목 일치 가산점도 못 받아요.", c["no_heading"])
    if q["duplicates"]["extra_chunks"]:
        extra = q["duplicates"]["extra_chunks"]
        add("warn" if c["total"] and extra / c["total"] >= 0.05 else "info", "duplicate_chunks",
            f"내용이 같은 조각 {extra}개 중복", "같은 문장이 여러 문서에 있으면 결과 자리를 겹쳐 차지해요.", extra)
    else:
        add("ok", "duplicate_chunks", "내용이 같은 조각 없음", "", 0)
    if doc["empty"] or doc["count_mismatch"]:
        add("warn", "documents_integrity", f"조각이 없는 문서 {doc['empty']} · 조각 수 불일치 {doc['count_mismatch']}",
            "문서에 기록된 조각 수와 실제 조각 수가 다르면 색인이 중간에 끊겼을 수 있어요.", doc["empty"] + doc["count_mismatch"])
    if src["errors"]:
        add("warn", "source_errors", f"색인 오류가 있는 출처 {src['errors']}개", "last_error 가 남은 출처예요. 개별 파일 읽기 실패는 이전 조각을 유지해요.", src["errors"])
    if src["stale"]:
        add("info", "stale_sources", f"48시간 넘게 갱신 안 된 출처 {len(src['stale'])}개", "매시간 재색인이 도는지 확인해요.", len(src["stale"]))
    if src["unlinked"]:
        add("info", "unlinked_sources", f"항목에 연결되지 않은 출처 {src['unlinked']}개",
            "엔터티에 묶이지 않은 출처는 별칭으로 문서를 찾는 경로를 못 타요.", src["unlinked"])
    if ents["total"]:
        add("warn" if ents["no_alias"] / ents["total"] > 0.4 else "info" if ents["no_alias"] else "ok", "no_alias",
            f"별칭 없는 엔터티 {ents['no_alias']}개", "검색은 이름·별칭 부분 일치가 전부라 별칭이 곧 동의어 사전이에요.", ents["no_alias"])
        add("warn" if ents["isolated"] / ents["total"] > 0.5 else "info" if ents["isolated"] else "ok", "isolated",
            f"관계 없는 엔터티 {ents['isolated']}개", "관계가 없으면 1단계 관계 확장이 일어나지 않아 그래프 의미가 없어요.", ents["isolated"])
        add("warn" if ents["no_evidence"] else "ok", "no_evidence", f"기억도 출처도 없는 엔터티 {ents['no_evidence']}개",
            "연결된 근거가 없는 엔터티는 recall 에서 이름만 나와요.", ents["no_evidence"])
    if ents["duplicate_pairs"]:
        add("warn", "duplicate_entities", f"중복 의심 엔터티 {len(ents['duplicate_pairs'])}쌍", "이름·별칭이 겹쳐 같은 대상을 둘로 나눴을 수 있어요.", len(ents["duplicate_pairs"]))
    if rel["predicate_variants"]:
        add("warn", "predicate_variants", f"표기만 다른 관계 이름 {len(rel['predicate_variants'])}묶음", "같은 뜻의 술어는 하나로 통일해요.", len(rel["predicate_variants"]))
    if mem["total"]:
        add("info" if mem["unlinked"] else "ok", "unlinked_memories", f"엔터티에 안 묶인 기억 {mem['unlinked']}개",
            "엔터티에 묶인 기억만 별칭으로 연결돼 나와요.", mem["unlinked"])
        if mem["no_source_ref"]:
            add("info", "memory_no_ref", f"출처(source_ref) 없는 기억 {mem['no_source_ref']}개", "근거 경로가 없으면 오래된 결정인지 확인하기 어려워요.", mem["no_source_ref"])
        if mem["duplicate"]:
            add("warn", "duplicate_memories", f"같은 내용의 기억 {mem['duplicate']}개", "", mem["duplicate"])
    return out


# ---- 검색 실험실

def matched_terms(words: list[str], *fields: str | None) -> list[str]:
    hay = [f.casefold() for f in fields if f]
    return [w for w in words if any(w in h for h in hay)]


async def search_lab(db: AsyncSession, query: str, k: int, scope: str) -> dict[str, Any]:
    query = query.strip()
    words = terms(query)
    started = time.perf_counter()
    hits = await store.recall(db, query, k, scope=scope)
    elapsed = round((time.perf_counter() - started) * 1000)
    ents = list(await db.scalars(select(CtxEntity).where(scope_clause(CtxEntity.scope, scope))))
    folded = unicodedata.normalize("NFKC", query).casefold()
    anchors = [e for e in ents if any(len(n) > 1 and n.casefold() in folded for n in (e.name, *e.aliases) if isinstance(n, str))]
    anchor_names = {e.name for e in anchors}
    anchor_ids = {str(e.id) for e in anchors}
    mem_ids = [uuid.UUID(h.id) for h in hits if h.kind == "memory"]
    mem_entities: dict[str, list[str]] = {}
    if mem_ids:
        by_id = {str(e.id): e.name for e in ents}
        for m in await db.scalars(select(CtxMemory).where(CtxMemory.id.in_(mem_ids))):
            mem_entities[str(m.id)] = [by_id.get(i, i) for i in m.entity_ids]
            mem_entities[f"{m.id}:ids"] = [str(i) for i in m.entity_ids]
    results = []
    for rank, h in enumerate(hits, 1):
        m = h.meta
        if h.kind == "chunk":
            fields = {"본문": h.text, "제목": m.get("title"), "절": m.get("heading")}
            via_entity = m.get("project") in anchor_names
        elif h.kind == "memory":
            fields = {"본문": h.text}
            via_entity = bool(anchor_ids & set(mem_entities.get(f"{h.id}:ids", [])))
        else:
            fields = {"이름": m.get("name"), "별칭": " ".join(m.get("aliases") or []), "요약": h.text}
            via_entity = m.get("name") in anchor_names
        found = {name: matched_terms(words, val) for name, val in fields.items()}
        flat = [w for ws in found.values() for w in ws]
        matched = [w for w in words if w in flat]
        via = "keyword" if matched else "entity" if via_entity else "fuzzy"
        results.append({
            "rank": rank, "type": h.kind, "id": h.id, "score": round(h.score, 3), "text": h.text,
            "matched_terms": matched, "match_in": [name for name, ws in found.items() if ws], "via": via,
            "coverage": round(len(matched) / len(words), 2) if words else 0.0,
            "entities": mem_entities.get(h.id) if h.kind == "memory" else None, **m})
    return {"query": query, "scope": scope, "terms": words, "anchors": sorted(anchor_names), "elapsed_ms": elapsed,
            "results": results}
