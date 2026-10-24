from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

import jwt
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import or_, select

from ttakkari.api.deps import Db, broker_http, get_artifact, get_workspace
from ttakkari.artifacts import preview
from ttakkari.artifacts.kinds import needs_server_preview
from ttakkari.artifacts.service import (
    _safe_filename,
    content_path,
    parse_link_path,
    preview_file,
    register_file,
    remove_stored,
)
from ttakkari.audit import record
from ttakkari.broker.policy import (
    BrokerError,
    ResolvedPath,
    is_sensitive,
    is_within,
    resolve_in_root,
    validate_absolute_in_roots,
)
from ttakkari.config import get_settings
from ttakkari.models import (
    Artifact,
    ArtifactArea,
    ArtifactKeepReason,
    ArtifactOrigin,
    ChatSession,
    ExportPolicy,
    PreviewStatus,
    Workspace,
    utcnow,
)
from ttakkari.schemas import (
    ArtifactAreaIn,
    ArtifactOut,
    ArtifactRegisterIn,
    ArtifactResolveIn,
    DownloadLinkOut,
)
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
    kind: str | None = None, category: str | None = None,
    origin: str | None = None, keep_reason: str | None = None,
    q: str | None = Query(None, max_length=200),
    changed: bool | None = None, area: Literal["files", "agent", "all"] | None = None,
    limit: int = Query(50, le=200), offset: int = Query(0, ge=0),
) -> list[ArtifactOut]:
    stmt = select(Artifact).where(Artifact.deleted_at.is_(None))
    # 보관함은 결과물·업로드만 보여 주려고 changed=false 를 보낸다. 작업이 고친 워크스페이스 파일은 세션 화면과 changed=true 에서만.
    if changed is True:
        stmt = stmt.where(Artifact.source == "agent_modified")
    elif changed is False:
        stmt = stmt.where(Artifact.source != "agent_modified")
    # 보관함 탭: files 는 사용자가 보관한 내 보관함, agent 는 AI 작업 파일. all 또는 생략 시 전체.
    if area and area != "all":
        stmt = stmt.where(Artifact.area == area)
        if area == "files":
            # 보관 사유가 없는 files 행(배포 전환 중 이전 세대가 만든 행 등)은 내 보관함에 보이지 않는다.
            stmt = stmt.where(Artifact.keep_reason.is_not(None))
    if category and category != "all":
        stmt = stmt.where(Artifact.category == category)
    if origin and origin != "all":
        stmt = stmt.where(Artifact.origin == origin)
    if keep_reason:
        stmt = stmt.where(Artifact.keep_reason == keep_reason)
    if workspace_id:
        stmt = stmt.where(Artifact.workspace_id == workspace_id)
    if session_id:
        stmt = stmt.where(Artifact.session_id == session_id)
    if run_id:
        stmt = stmt.where(Artifact.run_id == run_id)
    if kind and kind != "all":
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
        art = await register_file(
            db, r,
            source="user_registered",
            area=ArtifactArea.files,
            origin=ArtifactOrigin.unknown,  # 등록은 보관 의도이지 제작 증거가 아니므로 출처는 unknown
            keep_reason=ArtifactKeepReason.direct_register,
            workspace_id=ws.id,
            session_id=body.session_id,
            export_allowed=ws.export_allowed,
        )
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


async def _latest_artifact(db, *conds) -> Artifact | None:
    stmt = select(Artifact).where(Artifact.deleted_at.is_(None), *conds).order_by(Artifact.created_at.desc()).limit(1)
    return await db.scalar(stmt)


@router.post("/artifacts/resolve", response_model=ArtifactOut)
async def resolve_artifact_link(body: ArtifactResolveIn, _: User, db: Db, request: Request) -> ArtifactOut:
    """메시지 본문 링크의 경로 문자열을 결과물로 바꾼다(뷰어로 열 수 있게).

    이미 등록된 파일이면 그 결과물을 돌려주고, 아니면 워크스페이스 루트나 outbox 안의 파일만 새로 등록한다.
    이 밖의 경로, 민감 파일, 심볼릭 링크로 새는 경로는 열지 않는다.
    """
    s = get_settings()
    try:
        raw = parse_link_path(body.path)
        ws = await db.get(Workspace, body.workspace_id) if body.workspace_id else None
        sess = await db.get(ChatSession, body.session_id) if body.session_id else None
        if ws is None and sess is not None:
            ws = await db.get(Workspace, sess.workspace_id)

        if not os.path.isabs(raw):
            if ws is None:
                raise BrokerError("상대경로를 풀 작업 폴더를 알 수 없습니다.", "bad_path")
            try:
                resolved = resolve_in_root(Path(ws.root_path), raw)
            except BrokerError as e:
                if e.code != "not_found" or sess is None:
                    raise
                # 작업 폴더에 없으면 이 세션이 낸 결과물의 이름으로 찾는다(outbox 파일을 이름만 적은 경우).
                hit = await _latest_artifact(db, Artifact.session_id == sess.id,
                                             or_(Artifact.rel_path == raw, Artifact.filename == raw))
                if hit is None:
                    raise
                return ArtifactOut.from_model(hit)
            known = [str(resolved.path)]
        else:
            known = [os.path.normpath(raw)]
            resolved = None

        # 이미 결과물로 등록된 경로(outbox·업로드·에이전트가 고친 파일)는 그대로 쓴다. 원본이 사라져도 보관 사본이 있다.
        real_guess = str(Path(known[0]).resolve(strict=False))
        hit = await _latest_artifact(db, Artifact.original_path.in_({*known, real_guess}))
        if hit is not None:
            return ArtifactOut.from_model(hit)

        if resolved is None:
            workspaces = list(await db.scalars(select(Workspace)))
            roots = [Path(w.root_path) for w in sorted(workspaces, key=lambda w: w.id != (ws.id if ws else None))]
            roots.append(s.outbox_root)
            real = Path(known[0]).resolve(strict=False)
            # 존재 여부를 흘리지 않도록 허용 범위 판정을 먼저 한다.
            if not any(is_within(real, r.resolve()) for r in roots):
                raise BrokerError("허용된 폴더 밖의 파일이라 열 수 없습니다.", "outside_root")
            resolved = validate_absolute_in_roots(known[0], roots)
        if is_sensitive(resolved.path):
            raise BrokerError("민감한 파일은 열 수 없습니다.", "sensitive")

        in_outbox = is_within(resolved.path, s.outbox_root.resolve())
        owner = ws
        for w in await db.scalars(select(Workspace)):
            if is_within(resolved.path, Path(w.root_path).resolve()):
                owner = w
                break
        art = await register_file(
            db, resolved,
            source="agent_output" if in_outbox else "user_registered",
            area=ArtifactArea.agent,  # 링크를 눌러 열었다고 보관함 파일 탭에 쌓지 않는다.
            origin=ArtifactOrigin.agent if in_outbox else ArtifactOrigin.unknown,  # 비-outbox 링크는 사용자가 만들었다는 증거가 없으므로 unknown
            keep_reason=None,
            workspace_id=owner.id if owner else None,
            session_id=sess.id if sess else None,
            export_allowed=owner.export_allowed if owner else True,
        )
    except BrokerError as e:
        record(db, action="artifact.resolve", outcome="denied", detail={"path": body.path[:300], "code": e.code},
               ip=client_ip(request))
        await db.commit()
        raise broker_http(e) from e
    record(db, action="artifact.resolve", target_type="artifact", target_id=art.id, ip=client_ip(request))
    await db.commit()
    if needs_server_preview(art.kind):
        preview.enqueue(art.id)
    return ArtifactOut.from_model(art)


UPLOAD_CHUNK = 1024 * 1024
EXPIRED_HTML = """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>링크 만료</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;line-height:1.6">
<h1 style="font-size:1.25rem">다운로드 링크가 만료됐어요</h1>
<p>보안을 위해 링크는 5분 동안만 쓸 수 있어요. 따까리 앱에서 결과물을 열고 다시 다운로드해 주세요.</p>
</body></html>"""


@router.post("/uploads", response_model=ArtifactOut, status_code=201)
async def upload(
    _: User, db: Db, request: Request,
    file: Annotated[UploadFile, File()],
    workspace_id: Annotated[uuid.UUID, Form()],
    session_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ArtifactOut:
    """폰에서 올린 파일. 다음 지시의 context_artifact_ids 로 넘기면 에이전트가 원본 경로를 받는다."""
    s = get_settings()
    ws = await get_workspace(db, workspace_id)
    name = _safe_filename(file.filename or "upload")
    if name.startswith("."):
        name = "_" + name
    d = s.uploads_root / uuid.uuid4().hex
    d.mkdir(parents=True, mode=0o700)
    dest = d / name
    size = 0
    try:
        with dest.open("wb") as f:
            while chunk := await file.read(UPLOAD_CHUNK):
                size += len(chunk)
                if size > s.upload_max_bytes:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE,
                                        {"code": "too_large", "message": f"{s.upload_max_bytes // (1024 * 1024)}MB 까지 올릴 수 있어요."})
                f.write(chunk)
        art = await register_file(db, ResolvedPath(root=d.resolve(), path=dest.resolve()), source="upload",
                                  workspace_id=ws.id, session_id=session_id, rel_path=name,
                                  export_allowed=ws.export_allowed, keep_in_place=True)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    record(db, action="artifact.upload", target_type="artifact", target_id=art.id, detail={"size": size},
           ip=client_ip(request))
    await db.commit()
    if needs_server_preview(art.kind):
        preview.enqueue(art.id)
    return ArtifactOut.from_model(art)


@router.get("/artifacts/{art_id}", response_model=ArtifactOut)
async def get_art(art_id: uuid.UUID, _: User, db: Db) -> ArtifactOut:
    return ArtifactOut.from_model(await get_artifact(db, art_id))


@router.patch("/artifacts/{art_id}", response_model=ArtifactOut)
async def move_art(art_id: uuid.UUID, body: ArtifactAreaIn, _: User, db: Db, request: Request) -> ArtifactOut:
    """보관함 분류를 바꾼다(내 보관함 ↔ AI 작업 파일). 파일 내용·세션 연결·출처(origin)는 그대로다."""
    art = await get_artifact(db, art_id)
    if art.area != body.area:
        record(db, action="artifact.move", target_type="artifact", target_id=art.id,
               detail={"from": art.area, "to": body.area}, ip=client_ip(request))
        art.area = body.area
        if body.area == "files":
            if not art.keep_reason:
                art.keep_reason = "user_button"
        else:
            # 내 보관함에서 뺀 파일은 보관 사유/근거가 제거되며, 이후 자동 수집이나 AI 재공유로 되돌아가지 않는다.
            art.keep_reason = None
            art.keep_evidence = None
        await db.commit()
    return ArtifactOut.from_model(art)


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
async def download_link(art_id: uuid.UUID, _: User, db: Db, request: Request, variant: Variant = "original",
                        inline: bool = False) -> DownloadLinkOut:
    """브라우저 기본 다운로드는 Authorization 헤더를 못 싣는다. 그래서 짧게 사는 서명 링크를 따로 발급한다."""
    art = await get_artifact(db, art_id)
    if art.export_policy != ExportPolicy.allow:
        record(db, action="artifact.download_link", outcome="denied", target_type="artifact", target_id=art.id,
               detail={"policy": art.export_policy}, ip=client_ip(request))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": art.export_policy, "message": "반출이 차단된 파일입니다."})
    _file_for(art, variant)
    token, exp = issue_download_token(art.id, variant)
    record(db, action="artifact.download_link", target_type="artifact", target_id=art.id,
           detail={"variant": variant, "inline": inline}, ip=client_ip(request))
    await db.commit()
    # inline 링크는 HTML 미리보기용이다. 프론트와 다른 출처(이 API 도메인)에서 sandbox iframe 으로 연다.
    return DownloadLinkOut(url=f"/api/{'view' if inline else 'dl'}/{token}", expires_at=exp)


@router.get("/view/{token}", response_model=None)
async def view_inline(token: str, db: Db, request: Request) -> FileResponse | HTMLResponse:
    return await _serve_token(token, db, request, inline=True)


@router.get("/dl/{token}", response_model=None)
async def download(token: str, db: Db, request: Request) -> FileResponse | HTMLResponse:
    return await _serve_token(token, db, request, inline=False)


async def _serve_token(token: str, db: Db, request: Request, *, inline: bool) -> FileResponse | HTMLResponse:
    try:
        claims = decode(token, "download")
    except jwt.PyJWTError as e:
        record(db, actor="user", action="artifact.download", outcome="denied", detail={"reason": type(e).__name__},
               ip=client_ip(request))
        await db.commit()
        if "text/html" in request.headers.get("accept", ""):
            # 브라우저로 직접 연 링크는 JSON 대신 사람이 읽는 안내를 보여 준다.
            return HTMLResponse(EXPIRED_HTML, status_code=403, headers={"Cache-Control": "no-store"})
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
    if inline:
        return FileResponse(path, media_type=mime, headers={**INLINE_HEADERS, "Content-Disposition": _disposition("inline", name)})
    return FileResponse(path, media_type=mime, headers={
        "Content-Disposition": _disposition("attachment", name), "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store", "Content-Security-Policy": "sandbox",
    })
