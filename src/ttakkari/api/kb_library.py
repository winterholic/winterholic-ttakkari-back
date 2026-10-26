"""Owner KB API: 하나의 통합 지식 도서관(KB v3, KB3-402).

개인/회사 선택 파라미터 없이 하나의 공간으로 응답한다. 접근은 kb_access 단일 규칙이다: 사용자 웹 세션은 개인 세션 기준
(global + 자기 소유 personal 출처)이고 work_only 는 보이지 않는다. 접근 행이 없는 문서는 work_only 로 격리된다
(미상 scope 는 절대 personal 에 도달하지 않는다). 기존 `scope` 쿼리는 deprecated 로 받기만 한다
(생략·personal = 통합 공간, work = 기존 회사 범위 조회를 명시적으로 고르는 레거시 경로). 기존 응답 필드는 보존하고 v3 필드는 추가만 한다.
Model rankings are evidence candidates; KB7 quality approval is separate.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from ttakkari import kb_ingest, kb_jobs, kb_v3_ontology_views, kb_v3_views
from ttakkari import kb_library as lib
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_access import KbAccessContext, from_web_request
from ttakkari.kb_pipeline import PipelineRetryError
from ttakkari.kb_pipeline_models import KbPipelineJob
from ttakkari.kb_store import KbInputError
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant/kb", tags=["kb-library"])

LegacyScope = Annotated[
    Literal["personal", "work"] | None,
    Query(deprecated=True, description="deprecated: 생략하면 통합 도서관. work 는 기존 회사 범위 조회를 명시적으로 고르는 레거시 경로."),
]


def web_access(scope: str | None) -> KbAccessContext:
    return from_web_request(scope)



class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["personal", "work"] = "personal"
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=lib.MAX_TEXT_CHARS)
    source_key: str | None = Field(default=None, min_length=1, max_length=500)
    lang: str | None = Field(default=None, max_length=16)
    locator: str | None = Field(default=None, max_length=4000)

    @field_validator("title", "text", "source_key", "lang", "locator")
    @classmethod
    def valid_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if "\x00" in value:
            raise ValueError("unsupported_nul")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid_unicode") from exc
        return value

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank_title")
        return value.strip()


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_stage: Literal["parsing", "chunking", "extracting", "linking", "indexing", "canonicalizing"] | None = None


class AliasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=kb_v3_ontology_views.MAX_ALIAS_CHARS)
    lang: str | None = Field(default=None, max_length=8)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Literal["resolved_a", "resolved_b", "resolved_both_valid", "merged", "kept_separate"]
    note: str | None = Field(default=None, max_length=2000)


def _not_found(exc: lib.KbNotFound) -> HTTPException:
    return HTTPException(404, f"kb_{exc}_not_found")


# ---------------------------------------------------------------------------------------------
# 요약·문서
# ---------------------------------------------------------------------------------------------
@router.get("/summary")
async def kb_summary(_: User, db: Db, scope: LegacyScope = None) -> dict:
    return await kb_v3_views.summary_v3(db, web_access(scope))


@router.get("/documents")
async def kb_documents(
    _: User, db: Db,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    domain: str | None = Query(None, max_length=32),
    pipeline_status: str | None = Query(None, max_length=24, description="작업 상태, active(진행 중), attention(보류·실패·저하), none(처리 이력 없음)"),
    scope: LegacyScope = None,
) -> dict:
    try:
        return await kb_v3_views.list_documents_v3(
            db, web_access(scope), q=q, domain=domain, pipeline_status=pipeline_status, limit=limit, offset=offset
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/documents", status_code=201)
async def kb_register(body: RegisterIn, _: User, db: Db) -> dict:
    try:
        result = await lib.register_text(
            db, title=body.title, text=body.text, source_key=body.source_key, lang=body.lang,
            locator=body.locator, scope=body.scope,
        )
        record(
            db, actor="user", action="kb.register", target_type="kb_revision", target_id=result.revision_id,
            detail={"chars": len(body.text), "created": result.created},
        )
        job = await db.scalar(select(KbPipelineJob).where(KbPipelineJob.revision_id == result.revision_id))
        await db.commit()
    except KbInputError as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        # 같은 바이트를 동시에 등록하면 한쪽이 unique 충돌이다. 재시도하면 기존 행을 받는다(멱등).
        await db.rollback()
        raise HTTPException(409, "kb_register_conflict") from exc
    except DBAPIError as exc:
        await db.rollback()
        raise HTTPException(422, "kb_register_rejected") from exc
    return {
        "document_id": str(result.document_id),
        "revision_id": str(result.revision_id),
        "rev_no": result.rev_no,
        "created": result.created,
        "passage_count": result.passage_count,
        # v3 추가: 등록 즉시 자동 지식화 작업이 큐에 들어간다
        "job_id": str(job.id) if job else None,
        "pipeline_status": job.status if job else None,
    }


@router.get("/documents/{document_id}")
async def kb_document(
    document_id: uuid.UUID, _: User, db: Db,
    revision_id: uuid.UUID | None = None,
    passage_limit: int = Query(100, ge=1, le=500),
    passage_offset: int = Query(0, ge=0),
    scope: LegacyScope = None,
) -> dict:
    try:
        return await kb_v3_views.document_detail_v3(
            db, web_access(scope), document_id, revision_id=revision_id, passage_limit=passage_limit, passage_offset=passage_offset
        )
    except lib.KbNotFound as exc:
        raise _not_found(exc) from exc


# ---------------------------------------------------------------------------------------------
# 통합 등록과 파이프라인 작업
# ---------------------------------------------------------------------------------------------
async def _read_upload(upload: Any) -> bytes:
    """업로드 파일을 상한까지만 읽는다. 상한을 넘으면 읽기를 멈추고 한 바이트 넘긴 데이터를 돌려줘 항목 단위로 거절하게 한다."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        chunks.append(chunk)
        total += len(chunk)
        if total > kb_ingest.MAX_FILE_BYTES:
            break
    return b"".join(chunks)


@router.post("/ingest")
async def kb_ingest_route(request: Request, _: User, db: Db) -> dict:
    """텍스트·URL·여러 파일을 하나의 입구로 받는다. multipart(files[], urls[], text, title, domain, source_kind) 또는 JSON.

    항목별 상태: registered(작업 큐잉) / duplicate(같은 바이트가 이미 있음) / held(미지원·보류, 사유 포함) / failed(error 코드).
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    items: list[kb_ingest.IngestInput] = []
    urls: list[str] = []
    text = title = domain = source_kind = None
    if content_type == "application/json":
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(422, "ingest_invalid_json") from exc
        if not isinstance(body, dict) or set(body) - {"text", "title", "urls", "domain", "source_kind"}:
            raise HTTPException(422, "ingest_invalid_body")
        text, title, domain, source_kind = body.get("text"), body.get("title"), body.get("domain"), body.get("source_kind")
        urls = body.get("urls") or []
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise HTTPException(422, "ingest_invalid_urls")
    elif content_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        form = await request.form(max_files=kb_ingest.MAX_ITEMS_PER_BATCH, max_fields=200)
        text, title, domain, source_kind = form.get("text"), form.get("title"), form.get("domain"), form.get("source_kind")
        urls = [str(u) for key in ("urls[]", "urls") for u in form.getlist(key)]
        for key in ("files[]", "files"):
            for upload in form.getlist(key):
                if not hasattr(upload, "read"):
                    raise HTTPException(422, "ingest_invalid_file")
                items.append(kb_ingest.IngestInput(
                    kind="file", name=kb_ingest._clean_filename(upload.filename), data=await _read_upload(upload),
                    content_type=upload.content_type,
                ))
    else:
        raise HTTPException(415, "ingest_unsupported_content_type")

    for field_name, value in (("text", text), ("title", title), ("domain", domain), ("source_kind", source_kind)):
        if value is not None and not isinstance(value, str):
            raise HTTPException(422, f"ingest_invalid_{field_name}")
    title = (title or "").strip() or None
    if title and (len(title) > 500 or "\x00" in title):
        raise HTTPException(422, "ingest_invalid_title")
    domain = (domain or "").strip() or None
    if domain and (len(domain) > 32 or "\x00" in domain):
        raise HTTPException(422, "ingest_invalid_domain")
    source_kind = (source_kind or "").strip() or None
    if text and text.strip():
        items.append(kb_ingest.IngestInput(kind="text", name=title or "텍스트", data=text.encode("utf-8")))
    for raw_url in [u.strip() for u in urls if u and u.strip()]:
        try:
            items.append(await kb_ingest.fetch_url(raw_url))
        except KbInputError as exc:
            items.append(kb_ingest.IngestInput(kind="url", name=raw_url[:200], url=raw_url, error=str(exc)))
    try:
        result = await kb_ingest.ingest_batch(db, items, title=title, domain=domain, source_kind=source_kind)
        record(
            db, actor="user", action="kb.ingest", target_type="kb_batch", target_id=uuid.UUID(result["batch_id"]),
            detail={"items": len(result["items"]), "statuses": [i["status"] for i in result["items"]]},
        )
        await db.commit()
    except KbInputError as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except DBAPIError as exc:
        await db.rollback()
        raise HTTPException(422, "kb_ingest_rejected") from exc
    return result


@router.get("/jobs")
async def kb_jobs_list(
    _: User, db: Db,
    status: str | None = Query(None, max_length=24, description="작업 상태 또는 그룹 active(진행 중)·attention(보류·실패·저하)"),
    document_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=kb_jobs.MAX_JOB_PAGE),
    offset: int = Query(0, ge=0),
    scope: LegacyScope = None,
) -> dict:
    try:
        return await kb_jobs.list_jobs(db, web_access(scope), status=status, document_id=document_id, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/jobs/{job_id}")
async def kb_job(job_id: uuid.UUID, _: User, db: Db, scope: LegacyScope = None) -> dict:
    try:
        return await kb_jobs.get_job(db, web_access(scope), job_id)
    except kb_jobs.JobNotFound as exc:
        raise HTTPException(404, "kb_job_not_found") from exc


@router.post("/jobs/{job_id}/retry")
async def kb_job_retry(job_id: uuid.UUID, body: RetryIn, _: User, db: Db, scope: LegacyScope = None) -> dict:
    """보류·실패·저하 작업을 다시 큐에 넣는다. from_stage 가 없으면 성공하지 않은 첫 단계부터다(멱등)."""
    try:
        view = await kb_jobs.retry_job(sessionmaker(), db, web_access(scope), job_id, body.from_stage)
    except kb_jobs.JobNotFound as exc:
        raise HTTPException(404, "kb_job_not_found") from exc
    except PipelineRetryError as exc:
        message = str(exc)
        raise HTTPException(409 if message.startswith("job_running") else 422, message.split(":", 1)[0]) from exc
    record(db, actor="user", action="kb.job.retry", target_type="kb_pipeline_job", target_id=job_id,
           detail={"from_stage": body.from_stage, "requeued": view["retry"]["requeued"]})
    await db.commit()
    return view


# ---------------------------------------------------------------------------------------------
# 검색·근거
# ---------------------------------------------------------------------------------------------
@router.get("/search")
async def kb_search(
    _: User, db: Db,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(10, ge=1, le=30),
    offset: int = Query(0, ge=0),
    document_id: uuid.UUID | None = None,
    current_only: bool = True,
    mode: Literal["lexical", "hybrid", "vector"] = "lexical",
    domain: str | None = Query(None, max_length=32),
    entity_id: uuid.UUID | None = None,
    include_candidates: bool = Query(False, description="근거 있는 후보(candidate) 관계를 '후보' 표시와 낮은 가중치로 1-hop 확장에 포함"),
    scope: LegacyScope = None,
) -> dict:
    access = web_access(scope)
    if mode == "vector" and not get_settings().kb_semantic_enabled:
        raise HTTPException(503, "kb_semantic_temporarily_unavailable")
    plain_lexical = mode == "lexical" and not (domain or entity_id or include_candidates)
    try:
        if plain_lexical:
            return await lib.search_passages(
                db, query=q, limit=limit, offset=offset, document_id=document_id, current_only=current_only, access=access
            )
        return await kb_v3_views.search_v3(
            db, access, query=q, mode=mode, limit=limit, offset=offset, document_id=document_id, current_only=current_only,
            domain=domain, entity_id=entity_id, include_candidates=include_candidates,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, "kb_semantic_temporarily_unavailable") from exc


@router.get("/passages/{passage_id}/evidence")
async def kb_evidence(
    passage_id: uuid.UUID, _: User, db: Db,
    start: int | None = Query(None, ge=0),
    end: int | None = Query(None, ge=1),
    scope: LegacyScope = None,
) -> dict:
    try:
        return await lib.get_evidence(db, passage_id, start=start, end=end, access=web_access(scope))
    except lib.KbNotFound as exc:
        raise _not_found(exc) from exc
    except KbInputError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------------------------------------
# 온톨로지
# ---------------------------------------------------------------------------------------------
@router.get("/ontology/graph")
async def kb_ontology_graph(
    _: User, db: Db,
    center_entity_id: uuid.UUID | None = None,
    q: str | None = Query(None, max_length=200),
    domain: str | None = Query(None, max_length=32),
    hops: int = Query(1, ge=1, le=3, description="현재 1-hop 확장만 지원한다. 더 큰 값은 1로 응답하고 hops_requested 에 요청값을 남긴다."),
    limit: int = Query(60, ge=1, le=kb_v3_ontology_views.MAX_GRAPH_LIMIT),
    offset: int = Query(0, ge=0, description="center_entity_id 이웃 확장의 페이지 시작 위치"),
    include_candidates: bool = Query(False, description="근거 있는 후보 관계를 '후보' 표시와 낮은 가중치로 포함"),
    scope: LegacyScope = None,
) -> dict:
    try:
        return await kb_v3_ontology_views.graph_v3(
            db, web_access(scope), center_entity_id=center_entity_id, q=q, domain=domain, hops=hops, limit=limit,
            offset=offset, include_candidates=include_candidates,
        )
    except kb_v3_ontology_views.EntityNotFound as exc:
        raise HTTPException(404, "kb_entity_not_found") from exc


@router.get("/ontology/entities/{entity_id}")
async def kb_ontology_entity(
    entity_id: uuid.UUID, _: User, db: Db, include_candidates: bool = False, scope: LegacyScope = None,
) -> dict:
    try:
        return await kb_v3_ontology_views.entity_detail_v3(db, web_access(scope), entity_id, include_candidates=include_candidates)
    except kb_v3_ontology_views.EntityNotFound as exc:
        raise HTTPException(404, "kb_entity_not_found") from exc


@router.post("/ontology/entities/{entity_id}/aliases", status_code=201)
async def kb_ontology_add_alias(entity_id: uuid.UUID, body: AliasIn, _: User, db: Db, scope: LegacyScope = None) -> dict:
    try:
        result = await kb_v3_ontology_views.add_alias(db, web_access(scope), entity_id, alias=body.alias, lang=body.lang)
        record(db, actor="user", action="kb.alias.add", target_type="kb_entity", target_id=entity_id,
               detail={"created": result["created"], "relinked": result["relinked_count"]})
        await db.commit()
    except kb_v3_ontology_views.EntityNotFound as exc:
        await db.rollback()
        raise HTTPException(404, "kb_entity_not_found") from exc
    except kb_v3_ontology_views.AliasInvalid as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except kb_v3_ontology_views.AliasTaken as exc:
        await db.rollback()
        raise HTTPException(409, "kb_alias_belongs_to_other_entity") from exc
    return result


# ---------------------------------------------------------------------------------------------
# 충돌
# ---------------------------------------------------------------------------------------------
@router.get("/conflicts")
async def kb_conflicts(
    _: User, db: Db,
    status: Literal["open", "resolved_a", "resolved_b", "resolved_both_valid", "merged", "kept_separate", "all"] = "open",
    kind: Literal["value_contradiction", "polarity_contradiction", "version_divergence", "ambiguous_entity"] | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    scope: LegacyScope = None,
    entity_id: uuid.UUID | None = None,
) -> dict:
    return await kb_v3_ontology_views.conflicts_v3(
        db, web_access(scope), status=status, kind=kind, limit=limit, offset=offset, entity_id=entity_id
    )


@router.post("/conflicts/{conflict_id}/resolve")
async def kb_conflict_resolve(conflict_id: uuid.UUID, body: ResolveIn, _: User, db: Db, scope: LegacyScope = None) -> dict:
    try:
        result = await kb_v3_ontology_views.resolve_conflict_v3(
            db, web_access(scope), conflict_id, resolution=body.resolution, note=body.note
        )
        record(db, actor="user", action="kb.conflict.resolve", target_type="kb_conflict", target_id=conflict_id,
               detail={"resolution": body.resolution})
        await db.commit()
    except kb_v3_ontology_views.ConflictNotFound as exc:
        await db.rollback()
        raise HTTPException(404, "kb_conflict_not_found") from exc
    except kb_v3_ontology_views.ResolutionNotApplicable as exc:
        await db.rollback()
        raise HTTPException(422, "kb_resolution_not_applicable") from exc
    return result
