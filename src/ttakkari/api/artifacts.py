from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import jwt
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import or_, select

from ttakkari.api.deps import Db, broker_http, get_artifact, get_workspace
from ttakkari.artifacts import preview
from ttakkari.artifacts.kinds import needs_server_preview
from ttakkari.artifacts.service import (
    content_path,
    preview_file,
    register_file,
    remove_stored,
)
from ttakkari.audit import record
from ttakkari.broker.policy import BrokerError, resolve_in_root
from ttakkari.models import Artifact, ExportPolicy, PreviewStatus, utcnow
from ttakkari.schemas import ArtifactOut, ArtifactRegisterIn, DownloadLinkOut
from ttakkari.security.auth import User, client_ip, decode, issue_download_token

router = APIRouter(prefix="/api", tags=["artifacts"])
Variant = Literal["original", "preview"]

# 열람용 응답은 프론트 origin 에서 실행되면 안 된다. 직접 열어도 스크립트가 돌지 않게 CSP sandbox 를 건다.
INLINE_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


def _disposition(kind: str, filename: str) -> str:
    return f"{kind}; filename*=UTF-8''{quote(filename)}"


@router.get("/artifacts", response_model=list[ArtifactOut])
async def list_artifacts(
    _: User, db: Db,
    workspace_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None,
    kind: str | None = None, q: str | None = Query(None, max_length=200),
    limit: int = Query(50, le=200), offset: int = Query(0, ge=0),
) -> list[ArtifactOut]:
    stmt = select(Artifact).where(Artifact.deleted_at.is_(None))
    if workspace_id:
        stmt = stmt.where(Artifact.workspace_id == workspace_id)
    if session_id:
        stmt = stmt.where(Artifact.session_id == session_id)
    if run_id:
        stmt = stmt.where(Artifact.run_id == run_id)
    if kind:
        stmt = stmt.where(Artifact.kind == kind)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(or_(Artifact.filename.ilike(like), Artifact.rel_path.ilike(like)))
    stmt = stmt.order_by(Artifact.created_at.desc()).limit(limit).offset(offset)
    return [ArtifactOut.from_model(a) for a in await db.scalars(stmt)]


@router.post("/artifacts", response_model=ArtifactOut, status_code=201)
async def register_artifact(body: ArtifactRegisterIn, _: User, db: Db, request: Request) -> ArtifactOut:
    """사용자가 파일 탐색에서 고른 워크스페이스 파일을 결과물로 등록한다."""
    ws = await get_workspace(db, body.workspace_id)
    try:
        r = resolve_in_root(Path(ws.root_path), body.rel_path)
        art = await register_file(db, r, source="user_registered", workspace_id=ws.id, session_id=body.session_id,
                                  export_allowed=ws.export_allowed)
    except BrokerError as e:
        record(db, action="artifact.register", outcome="denied", detail={"rel_path": body.rel_path, "code": e.code},
               ip=client_ip(request))
        await db.commit()
        raise broker_http(e) from e
    record(db, action="artifact.register", target_type="artifact", target_id=art.id, ip=client_ip(request))
    await db.commit()
    if needs_server_preview(art.kind):
        preview.enqueue(art.id)
    return ArtifactOut.from_model(art)


@router.get("/artifacts/{art_id}", response_model=ArtifactOut)
async def get_art(art_id: uuid.UUID, _: User, db: Db) -> ArtifactOut:
    return ArtifactOut.from_model(await get_artifact(db, art_id))


@router.delete("/artifacts/{art_id}", status_code=204)
async def delete_art(art_id: uuid.UUID, _: User, db: Db, request: Request) -> None:
    art = await get_artifact(db, art_id)
    remove_stored(art)
    art.deleted_at = utcnow()
    record(db, action="artifact.delete", target_type="artifact", target_id=art.id, ip=client_ip(request))
    await db.commit()


@router.post("/artifacts/{art_id}/preview/retry", response_model=ArtifactOut)
async def retry_preview(art_id: uuid.UUID, _: User, db: Db) -> ArtifactOut:
    art = await get_artifact(db, art_id)
    if not needs_server_preview(art.kind):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "서버 미리보기가 필요 없는 형식입니다.")
    if art.preview_status in (PreviewStatus.failed, PreviewStatus.unavailable):
        art.preview_status = PreviewStatus.pending
        art.preview_error = None
        await db.commit()
        preview.enqueue(art.id)
    return ArtifactOut.from_model(art)


def _file_for(art: Artifact, variant: Variant):
    if variant == "preview":
        p = preview_file(art)
        if p is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "no_preview", "message": "미리보기가 준비되지 않았습니다."})
        return p, art.preview_mime or "application/pdf", art.filename.rsplit(".", 1)[0] + ".pdf"
    try:
        return content_path(art), art.mime_type, art.filename
    except BrokerError as e:
        raise broker_http(e) from e


@router.get("/artifacts/{art_id}/content")
async def view_content(art_id: uuid.UUID, _: User, db: Db, request: Request,
                       variant: Variant = "original") -> FileResponse:
    """뷰어용 인라인 응답(Bearer 필요). Range 요청을 지원해 PDF.js 지연 로딩이 된다.

    열람도 원본 내용을 원격으로 보내는 일이라 민감 파일은 열람까지 막는다.
    """
    art = await get_artifact(db, art_id)
    if art.export_policy == ExportPolicy.sensitive:
        record(db, action="artifact.view", outcome="denied", target_type="artifact", target_id=art.id,
               detail={"reason": "sensitive"}, ip=client_ip(request))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "sensitive", "message": "민감 파일은 원격 열람이 차단됩니다."})
    path, mime, name = _file_for(art, variant)
    if request.headers.get("range") is None:
        record(db, action="artifact.view", target_type="artifact", target_id=art.id, detail={"variant": variant},
               ip=client_ip(request))
        await db.commit()
    return FileResponse(path, media_type=mime, headers={**INLINE_HEADERS, "Content-Disposition": _disposition("inline", name)})


@router.post("/artifacts/{art_id}/download-link", response_model=DownloadLinkOut)
async def download_link(art_id: uuid.UUID, _: User, db: Db, request: Request, variant: Variant = "original") -> DownloadLinkOut:
    """브라우저 기본 다운로드는 Authorization 헤더를 못 싣는다. 그래서 짧게 사는 서명 링크를 따로 발급한다."""
    art = await get_artifact(db, art_id)
    if art.export_policy != ExportPolicy.allow:
        record(db, action="artifact.download_link", outcome="denied", target_type="artifact", target_id=art.id,
               detail={"policy": art.export_policy}, ip=client_ip(request))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": art.export_policy, "message": "반출이 차단된 파일입니다."})
    _file_for(art, variant)
    token, exp = issue_download_token(art.id, variant)
    record(db, action="artifact.download_link", target_type="artifact", target_id=art.id, detail={"variant": variant},
           ip=client_ip(request))
    await db.commit()
    return DownloadLinkOut(url=f"/api/dl/{token}", expires_at=exp)


@router.get("/dl/{token}")
async def download(token: str, db: Db, request: Request) -> FileResponse:
    try:
        claims = decode(token, "download")
    except jwt.PyJWTError as e:
        record(db, actor="user", action="artifact.download", outcome="denied", detail={"reason": type(e).__name__},
               ip=client_ip(request))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "다운로드 링크가 만료되었거나 올바르지 않습니다.") from e
    art = await get_artifact(db, uuid.UUID(claims["sub"]))
    # 링크 발급 후 정책이 바뀌었을 수 있으니 다시 본다.
    if art.export_policy != ExportPolicy.allow:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "반출이 차단된 파일입니다.")
    path, mime, name = _file_for(art, claims.get("var", "original"))
    if request.headers.get("range") is None:
        record(db, action="artifact.download", target_type="artifact", target_id=art.id,
               detail={"jti": claims.get("jti"), "variant": claims.get("var")}, ip=client_ip(request),
               user_agent=request.headers.get("user-agent"))
        await db.commit()
    return FileResponse(path, media_type=mime, headers={
        "Content-Disposition": _disposition("attachment", name), "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store", "Content-Security-Policy": "sandbox",
    })
