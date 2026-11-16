"""초안 생성·편집·검토·게시와 키 관리. Gateway 결과를 확인하기 전에는 로컬 성공 상태를 기록하지 않는다."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.audit import record
from ttakkari.context_sharing.adapter import ContextSourceAdapter, SourceDocument
from ttakkari.context_sharing.errors import ShareError
from ttakkari.context_sharing.gateway import (
    GatewayBadResponse,
    GatewayControl,
    GatewayError,
    GatewayNotConfigured,
    GatewayRejected,
    GatewayUnavailable,
)
from ttakkari.context_sharing.models import (
    ShareDraft,
    ShareDraftDocument,
    ShareKey,
    ShareProject,
    SharePublication,
)
from ttakkari.context_sharing.scanner import HIGH, MEDIUM, Finding, scan_text
from ttakkari.models import CtxEntity

MAX_DOCUMENTS = 50
MAX_DOCUMENT_CHARS = 200_000
MAX_DRAFT_CHARS = 1_000_000
KEY_TTL_SECONDS = 3600
LIMITATIONS = [
    "정규식 기반 탐지입니다. 회사 기밀, 맥락상 비공개 내용, 사람 이름과 숫자는 자동으로 찾지 못하니 게시 전에 직접 읽어 확인하세요.",
    "본문은 색인된 조각을 이어 붙인 것이라 원본 파일과 다를 수 있습니다.",
    "verified_at 이 비어 있는 문서는 아직 원본과 대조하지 않았습니다. 직접 확인했다면 owner_verified 로 표시하세요.",
]


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="microseconds") if dt else None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- digest·미리보기

def compute_digest(draft: ShareDraft, docs: list[ShareDraftDocument]) -> str:
    """게시로 나가는 모든 내용(제목·요약·문서별 본문 해시·출처·검증 표시)의 정규화 해시. 저장된 열이 아니라 본문에서 다시 계산한다."""
    payload = {
        "v": 1, "title": draft.title, "summary": draft.summary,
        "documents": [{"path": d.logical_path, "title": d.title, "sha256": sha256_text(d.content),
                       "source_refs": d.source_refs, "source_revision": d.source_revision,
                       "generated_at": iso(d.generated_at), "verified_at": iso(d.verified_at),
                       "verified_by": d.verified_by} for d in sorted(docs, key=lambda x: x.position)],
    }
    return sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def scan_draft(draft: ShareDraft, docs: list[ShareDraftDocument]) -> list[Finding]:
    found = scan_text(draft.title, field="title") + scan_text(draft.summary, field="summary")
    for d in sorted(docs, key=lambda x: x.position):
        found += scan_text(d.title, field="document_title", document_id=str(d.id))
        found += scan_text(d.content, field="content", document_id=str(d.id))
        for ref in d.source_refs:
            found += scan_text(str(ref), field="source_ref", document_id=str(d.id))
    return found


def finding_out(f: Finding) -> dict[str, Any]:
    return {"rule": f.rule, "category": f.category, "severity": f.severity, "field": f.field,
            "document_id": f.document_id, "line": f.line, "excerpt": f.excerpt}


def preview(draft: ShareDraft, docs: list[ShareDraftDocument]) -> dict[str, Any]:
    findings = scan_draft(draft, docs)
    counts = {HIGH: sum(f.severity == HIGH for f in findings), MEDIUM: sum(f.severity == MEDIUM for f in findings)}
    return {"digest": compute_digest(draft, docs), "blocked": counts[HIGH] > 0, "counts": counts,
            "findings": [finding_out(f) for f in findings], "limitations": LIMITATIONS}


# ---- 직렬화

def document_out(d: ShareDraftDocument) -> dict[str, Any]:
    return {"id": str(d.id), "title": d.title, "logical_path": d.logical_path, "content": d.content,
            "sha256": d.sha256, "source_refs": d.source_refs, "source_revision": d.source_revision,
            "generated_at": iso(d.generated_at), "verified_at": iso(d.verified_at), "verified_by": d.verified_by,
            "content_origin": d.content_origin, "edited": d.edited}


def publication_out(p: SharePublication | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {"id": str(p.id), "status": p.status, "draft_id": str(p.draft_id), "digest": p.digest,
            "snapshot_id": p.gateway_snapshot_id, "published_at": iso(p.published_at), "revoked_at": iso(p.revoked_at)}


def draft_out(draft: ShareDraft, project: ShareProject, docs: list[ShareDraftDocument],
              publication: SharePublication | None, *, detail: bool) -> dict[str, Any]:
    pv = preview(draft, docs)
    out = {"id": str(draft.id), "entity_id": str(project.entity_id) if project.entity_id else None,
           "project_name": project.name, "slug": project.slug, "title": draft.title, "summary": draft.summary,
           "status": draft.status, "revision": draft.revision, "created_by": draft.created_by, "scope": draft.scope,
           "digest": pv["digest"], "created_at": iso(draft.created_at), "updated_at": iso(draft.updated_at),
           "document_count": len(docs), "blocked": pv["blocked"], "finding_counts": pv["counts"],
           "publication": publication_out(publication)}
    if detail:
        out["documents"] = [document_out(d) for d in sorted(docs, key=lambda x: x.position)]
        out["preview"] = pv
    return out


# ---- 조회 보조

async def _docs(db: AsyncSession, draft_id: uuid.UUID) -> list[ShareDraftDocument]:
    return list((await db.scalars(select(ShareDraftDocument).where(ShareDraftDocument.draft_id == draft_id)
                                  .order_by(ShareDraftDocument.position))).all())


async def _publication_for(db: AsyncSession, draft_id: uuid.UUID) -> SharePublication | None:
    return await db.scalar(select(SharePublication).where(SharePublication.draft_id == draft_id))


async def _project_of(db: AsyncSession, draft: ShareDraft) -> ShareProject:
    project = await db.get(ShareProject, draft.project_id)
    assert project is not None
    return project


async def _load_draft(db: AsyncSession, draft_id: uuid.UUID, *, lock: bool = False) -> ShareDraft:
    q = select(ShareDraft).where(ShareDraft.id == draft_id)
    draft = await db.scalar(q.with_for_update() if lock else q)
    if draft is None:
        raise ShareError(404, "draft_not_found", "초안을 찾을 수 없습니다.")
    return draft


async def get_draft(db: AsyncSession, draft_id: uuid.UUID) -> dict[str, Any]:
    draft = await _load_draft(db, draft_id)
    return draft_out(draft, await _project_of(db, draft), await _docs(db, draft.id),
                     await _publication_for(db, draft.id), detail=True)


async def list_drafts(db: AsyncSession, *, entity_id: uuid.UUID | None, status: str | None, limit: int,
                      offset: int) -> dict[str, Any]:
    q = select(ShareDraft, ShareProject).join(ShareProject, ShareProject.id == ShareDraft.project_id)
    if entity_id is not None:
        q = q.where(ShareProject.entity_id == entity_id)
    if status:
        q = q.where(ShareDraft.status == status)
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows = (await db.execute(q.order_by(ShareDraft.created_at.desc()).limit(limit).offset(offset))).all()
    out = []
    for draft, project in rows:
        out.append(draft_out(draft, project, await _docs(db, draft.id), await _publication_for(db, draft.id), detail=False))
    return {"drafts": out, "total": total or 0, "limit": limit, "offset": offset}


# ---- 프로젝트

_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def propose_slug(name: str, entity_id: uuid.UUID) -> str:
    base = _SLUG_BAD.sub("-", name.lower().encode("ascii", "ignore").decode()).strip("-")[:48]
    return base if len(base) >= 3 else f"project-{entity_id.hex[:8]}"


async def ensure_project(db: AsyncSession, entity: CtxEntity) -> ShareProject:
    existing = await db.scalar(select(ShareProject).where(ShareProject.entity_id == entity.id))
    if existing is not None:
        return existing
    slug = propose_slug(entity.name, entity.id)
    if await db.scalar(select(ShareProject.id).where(ShareProject.slug == slug)):
        slug = f"{slug[:60]}-{entity.id.hex[:6]}"
    project = ShareProject(entity_id=entity.id, slug=slug, name=entity.name)
    try:
        async with db.begin_nested():
            db.add(project)
    except IntegrityError:
        existing = await db.scalar(select(ShareProject).where(ShareProject.entity_id == entity.id))
        if existing is None:
            raise ShareError(409, "slug_conflict", "프로젝트 공유 주소를 만들 수 없습니다. 잠시 뒤 다시 시도하세요.") from None
        return existing
    return project


async def _project_by_slug(db: AsyncSession, slug: str) -> ShareProject:
    project = await db.scalar(select(ShareProject).where(ShareProject.slug == slug))
    if project is None:
        raise ShareError(404, "project_not_found", "공유한 적 없는 프로젝트입니다.")
    return project


async def _active_publication(db: AsyncSession, project_id: uuid.UUID, *, lock: bool = False) -> SharePublication | None:
    q = select(SharePublication).where(SharePublication.project_id == project_id, SharePublication.status == "active")
    return await db.scalar(q.with_for_update() if lock else q)


async def project_states(db: AsyncSession, entity_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not entity_ids:
        return {}
    projects = (await db.scalars(select(ShareProject).where(ShareProject.entity_id.in_(entity_ids)))).all()
    by_id = {p.id: p for p in projects}
    if not by_id:
        return {}
    pubs = (await db.scalars(select(SharePublication).where(SharePublication.project_id.in_(by_id))
                             .order_by(SharePublication.published_at))).all()
    latest: dict[uuid.UUID, SharePublication] = {}
    for p in pubs:  # 게시 순으로 덮어써 마지막(활성이 있으면 그것)을 남긴다.
        if p.status in {"active", "revoked"}:
            latest[p.project_id] = p
    now_ = now()
    keys = (await db.execute(select(ShareKey.project_id, func.count(ShareKey.id))
                             .where(ShareKey.project_id.in_(by_id), ShareKey.status == "active", ShareKey.expires_at > now_)
                             .group_by(ShareKey.project_id))).all()
    key_counts = dict(keys)
    return {p.entity_id: {"slug": p.slug, "publication": publication_out(latest.get(p.id)),
                          "active_key_count": key_counts.get(p.id, 0)} for p in projects if p.entity_id}


# ---- 초안 생성·편집

def _unique_paths(docs: list[SourceDocument]) -> list[str]:
    seen: set[str] = set()
    out = []
    for d in docs:
        path = d.ref.logical_path
        if path in seen:
            path = f"{d.ref.source_label}/{path}"
        n = 2
        stem = path
        while path in seen:
            path = f"{stem}-{n}"
            n += 1
        seen.add(path)
        out.append(path)
    return out


async def create_draft(db: AsyncSession, adapter: ContextSourceAdapter, *, entity_id: uuid.UUID,
                       document_ids: list[uuid.UUID], title: str | None, summary: str, actor: str,
                       confirm_work_scope: bool, ip: str | None = None, user_agent: str | None = None) -> dict[str, Any]:
    project_info = await adapter.get_project(entity_id)
    if project_info is None:
        raise ShareError(404, "project_not_found", "라이브러리에 그런 프로젝트가 없습니다.")
    if not document_ids or len(set(document_ids)) > MAX_DOCUMENTS:
        raise ShareError(422, "invalid_selection", f"문서는 1개 이상 {MAX_DOCUMENTS}개 이하로 골라 주세요.")
    docs = await adapter.read_documents(entity_id, document_ids)
    scope = "work" if project_info.scope == "work" or any(d.ref.source_scope == "work" for d in docs) else project_info.scope
    if scope == "work":
        if actor == "agent":
            raise ShareError(403, "work_scope_owner_only", "회사(work) 범위 프로젝트의 초안은 사용자가 직접 만들어야 합니다.")
        if not confirm_work_scope:
            raise ShareError(403, "work_scope_confirmation_required",
                             "회사(work) 범위 프로젝트입니다. 외부 공유 의도를 확인하려면 confirm_work_scope=true 로 다시 요청하세요.")
    if any(len(d.content) > MAX_DOCUMENT_CHARS for d in docs) or sum(len(d.content) for d in docs) > MAX_DRAFT_CHARS:
        raise ShareError(422, "draft_too_large", "선택한 문서가 너무 큽니다. 문서 수를 줄여 주세요.")
    entity = await db.get(CtxEntity, entity_id)
    assert entity is not None
    project = await ensure_project(db, entity)
    generated = now()
    draft = ShareDraft(id=uuid.uuid4(), project_id=project.id, title=(title or f"{entity.name} 프로젝트 맥락").strip(),
                       summary=summary.strip(), created_by=actor, scope=scope, digest="")
    db.add(draft)
    await db.flush()
    paths = _unique_paths(docs)
    rows = [ShareDraftDocument(
        id=uuid.uuid4(), draft_id=draft.id, position=i, source_document_id=d.ref.document_id, title=d.ref.title or path,
        logical_path=path, content=d.content, sha256=sha256_text(d.content), source_refs=[d.source_ref],
        source_revision=d.ref.source_revision, generated_at=generated, content_origin="indexed_chunks")
        for i, (d, path) in enumerate(zip(docs, paths, strict=True))]
    db.add_all(rows)
    await db.flush()
    draft.digest = compute_digest(draft, rows)
    record(db, action="context_share.draft_create", actor=actor, target_type="share_draft", target_id=draft.id,
           detail={"entity_id": str(entity_id), "documents": len(rows), "digest": draft.digest, "scope": scope},
           ip=ip, user_agent=user_agent)
    await db.commit()
    return await get_draft(db, draft.id)


async def update_draft(db: AsyncSession, draft_id: uuid.UUID, patch: dict[str, Any], *, ip: str | None = None,
                       user_agent: str | None = None) -> dict[str, Any]:
    draft = await _load_draft(db, draft_id, lock=True)
    if draft.status != "draft":
        raise ShareError(409, "draft_not_editable", "이미 게시한 초안은 고칠 수 없습니다. 새 초안을 만들어 다시 게시하세요.")
    docs = await _docs(db, draft.id)
    if patch.get("expected_digest") and patch["expected_digest"] != compute_digest(draft, docs):
        raise ShareError(409, "digest_mismatch", "초안이 다른 곳에서 바뀌었습니다. 새로 불러온 뒤 다시 고치세요.")
    by_id = {str(d.id): d for d in docs}
    changed = False
    for field in ("title", "summary"):
        if patch.get(field) is not None and patch[field].strip() != getattr(draft, field):
            setattr(draft, field, patch[field].strip())
            changed = True
    if not draft.title:
        raise ShareError(422, "validation", "제목은 비울 수 없습니다.")
    for rid in patch.get("remove_document_ids") or []:
        doc = by_id.pop(str(rid), None)
        if doc is None:
            raise ShareError(404, "document_not_found", "이 초안에 없는 문서입니다.", extra={"document_id": str(rid)})
        await db.delete(doc)
        changed = True
    if not by_id:
        raise ShareError(422, "draft_empty", "문서를 모두 빼면 초안이 비어 게시할 수 없습니다.")
    for edit in patch.get("documents") or []:
        doc = by_id.get(str(edit["id"]))
        if doc is None:
            raise ShareError(404, "document_not_found", "이 초안에 없는 문서입니다.", extra={"document_id": str(edit["id"])})
        content_changed = False
        if edit.get("title") is not None and edit["title"] != doc.title:
            doc.title, content_changed = edit["title"], True
        if edit.get("content") is not None and edit["content"] != doc.content:
            doc.content, doc.sha256, doc.edited, content_changed = edit["content"], sha256_text(edit["content"]), True, True
        if content_changed:
            doc.verified_at = doc.verified_by = None  # 고친 내용은 아무도 대조하지 않았다.
            changed = True
        if edit.get("owner_verified") is True and doc.verified_at is None:
            doc.verified_at, doc.verified_by = now(), "owner"
            changed = True
        elif edit.get("owner_verified") is False and doc.verified_at is not None:
            doc.verified_at = doc.verified_by = None
            changed = True
    if sum(len(d.content) for d in by_id.values()) > MAX_DRAFT_CHARS or any(len(d.content) > MAX_DOCUMENT_CHARS for d in by_id.values()):
        raise ShareError(422, "draft_too_large", "문서가 너무 큽니다.")
    if changed:
        await db.flush()
        remaining = list(by_id.values())
        for i, d in enumerate(sorted(remaining, key=lambda x: x.position)):
            d.position = i
        draft.revision += 1
        draft.updated_at = now()
        draft.digest = compute_digest(draft, remaining)
        record(db, action="context_share.draft_update", target_type="share_draft", target_id=draft.id,
               detail={"revision": draft.revision, "digest": draft.digest}, ip=ip, user_agent=user_agent)
    await db.commit()
    return await get_draft(db, draft.id)


# ---- Gateway 오류 변환

def gateway_problem(exc: GatewayError) -> ShareError:
    """Gateway 실패를 사용자에게 이해되는 오류로 바꾼다. 상류 본문은 싣지 않는다."""
    if isinstance(exc, GatewayNotConfigured):
        return ShareError(503, "gateway_not_configured",
                          "Context Gateway control 연결이 설정되지 않았습니다. 서버 설정(context_gateway_control_url·token)을 확인하세요.",
                          extra={"gateway": exc.status}, headers={"Retry-After": "300"})
    if isinstance(exc, GatewayUnavailable):
        return ShareError(503, "gateway_unavailable", "Context Gateway 에 연결할 수 없습니다. 잠시 뒤 다시 시도하세요.",
                          headers={"Retry-After": "30"})
    if isinstance(exc, GatewayRejected):
        extra = {"gateway_status": exc.http_status, **({"gateway_code": exc.gateway_code} if exc.gateway_code else {})}
        return ShareError(502, "gateway_rejected", "Context Gateway 가 요청을 거절했습니다. 아무것도 바뀌지 않았습니다.", extra=extra)
    return ShareError(502, "gateway_bad_response", "Context Gateway 응답이 계약과 달라 결과를 확인할 수 없습니다.")


async def _fail(db: AsyncSession, action: str, exc: GatewayError, *, target_type: str, target_id: Any,
                detail: dict[str, Any], ip: str | None, user_agent: str | None) -> ShareError:
    err = gateway_problem(exc)
    record(db, action=action, outcome="error", target_type=target_type, target_id=target_id,
           detail={**detail, "error": err.code}, ip=ip, user_agent=user_agent)
    await db.commit()
    return err


# ---- 게시

def gateway_doc_ids(docs: list[ShareDraftDocument]) -> list[str]:
    """코어의 문서 id 규칙(^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$)에 맞춘 결정적 id. 논리 경로에서 만든다."""
    seen: set[str] = set()
    out = []
    for d in sorted(docs, key=lambda x: x.position):
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(PurePosixPath(d.logical_path).with_suffix(""))).strip("-._")[:100] or "doc"
        doc_id, n = base, 2
        while doc_id in seen:
            doc_id, n = f"{base}-{n}", n + 1
        seen.add(doc_id)
        out.append(doc_id)
    return out


def gateway_source_ref(doc: ShareDraftDocument) -> str:
    """Gateway에는 경로 대신 색인 문서의 불투명한 안정 ID를 보낸다."""
    if doc.source_document_id is not None:
        identifier = str(doc.source_document_id)
    else:
        identifier = sha256_text("\0".join([*sorted(map(str, doc.source_refs)), doc.source_revision]))
    return f"library-document:{identifier}"


def snapshot_payload(project: ShareProject, draft: ShareDraft, docs: list[ShareDraftDocument]) -> dict[str, Any]:
    """코어 SnapshotPublishRequest. verified_at 은 모든 문서가 소유자 검증을 거쳤을 때만 호출된다."""
    ordered = sorted(docs, key=lambda x: x.position)
    return {
        "project_slug": project.slug,
        "project_name": project.name,
        "project_description": draft.summary,
        "source_refs": sorted({gateway_source_ref(d) for d in ordered}),
        "source_revision": f"library-index:{compute_digest(draft, ordered)}",
        "generated_at": iso(max(d.generated_at for d in ordered)),
        "verified_at": iso(min(d.verified_at for d in ordered if d.verified_at)),
        "documents": [{"id": doc_id, "title": d.title[:500], "content": d.content,
                       "content_type": "text/markdown" if d.logical_path.lower().endswith((".md", ".markdown")) else "text/plain",
                       "sha256": sha256_text(d.content)} for doc_id, d in zip(gateway_doc_ids(ordered), ordered, strict=True)],
    }


async def publish_draft(db: AsyncSession, gateway: GatewayControl, draft_id: uuid.UUID, approved_digest: str, *,
                        ip: str | None = None, user_agent: str | None = None) -> dict[str, Any]:
    draft = await _load_draft(db, draft_id, lock=True)
    if draft.status != "draft":
        raise ShareError(409, "draft_not_editable", "이미 게시한 초안입니다. 변경하려면 새 초안을 만드세요.")
    docs = await _docs(db, draft.id)
    digest = compute_digest(draft, docs)
    if approved_digest != digest:
        raise ShareError(409, "digest_mismatch", "승인한 내용과 현재 초안이 다릅니다. 미리보기를 다시 확인하고 새 digest 로 승인하세요.",
                         extra={"current_digest": digest})
    pv = preview(draft, docs)
    if pv["blocked"]:
        raise ShareError(422, "sensitive_content_blocked", "인증정보·내부 주소·로컬 경로 같은 고위험 항목이 남아 있어 게시할 수 없습니다. 초안을 고쳐 제거하세요.",
                         extra={"findings": pv["findings"], "counts": pv["counts"]})
    unverified = [str(d.id) for d in docs if d.verified_at is None]
    if unverified:
        # Gateway 는 verified_at 을 필수로 받는다. 검증하지 않은 문서에 현재 시각을 채우지 않고 소유자 확인을 요구한다.
        raise ShareError(422, "verification_required", "원본과 대조해 확인한 문서만 게시할 수 있습니다. 각 문서를 확인한 뒤 owner_verified 로 표시하세요.",
                         extra={"document_ids": unverified})
    project = await _project_of(db, draft)
    detail = {"digest": digest, "slug": project.slug, "documents": len(docs)}
    try:
        receipt = await gateway.publish_snapshot(snapshot_payload(project, draft, docs))
        if project.gateway_project_id not in (None, receipt["project_id"]):
            raise GatewayBadResponse("gateway project id changed")
    except GatewayError as exc:
        raise await _fail(db, "context_share.publish", exc, target_type="share_draft", target_id=draft.id,
                          detail=detail, ip=ip, user_agent=user_agent) from None
    previous = await _active_publication(db, project.id, lock=True)
    if previous is not None:
        previous.status = "superseded"
        await db.flush()
    project.gateway_project_id = receipt["project_id"]
    publication = SharePublication(id=uuid.uuid4(), project_id=project.id, draft_id=draft.id, digest=digest,
                                   gateway_snapshot_id=receipt["id"][:64],
                                   gateway_manifest_sha256=str(receipt.get("manifest_sha256") or "")[:64] or None,
                                   status="active", published_at=now())
    db.add(publication)
    draft.status = "published"
    draft.updated_at = now()
    record(db, action="context_share.publish", target_type="share_draft", target_id=draft.id,
           detail={**detail, "snapshot_id": publication.gateway_snapshot_id}, ip=ip, user_agent=user_agent)
    await db.commit()
    return {"draft": await get_draft(db, draft.id), "publication": publication_out(publication)}


# ---- 키

def key_out(k: ShareKey, project: ShareProject) -> dict[str, Any]:
    status = k.status
    if status == "active" and k.expires_at <= now():
        status = "expired"
    return {"id": str(k.id), "project_slug": project.slug, "label": k.label, "status": status,
            "ttl_seconds": k.ttl_seconds, "single_use": True, "issued_at": iso(k.issued_at),
            "expires_at": iso(k.expires_at), "revoked_at": iso(k.revoked_at), "publication_id": str(k.publication_id)}


KEY_SCOPES = ("read", "search", "export")


async def issue_key(db: AsyncSession, gateway: GatewayControl, slug: str, *, label: str,
                    scopes: list[str], ip: str | None = None, user_agent: str | None = None) -> dict[str, Any]:
    project = await _project_by_slug(db, slug)
    publication = await _active_publication(db, project.id)
    if publication is None:
        raise ShareError(409, "project_not_published", "게시된 스냅샷이 없는 프로젝트입니다. 먼저 초안을 승인해 게시하세요.")
    if not project.gateway_project_id:
        raise ShareError(503, "gateway_contract_incomplete",
                         "이전 게시 기록에 Gateway 프로젝트 ID가 없어 키를 발급할 수 없습니다. 새 초안을 게시해 주세요.",
                         headers={"Retry-After": "3600"})
    detail = {"slug": slug, "ttl_seconds": KEY_TTL_SECONDS, "scopes": scopes, "publication_id": str(publication.id)}
    issued_at = now()
    issued: dict[str, Any] | None = None
    try:
        issued = await gateway.issue_key(project.gateway_project_id, label=label or f"{project.name} 외부 공유",
                                         scopes=scopes)
        try:
            expires_at = datetime.fromisoformat(issued["expires_at"])
        except ValueError:
            raise GatewayBadResponse("expires_at") from None
        if (expires_at.tzinfo is None
                or expires_at < issued_at + timedelta(seconds=KEY_TTL_SECONDS - 60)
                or expires_at > issued_at + timedelta(seconds=KEY_TTL_SECONDS + 60)):
            # 코어가 고정 1시간 계약을 벗어난 키를 줬다. 그대로 넘기지 않고 폐기한다.
            raise GatewayBadResponse("key expiry is outside the fixed ttl")
    except GatewayError as exc:
        if issued is not None:
            try:
                await gateway.revoke_key(str(issued["key_id"]))
            except GatewayError:
                pass
        raise await _fail(db, "context_share.key_issue", exc, target_type="share_project", target_id=project.id,
                          detail=detail, ip=ip, user_agent=user_agent) from None
    key = ShareKey(id=uuid.uuid4(), project_id=project.id, publication_id=publication.id,
                   gateway_key_id=str(issued["key_id"])[:64], label=label, ttl_seconds=KEY_TTL_SECONDS, issued_at=issued_at,
                   expires_at=expires_at)
    try:
        db.add(key)
        record(db, action="context_share.key_issue", target_type="share_key", target_id=key.id,
               detail={**detail, "gateway_key_id": key.gateway_key_id}, ip=ip, user_agent=user_agent)
        await db.commit()
    except Exception:
        await db.rollback()
        try:  # 로컬에 남지 않은 키가 Gateway 에서 살아 있으면 안 된다.
            await gateway.revoke_key(str(issued["key_id"]))
        except GatewayError:
            pass
        raise
    out = key_out(key, project)
    out["scopes"] = scopes
    out["key"] = issued["key"]
    out["connect"] = {"url": gateway.public_url, "note": "이 키는 지금 한 번만 보입니다. 외부 AI 에 1회 교환용으로 전달하세요."}
    return out


async def list_keys(db: AsyncSession, slug: str | None) -> dict[str, Any]:
    q = select(ShareKey, ShareProject).join(ShareProject, ShareProject.id == ShareKey.project_id)
    if slug:
        q = q.where(ShareProject.slug == slug)
    rows = (await db.execute(q.order_by(ShareKey.issued_at.desc()).limit(200))).all()
    return {"keys": [key_out(k, p) for k, p in rows]}


async def revoke_key(db: AsyncSession, gateway: GatewayControl, key_id: uuid.UUID, *, ip: str | None = None,
                     user_agent: str | None = None) -> dict[str, Any]:
    key = await db.scalar(select(ShareKey).where(ShareKey.id == key_id).with_for_update())
    if key is None:
        raise ShareError(404, "key_not_found", "발급한 적 없는 키입니다.")
    project = await db.get(ShareProject, key.project_id)
    assert project is not None
    if key.status == "revoked":
        return key_out(key, project)
    try:
        await gateway.revoke_key(key.gateway_key_id)
    except GatewayError as exc:
        raise await _fail(db, "context_share.key_revoke", exc, target_type="share_key", target_id=key.id,
                          detail={"slug": project.slug}, ip=ip, user_agent=user_agent) from None
    key.status, key.revoked_at = "revoked", now()  # 코어 영수증에는 시각이 없어 확인한 시각을 쓴다
    record(db, action="context_share.key_revoke", target_type="share_key", target_id=key.id,
           detail={"slug": project.slug}, ip=ip, user_agent=user_agent)
    await db.commit()
    return key_out(key, project)


async def revoke_project(db: AsyncSession, gateway: GatewayControl, slug: str, *, ip: str | None = None,
                         user_agent: str | None = None) -> dict[str, Any]:
    """Gateway 에 프로젝트 철회 호출이 없어 스냅샷을 오래된 것부터 하나씩, 그다음 발급 키를 폐기한다.

    최신 스냅샷을 먼저 폐기하면 코어가 이전 스냅샷을 최신으로 되살리므로 최신을 마지막에 폐기한다.
    Gateway 가 확인한 항목만 그때그때 로컬에 기록하고, 하나라도 실패하면 거기서 멈춰 실패를 알린다."""
    project = await _project_by_slug(db, slug)
    pubs = list((await db.scalars(select(SharePublication).where(
        SharePublication.project_id == project.id, SharePublication.status.in_(("active", "superseded")))
        .order_by(SharePublication.status.desc(), SharePublication.published_at).with_for_update())).all())
    # status 내림차순: superseded 가 active 보다 먼저 온다.
    keys = list((await db.scalars(select(ShareKey).where(ShareKey.project_id == project.id, ShareKey.status == "active")
                                  .with_for_update())).all())
    if not pubs and not keys:
        latest = await db.scalar(select(SharePublication).where(SharePublication.project_id == project.id)
                                 .order_by(SharePublication.published_at.desc()))
        if latest is None:
            raise ShareError(409, "project_not_published", "게시된 적 없는 프로젝트라 철회할 것이 없습니다.")
        return {"project_slug": slug, "publication": publication_out(latest), "revoked_snapshots": 0, "revoked_keys": 0,
                "already_revoked": True}
    done_snapshots = done_keys = 0
    try:
        for pub in pubs:
            await gateway.revoke_snapshot(pub.gateway_snapshot_id)
            pub.status, pub.revoked_at = "revoked", now()
            done_snapshots += 1
            await db.commit()
        for k in keys:
            await gateway.revoke_key(k.gateway_key_id)
            k.status, k.revoked_at = "revoked", now()
            done_keys += 1
            await db.commit()
    except GatewayError as exc:
        err = await _fail(db, "context_share.project_revoke", exc, target_type="share_project", target_id=project.id,
                          detail={"slug": slug, "revoked_snapshots": done_snapshots, "revoked_keys": done_keys},
                          ip=ip, user_agent=user_agent)
        err.extra.update({"partial": {"revoked_snapshots": done_snapshots, "remaining_snapshots": len(pubs) - done_snapshots,
                                      "revoked_keys": done_keys, "remaining_keys": len(keys) - done_keys}})
        raise err from None
    record(db, action="context_share.project_revoke", target_type="share_project", target_id=project.id,
           detail={"slug": slug, "revoked_snapshots": done_snapshots, "revoked_keys": done_keys}, ip=ip, user_agent=user_agent)
    await db.commit()
    latest = await db.scalar(select(SharePublication).where(SharePublication.project_id == project.id)
                             .order_by(SharePublication.published_at.desc()))
    return {"project_slug": slug, "publication": publication_out(latest), "revoked_snapshots": done_snapshots,
            "revoked_keys": done_keys, "already_revoked": False}
