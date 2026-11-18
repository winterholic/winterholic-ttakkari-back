"""Explicit, repeatable imports of sourced project context; never infer facts from repository names."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context import store
from ttakkari.context.indexer import index_source
from ttakkari.context.search import scope_clause
from ttakkari.models import CtxEntity, CtxMemory, CtxRelation, CtxSource, utcnow

Scope = Literal["personal", "work", "shared"]
Category = Literal["projects", "work-history", "knowledge", "health"]


class Source(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    pattern: str = Field(default="**/*.md", max_length=100)
    category: Category = "projects"


class Fact(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(min_length=1, max_length=4096)
    kind: Literal["fact", "preference", "decision", "event", "instruction", "note"] = "fact"


class Project(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: Literal["project", "tool", "org", "topic"] = "project"
    aliases: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(min_length=1, max_length=2000)
    scope: Scope = "personal"
    attributes: dict[str, Any] = Field(default_factory=dict)
    sources: list[Source] = Field(default_factory=list, max_length=100)
    facts: list[Fact] = Field(default_factory=list, max_length=100)


class Relation(BaseModel):
    subject: str
    predicate: str = Field(min_length=1, max_length=60)
    object: str
    note: str | None = Field(default=None, max_length=1000)


class Catalog(BaseModel):
    projects: list[Project] = Field(max_length=100)
    relations: list[Relation] = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def unique_keys(self):
        names = [p.name.casefold() for p in self.projects]
        if len(names) != len(set(names)):
            raise ValueError("중복 프로젝트 이름")
        for p in self.projects:
            keys = [f.key for f in p.facts]
            if len(keys) != len(set(keys)):
                raise ValueError(f"{p.name}: 중복 기억 키")
        return self


CATALOG_MAX_BYTES = 2 * 1024 * 1024
LIST_LIMIT = 50


def load_catalog_file(path: Path) -> tuple[dict[str, Any], str]:
    """검증된 목록 파일을 읽는다. 민감 경로·.json 아님·2MB 초과는 거부한다. (내용, sha256)을 돌려준다."""
    from ttakkari.broker.policy import is_sensitive

    path = Path(path).expanduser().resolve()
    if is_sensitive(path) or path.suffix.lower() != ".json" or not path.is_file() or path.stat().st_size > CATALOG_MAX_BYTES:
        raise ValueError("2MB 이하의 일반 JSON 목록 파일을 지정해 주세요.")
    raw = path.read_bytes()
    if len(raw) > CATALOG_MAX_BYTES:
        raise ValueError("2MB 이하의 일반 JSON 목록 파일을 지정해 주세요.")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("목록 파일의 최상위는 객체여야 해요.")
    return data, hashlib.sha256(raw).hexdigest()


@dataclass
class CatalogPlan:
    """쓰기 결과의 새로움·변경 집계. 이름과 개수만 담고 본문은 담지 않는다."""
    stats: dict[str, int]
    counts: dict[str, dict[str, int]]
    new_projects: list[str]
    changed_projects: list[str]
    sources: list[CtxSource]
    to_index: list[uuid.UUID]


async def _write_catalog(db: AsyncSession, catalog: Catalog, paths: dict[str, str]) -> CatalogPlan:
    stats = {"entities": 0, "memories": 0, "sources": 0, "relations": 0, "chunks": 0}
    counts = {k: {"new": 0, "changed": 0, "unchanged": 0} for k in ("projects", "facts", "sources")}
    counts["relations"] = {"new": 0, "existing": 0}
    counts["projects"]["scope_differs"] = 0
    new_projects: list[str] = []
    changed_projects: list[str] = []
    sources: list[CtxSource] = []
    new_source_ids: set[uuid.UUID] = set()
    for project in catalog.projects:
        before = await store.find_entity(db, project.name)
        snapshot = None if before is None else (before.type, before.summary, list(before.aliases), dict(before.attributes))
        e = await store.upsert_entity(db, project.name, project.type, summary=project.summary,
                                      aliases=project.aliases, attributes=project.attributes, scope=project.scope)
        if snapshot is None:
            counts["projects"]["new"] += 1
            new_projects.append(project.name)
        elif snapshot != (e.type, e.summary, list(e.aliases), dict(e.attributes)):
            counts["projects"]["changed"] += 1
            changed_projects.append(project.name)
        else:
            counts["projects"]["unchanged"] += 1
        # 기존 항목의 범위는 upsert 가 유지한다. 목록과 다르면 숨기지 않고 센다.
        if snapshot is not None and e.scope != project.scope:
            counts["projects"]["scope_differs"] += 1
        stats["entities"] += 1
        for fact in project.facts:
            prefix = f"catalog:{project.name}:{fact.key} | "
            ref = prefix + fact.source_ref
            m = await db.scalar(select(CtxMemory).where(CtxMemory.source == "ingest",
                                                       CtxMemory.source_ref.startswith(prefix, autoescape=True)))
            if m is None:
                await store.remember(db, fact.content, kind=fact.kind, entities=[e.name], source="ingest",
                                     source_ref=ref, scope=project.scope)
                counts["facts"]["new"] += 1
            elif m.archived_at is None and (m.content, m.kind, m.source_ref) != (fact.content, fact.kind, ref):
                m.content, m.kind, m.source_ref, m.updated_at = fact.content, fact.kind, ref, utcnow()
                counts["facts"]["changed"] += 1
            else:
                counts["facts"]["unchanged"] += 1
            stats["memories"] += 1
        for source in project.sources:
            path = paths[source.path]
            src = await db.scalar(select(CtxSource).where(CtxSource.path == path))
            if src is None:
                src = CtxSource(path=path, pattern=source.pattern, scope=project.scope,
                                category=source.category, entity_id=e.id)
                db.add(src)
                await db.flush()
                new_source_ids.add(src.id)
                counts["sources"]["new"] += 1
            elif src.scope != project.scope or (src.entity_id is not None and src.entity_id != e.id):
                raise store.ContextError(f"다른 프로젝트·범위의 출처를 덮어쓸 수 없어요: {path}")
            else:
                if (src.entity_id, src.category) != (e.id, source.category):
                    counts["sources"]["changed"] += 1
                else:
                    counts["sources"]["unchanged"] += 1
                src.entity_id, src.category = e.id, source.category
            sources.append(src)
        stats["sources"] += len(project.sources)
    for rel in catalog.relations:
        subject = await store.find_entity(db, rel.subject)
        obj = await store.find_entity(db, rel.object)
        if subject is None or obj is None:
            raise store.ContextError("관계 양쪽 항목을 먼저 등록해 주세요.")
        predicate = rel.predicate.strip().lower().replace(" ", "_")[:60]
        known = await db.scalar(select(CtxRelation.id).where(
            CtxRelation.subject_id == subject.id, CtxRelation.predicate == predicate, CtxRelation.object_id == obj.id))
        await store.link(db, subject.name, rel.predicate, obj.name, note=rel.note, source="ingest")
        counts["relations"]["existing" if known else "new"] += 1
        stats["relations"] += 1
    await db.flush()
    # 새 출처이거나 아직 한 번도 색인되지 않은 출처만 색인을 요청한다(앞선 apply 의 색인 요청이 실패했어도 재실행이 메운다).
    to_index = list(dict.fromkeys(s.id for s in sources if s.id in new_source_ids or s.last_indexed_at is None))
    return CatalogPlan(stats, counts, new_projects, changed_projects, sources, to_index)


def _validated(data: dict[str, Any]) -> tuple[Catalog, dict[str, str]]:
    from ttakkari.api.assistant import _check_source

    catalog = Catalog.model_validate(data)
    # Validate every path before the first write; a bad import must not leave half a catalog.
    return catalog, {s.path: str(_check_source(s.path)) for p in catalog.projects for s in p.sources}


async def import_catalog(db: AsyncSession, data: dict[str, Any]) -> dict[str, int]:
    catalog, paths = _validated(data)
    plan = await _write_catalog(db, catalog, paths)
    stats = plan.stats
    await db.commit()
    for source in plan.sources:
        result = await index_source(db, source)
        stats["chunks"] += result["chunks"]
        if result["errors"]:
            raise store.ContextError(source.last_error or "색인 일부 실패")
    return stats


async def import_catalog_file(db: AsyncSession, *, apply: bool) -> dict[str, Any]:
    """설정에 고정된 목록 파일 하나를 가져온다. 경로는 인자로 받지 않는다.

    preview 는 같은 쓰기를 실행하고 롤백해 바뀔 개수만 보고한다. apply 는 커밋한 뒤 색인이 필요한 출처의 색인을 요청한다.
    결과에는 개수·프로젝트 이름·해시만 담는다."""
    from ttakkari.api.assistant import _start_index
    from ttakkari.config import get_settings

    path = Path(get_settings().context_catalog_file)
    data, digest = load_catalog_file(path)
    catalog, paths = _validated(data)
    try:
        plan = await _write_catalog(db, catalog, paths)
        if not apply:
            await db.rollback()
        else:
            await db.commit()
    except BaseException:
        await db.rollback()
        raise
    report: dict[str, Any] = {
        "applied": apply,
        "catalog": {"name": path.name, "sha256": digest, "projects": len(catalog.projects),
                    "relations": len(catalog.relations)},
        "projects": {**plan.counts["projects"], "new_names": plan.new_projects[:LIST_LIMIT],
                     "changed_names": plan.changed_projects[:LIST_LIMIT]},
        "facts": plan.counts["facts"], "sources": plan.counts["sources"], "relations": plan.counts["relations"],
        # 기존 항목의 값이 목록 내용으로 바뀌는 개수. import 는 목록이 기준이라 덮어쓴다.
        "overwritten": {"projects": plan.counts["projects"]["changed"], "facts": plan.counts["facts"]["changed"],
                        "source_links": plan.counts["sources"]["changed"]},
    }
    if not apply:
        report["index"] = {"would_request": len(plan.to_index)}
        return report
    failed = 0
    for source_id in plan.to_index:
        try:
            await _start_index(source_id)
        except Exception:  # noqa: BLE001 -- 커밋은 끝났다. 실패 수만 알리고 재실행이 남은 출처를 메운다
            failed += 1
    report["index"] = {"requested": len(plan.to_index) - failed, "failed": failed}
    return report


async def library_catalog(db: AsyncSession, *, scope: str = "all") -> dict[str, Any]:
    projects = await db.scalars(select(CtxEntity).where(CtxEntity.type == "project", scope_clause(CtxEntity.scope, scope))
                               .order_by(CtxEntity.name).limit(100))
    categories = await db.execute(select(CtxSource.category, func.count(CtxSource.id))
                                 .where(CtxSource.enabled.is_(True), scope_clause(CtxSource.scope, scope))
                                 .group_by(CtxSource.category))
    # 프로젝트가 아닌 지식·작업 기록 모음의 2단 색인. 건강처럼 출처가 없는 종류는 나오지 않는다.
    rows = await db.execute(
        select(CtxEntity, CtxSource.category, func.count(CtxSource.id))
        .join(CtxSource, CtxSource.entity_id == CtxEntity.id)
        .where(CtxSource.enabled.is_(True), CtxSource.category != "projects", scope_clause(CtxSource.scope, scope),
               scope_clause(CtxEntity.scope, scope))
        .group_by(CtxEntity.id, CtxSource.category).order_by(CtxSource.category, CtxEntity.name).limit(100))
    collections = [{"name": e.name, "aliases": e.aliases, "summary": (e.summary or "")[:240], "scope": e.scope,
                    "category": category, "sources": n,
                    "attributes": {k: v for k, v in e.attributes.items()
                                   if k in {"root_path", "kind", "skill_count", "checked_at"}}}
                   for e, category, n in rows.all()]
    return {"retrieval": "lexical", "categories": dict(categories.all()), "collections": collections,
            "projects": [{"name": p.name, "aliases": p.aliases, "summary": (p.summary or "")[:240], "scope": p.scope,
                          "attributes": {k: v for k, v in p.attributes.items()
                                         if k in {"root_path", "checked_at", "remote_freshness"}}} for p in projects]}
