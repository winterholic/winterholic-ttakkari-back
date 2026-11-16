"""맥락 출처 어댑터. 원본·도서관 접근은 Private Publisher 안의 이 계층만 한다.

LibraryAdapter 는 CtxEntity(type=project)에 연결된 CtxSource 의 색인 문서(CtxDocument/CtxChunk) 중
호출자가 id 로 명시한 것만 읽는다. 전체 검색·메모리·대화·임의 경로 읽기는 이 인터페이스에 없다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context_sharing.errors import ShareError
from ttakkari.models import CtxChunk, CtxDocument, CtxEntity, CtxSource

LISTING_LIMIT = 500


@dataclass(frozen=True)
class ProjectInfo:
    entity_id: uuid.UUID
    name: str
    aliases: list[str]
    summary: str
    scope: str
    source_count: int
    document_count: int


@dataclass(frozen=True)
class DocumentRef:
    document_id: uuid.UUID
    title: str
    logical_path: str
    category: str
    source_label: str
    source_scope: str
    chunk_count: int
    chars: int
    source_revision: str
    indexed_at: datetime


@dataclass(frozen=True)
class SourceDocument:
    ref: DocumentRef
    content: str

    @property
    def source_ref(self) -> str:
        return f"library:{self.ref.category}/{self.ref.logical_path}"


@dataclass(frozen=True)
class SourceGroup:
    source_id: uuid.UUID
    label: str
    category: str
    scope: str
    enabled: bool
    documents: list[DocumentRef]
    truncated: bool


class ContextSourceAdapter(Protocol):
    name: str

    async def list_projects(self) -> list[ProjectInfo]: ...
    async def get_project(self, entity_id: uuid.UUID) -> ProjectInfo | None: ...
    async def list_documents(self, entity_id: uuid.UUID) -> list[SourceGroup]: ...
    async def read_documents(self, entity_id: uuid.UUID, document_ids: list[uuid.UUID]) -> list[SourceDocument]: ...


def logical_path(source_path: str, document_path: str) -> str:
    """색인 루트 기준 상대 경로. 절대 경로·상위 이동·홈 경로는 외부에 내보내지 않는다."""
    root, doc = PurePosixPath(source_path), PurePosixPath(document_path)
    try:
        rel = doc.relative_to(root)
        parts = rel.parts
    except ValueError:
        parts = ()
    if not parts:  # 파일 하나를 출처로 쓴 경우 또는 루트 밖
        parts = (doc.name,)
    clean = [p for p in parts if p not in {"", ".", ".."}]
    return "/".join(clean) or "document"


def source_label(source_path: str) -> str:
    return PurePosixPath(source_path).name or "source"


def assemble(chunks: list[tuple[str | None, str]]) -> str:
    """색인 조각을 이어 붙인다. 조각 경계의 제목은 복원 가능한 만큼만 둔다(원본과 같다고 주장하지 않는다)."""
    out: list[str] = []
    last: str | None = None
    for heading, content in chunks:
        if heading and heading != last:
            out.append(f"## {heading}")
        last = heading or last
        out.append(content.strip())
    return "\n\n".join(p for p in out if p) + "\n"


class LibraryAdapter:
    name = "library"

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _project(self, entity_id: uuid.UUID) -> CtxEntity | None:
        e = await self.db.get(CtxEntity, entity_id)
        return e if e is not None and e.type == "project" else None

    async def list_projects(self) -> list[ProjectInfo]:
        entities = (await self.db.scalars(select(CtxEntity).where(CtxEntity.type == "project")
                                          .order_by(CtxEntity.name).limit(200))).all()
        if not entities:
            return []
        ids = [e.id for e in entities]
        sources = dict((await self.db.execute(
            select(CtxSource.entity_id, func.count(CtxSource.id))
            .where(CtxSource.entity_id.in_(ids), CtxSource.enabled.is_(True)).group_by(CtxSource.entity_id))).all())
        docs = dict((await self.db.execute(
            select(CtxSource.entity_id, func.count(CtxDocument.id))
            .join(CtxDocument, CtxDocument.source_id == CtxSource.id)
            .where(CtxSource.entity_id.in_(ids), CtxSource.enabled.is_(True)).group_by(CtxSource.entity_id))).all())
        return [ProjectInfo(e.id, e.name, [str(a) for a in e.aliases], e.summary or "", e.scope,
                            sources.get(e.id, 0), docs.get(e.id, 0)) for e in entities]

    async def get_project(self, entity_id: uuid.UUID) -> ProjectInfo | None:
        e = await self._project(entity_id)
        if e is None:
            return None
        return next((p for p in await self.list_projects() if p.entity_id == e.id), None)

    async def list_documents(self, entity_id: uuid.UUID) -> list[SourceGroup]:
        if await self._project(entity_id) is None:
            raise ShareError(404, "project_not_found", "라이브러리에 그런 프로젝트가 없습니다.")
        sources = (await self.db.scalars(select(CtxSource).where(CtxSource.entity_id == entity_id)
                                         .order_by(CtxSource.path))).all()
        groups = []
        for src in sources:
            refs: list[DocumentRef] = []
            truncated = False
            if src.enabled:
                rows = (await self.db.execute(
                    select(CtxDocument, func.coalesce(func.sum(func.length(CtxChunk.content)), 0))
                    .outerjoin(CtxChunk, CtxChunk.document_id == CtxDocument.id)
                    .where(CtxDocument.source_id == src.id).group_by(CtxDocument.id)
                    .order_by(CtxDocument.path).limit(LISTING_LIMIT + 1))).all()
                truncated = len(rows) > LISTING_LIMIT
                refs = [self._ref(src, d, int(chars)) for d, chars in rows[:LISTING_LIMIT]]
            groups.append(SourceGroup(src.id, source_label(src.path), src.category, src.scope, src.enabled, refs, truncated))
        return groups

    @staticmethod
    def _ref(src: CtxSource, doc: CtxDocument, chars: int) -> DocumentRef:
        return DocumentRef(doc.id, doc.title, logical_path(src.path, doc.path), src.category, source_label(src.path),
                           src.scope, doc.chunk_count, chars, doc.sha256, doc.indexed_at)

    async def read_documents(self, entity_id: uuid.UUID, document_ids: list[uuid.UUID]) -> list[SourceDocument]:
        if await self._project(entity_id) is None:
            raise ShareError(404, "project_not_found", "라이브러리에 그런 프로젝트가 없습니다.")
        wanted = list(dict.fromkeys(document_ids))
        rows = (await self.db.execute(
            select(CtxDocument, CtxSource).join(CtxSource, CtxSource.id == CtxDocument.source_id)
            .where(CtxDocument.id.in_(wanted), CtxSource.entity_id == entity_id, CtxSource.enabled.is_(True)))).all()
        found = {d.id: (d, s) for d, s in rows}
        missing = [str(i) for i in wanted if i not in found]
        if missing:
            # 다른 프로젝트·꺼진 출처·없는 문서를 구분해 알려 주지 않는다(존재 여부를 흘리지 않는다).
            raise ShareError(422, "document_not_in_project", "선택한 문서가 이 프로젝트의 사용 중인 출처에 없습니다.",
                             extra={"document_ids": missing})
        chunks: dict[uuid.UUID, list[tuple[str | None, str]]] = {i: [] for i in wanted}
        for doc_id, heading, content in (await self.db.execute(
                select(CtxChunk.document_id, CtxChunk.heading, CtxChunk.content)
                .where(CtxChunk.document_id.in_(wanted)).order_by(CtxChunk.document_id, CtxChunk.ord))).all():
            chunks[doc_id].append((heading, content))
        out = []
        for doc_id in wanted:
            doc, src = found[doc_id]
            body = assemble(chunks[doc_id])
            if not chunks[doc_id]:
                raise ShareError(422, "document_empty", "색인된 본문이 없는 문서는 선택할 수 없습니다.",
                                 extra={"document_ids": [str(doc_id)]})
            out.append(SourceDocument(self._ref(src, doc, len(body)), body))
        return out
