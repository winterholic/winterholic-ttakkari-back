"""Explicit, repeatable imports of sourced project context; never infer facts from repository names."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context import store
from ttakkari.context.indexer import index_source
from ttakkari.context.search import scope_clause
from ttakkari.models import CtxEntity, CtxMemory, CtxSource, utcnow

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


async def import_catalog(db: AsyncSession, data: dict[str, Any]) -> dict[str, int]:
    from ttakkari.api.assistant import _check_source

    catalog = Catalog.model_validate(data)
    # Validate every path before the first write; a bad import must not leave half a catalog.
    paths = {s.path: str(_check_source(s.path)) for p in catalog.projects for s in p.sources}
    stats = {"entities": 0, "memories": 0, "sources": 0, "relations": 0, "chunks": 0}
    sources = []
    for project in catalog.projects:
        e = await store.upsert_entity(db, project.name, project.type, summary=project.summary,
                                      aliases=project.aliases, attributes=project.attributes, scope=project.scope)
        stats["entities"] += 1
        for fact in project.facts:
            prefix = f"catalog:{project.name}:{fact.key} | "
            ref = prefix + fact.source_ref
            m = await db.scalar(select(CtxMemory).where(CtxMemory.source == "ingest",
                                                       CtxMemory.source_ref.startswith(prefix, autoescape=True)))
            if m is None:
                await store.remember(db, fact.content, kind=fact.kind, entities=[e.name], source="ingest",
                                     source_ref=ref, scope=project.scope)
            elif m.archived_at is None and (m.content, m.kind, m.source_ref) != (fact.content, fact.kind, ref):
                m.content, m.kind, m.source_ref, m.updated_at = fact.content, fact.kind, ref, utcnow()
            stats["memories"] += 1
        for source in project.sources:
            path = paths[source.path]
            src = await db.scalar(select(CtxSource).where(CtxSource.path == path))
            if src is None:
                src = CtxSource(path=path, pattern=source.pattern, scope=project.scope,
                                category=source.category, entity_id=e.id)
                db.add(src)
            elif src.scope != project.scope or (src.entity_id is not None and src.entity_id != e.id):
                raise store.ContextError(f"다른 프로젝트·범위의 출처를 덮어쓸 수 없어요: {path}")
            else:
                src.entity_id, src.category = e.id, source.category
            sources.append(src)
        stats["sources"] += len(project.sources)
    for rel in catalog.relations:
        subject = await store.find_entity(db, rel.subject)
        obj = await store.find_entity(db, rel.object)
        if subject is None or obj is None:
            raise store.ContextError("관계 양쪽 항목을 먼저 등록해 주세요.")
        await store.link(db, subject.name, rel.predicate, obj.name, note=rel.note, source="ingest")
        stats["relations"] += 1
    await db.commit()
    for source in sources:
        result = await index_source(db, source)
        stats["chunks"] += result["chunks"]
        if result["errors"]:
            raise store.ContextError(source.last_error or "색인 일부 실패")
    return stats


async def library_catalog(db: AsyncSession, *, scope: str = "all") -> dict[str, Any]:
    projects = await db.scalars(select(CtxEntity).where(CtxEntity.type == "project", scope_clause(CtxEntity.scope, scope))
                               .order_by(CtxEntity.name).limit(100))
    categories = await db.execute(select(CtxSource.category, func.count(CtxSource.id))
                                 .where(CtxSource.enabled.is_(True), scope_clause(CtxSource.scope, scope))
                                 .group_by(CtxSource.category))
    return {"retrieval": "lexical", "categories": dict(categories.all()),
            "projects": [{"name": p.name, "aliases": p.aliases, "summary": (p.summary or "")[:240], "scope": p.scope,
                          "attributes": {k: v for k, v in p.attributes.items()
                                         if k in {"root_path", "checked_at", "remote_freshness"}}} for p in projects]}
