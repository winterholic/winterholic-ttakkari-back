"""폰 → 컴퓨터 파일 보내기 API. 앱은 조각 업로드, 아이폰 단축어는 한 번에 올리기(업로드 전용 토큰)."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select

from ttakkari.api.deps import Db, broker_http
from ttakkari.api.workspaces import ensure_default_workspace
from ttakkari.audit import record
from ttakkari.broker.policy import BrokerError, is_sensitive
from ttakkari.config import get_settings
from ttakkari.models import TransferToken, utcnow
from ttakkari.security.auth import User, client_ip, require_user
from ttakkari.transfer import service

router = APIRouter(prefix="/api/transfers", tags=["transfers"])
_bearer = HTTPBearer(auto_error=False)
TOKEN_PREFIX = "ttk_up_"
MAX_DIRS = 500


class TransferIn(BaseModel):
    filename: str = Field(min_length=1, max_length=1000)
    size: int = Field(ge=0)
    dest_dir: str = Field(min_length=1, max_length=4096)
    convert_heic: bool = True
    strip_location: bool = True
    keep_original: bool = False


class TransferOut(BaseModel):
    id: str
    received: int
    size: int
    chunk_size: int


class DeliveredOut(BaseModel):
    path: str
    name: str
    size: int
    artifact_id: str | None


class CompleteOut(BaseModel):
    files: list[DeliveredOut]
    notes: list[str]


class FolderOut(BaseModel):
    name: str
    path: str


class FoldersOut(BaseModel):
    path: str | None
    parent: str | None
    roots: list[FolderOut]
    dirs: list[FolderOut]
    default: str
    can_write: bool


class FolderIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class TokenIn(BaseModel):
    name: str = Field(default="아이폰 단축어", min_length=1, max_length=60)


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: str
    last_used_at: str | None
    # 만들 때 한 번만 채운다.
    token: str | None = None


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _err(e: BrokerError) -> HTTPException:
    if e.code == "incomplete":
        return HTTPException(status.HTTP_409_CONFLICT, {"code": e.code, "message": str(e)})
    if e.code == "too_large":
        return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, {"code": e.code, "message": str(e)})
    return broker_http(e)


# ---- 폴더 고르기

@router.get("/folders", response_model=FoldersOut)
async def folders(_: User, db: Db, path: Annotated[str | None, Query(max_length=4096)] = None) -> FoldersOut:
    ws = await ensure_default_workspace(db)
    scope = service.write_scope(ws)
    roots = [FolderOut(name=r.name, path=str(r)) for r in scope]
    inbox = str(get_settings().inbox_root)
    if path is None:
        return FoldersOut(path=None, parent=None, roots=roots, dirs=[], default=inbox, can_write=False)
    try:
        cur = service.check_dest(path, scope)
    except BrokerError as e:
        raise broker_http(e) from e
    dirs: list[FolderOut] = []
    if cur.is_dir():
        with os.scandir(cur) as it:
            for de in sorted(it, key=lambda d: d.name.lower()):
                if len(dirs) >= MAX_DIRS:
                    break
                if de.name.startswith(".") or not de.is_dir(follow_symlinks=False) or is_sensitive(Path(de.path)):
                    continue
                dirs.append(FolderOut(name=de.name, path=str(Path(de.path))))
    parent = None if any(cur == r for r in scope) else str(cur.parent)
    return FoldersOut(path=str(cur), parent=parent, roots=roots, dirs=dirs, default=inbox, can_write=True)


@router.post("/folders", response_model=FolderOut, status_code=201)
async def make_folder(body: FolderIn, _: User, db: Db) -> FolderOut:
    ws = await ensure_default_workspace(db)
    try:
        p = service.check_dest(body.path, service.write_scope(ws), create=True)
    except BrokerError as e:
        raise broker_http(e) from e
    return FolderOut(name=p.name, path=str(p))


# ---- 조각 업로드(앱)

@router.post("", response_model=TransferOut, status_code=201)
async def start(body: TransferIn, _: User, db: Db) -> TransferOut:
    s = get_settings()
    if body.size > s.transfer_max_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE,
                            {"code": "too_large", "message": f"{s.transfer_max_bytes // 1024**3}GB 까지 보낼 수 있어요."})
    ws = await ensure_default_workspace(db)
    try:
        service.check_dest(body.dest_dir, service.write_scope(ws))
    except BrokerError as e:
        raise broker_http(e) from e
    meta = service.create(service.Meta(id=str(uuid.uuid4()), filename=body.filename, size=body.size, dest_dir=body.dest_dir,
                                       convert_heic=body.convert_heic, strip_location=body.strip_location,
                                       keep_original=body.keep_original))
    return TransferOut(id=meta.id, received=0, size=meta.size, chunk_size=s.transfer_chunk_bytes)


@router.get("/{tid}", response_model=TransferOut)
async def status_of(tid: uuid.UUID, _: User) -> TransferOut:
    try:
        meta = service.load(str(tid))
        return TransferOut(id=meta.id, received=service.received(meta.id), size=meta.size,
                           chunk_size=get_settings().transfer_chunk_bytes)
    except BrokerError as e:
        raise _err(e) from e


@router.put("/{tid}", response_model=TransferOut)
async def put_chunk(tid: uuid.UUID, request: Request, _: User,
                    offset: Annotated[int, Query(ge=0)]) -> TransferOut:
    s = get_settings()
    body = await request.body()
    if len(body) > s.transfer_chunk_bytes * 2:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, {"code": "chunk_too_large", "message": "조각이 너무 커요."})
    try:
        got = service.append(str(tid), offset, body)
        meta = service.load(str(tid))
    except service.OffsetMismatch as e:
        # 끊겼다 다시 붙은 경우: 서버가 가진 크기부터 이어 보내게 한다.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"code": "offset_mismatch", "message": f"received={e.received}"}) from e
    except BrokerError as e:
        raise _err(e) from e
    return TransferOut(id=meta.id, received=got, size=meta.size, chunk_size=s.transfer_chunk_bytes)


@router.post("/{tid}/complete", response_model=CompleteOut)
async def complete(tid: uuid.UUID, _: User, db: Db, request: Request) -> CompleteOut:
    ws = await ensure_default_workspace(db)
    try:
        files, notes = await service.finalize(db, ws, str(tid))
    except BrokerError as e:
        raise _err(e) from e
    record(db, action="transfer.complete", detail={"files": [f.path for f in files]}, ip=client_ip(request))
    await db.commit()
    service.flush_previews()
    return CompleteOut(files=[DeliveredOut(**f.__dict__) for f in files], notes=notes)


@router.delete("/{tid}", status_code=204)
async def cancel(tid: uuid.UUID, _: User) -> None:
    service.cancel(str(tid))


# ---- 한 번에 올리기. 사용자 토큰 또는 업로드 전용 토큰.

async def _simple_auth(db: Db, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> str:
    if creds is not None and creds.credentials.startswith(TOKEN_PREFIX):
        tok = await db.scalar(select(TransferToken).where(TransferToken.token_hash == _hash(creds.credentials),
                                                          TransferToken.revoked_at.is_(None)))
        if tok is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "업로드 토큰이 올바르지 않아요.")
        tok.last_used_at = utcnow()
        return f"token:{tok.name}"
    await require_user(creds)
    return "owner"


@router.post("/simple", response_model=CompleteOut, status_code=201)
async def simple(db: Db, request: Request, who: Annotated[str, Depends(_simple_auth)],
                 files: Annotated[list[UploadFile], File()],
                 dest_dir: Annotated[str | None, Form()] = None,
                 convert_heic: Annotated[bool, Form()] = True,
                 strip_location: Annotated[bool, Form()] = True,
                 keep_original: Annotated[bool, Form()] = False) -> CompleteOut:
    s = get_settings()
    ws = await ensure_default_workspace(db)
    dest = dest_dir or str(s.inbox_root)
    try:
        service.check_dest(dest, service.write_scope(ws))
    except BrokerError as e:
        raise broker_http(e) from e
    all_files: list[DeliveredOut] = []
    notes: list[str] = []
    for up in files:
        meta = service.create(service.Meta(id=str(uuid.uuid4()), filename=up.filename or "upload", size=0, dest_dir=dest,
                                           convert_heic=convert_heic, strip_location=strip_location,
                                           keep_original=keep_original))
        data = service.staging_root() / meta.id / "data"
        size = 0
        try:
            with data.open("wb") as f:
                while chunk := await up.read(1024 * 1024):
                    size += len(chunk)
                    if size > s.transfer_max_bytes:
                        raise BrokerError("파일이 너무 커요.", "too_large")
                    f.write(chunk)
            meta.size = size
            service.save(meta)
            got, n = await service.finalize(db, ws, meta.id)
        except BrokerError as e:
            service.cancel(meta.id)
            raise _err(e) from e
        all_files += [DeliveredOut(**f.__dict__) for f in got]
        notes += [x for x in n if x not in notes]
    record(db, action="transfer.simple", detail={"by": who, "files": [f.path for f in all_files]}, ip=client_ip(request))
    await db.commit()
    service.flush_previews()
    return CompleteOut(files=all_files, notes=notes)


@router.post("/raw", response_model=CompleteOut, status_code=201, openapi_extra={
    "requestBody": {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
})
async def raw(db: Db, request: Request, who: Annotated[str, Depends(_simple_auth)],
              filename: Annotated[str, Query(min_length=1, max_length=1000)],
              dest_dir: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
              convert_heic: bool = True, strip_location: bool = True, keep_original: bool = False) -> CompleteOut:
    """단축어의 요청 본문=파일. multipart 필드 없이 파일 한 개의 바이트를 그대로 받는다."""
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime in {"application/json", "application/x-www-form-urlencoded"} or mime.startswith("multipart/"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, {
            "code": "wrong_body_type", "message": "요청 본문을 '파일'로 선택하고 파일 변수를 넣어 주세요.",
        })
    s = get_settings()
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_length", "message": "잘못된 파일 크기입니다."})
        if int(length) > s.transfer_max_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, {"code": "too_large", "message": "파일이 너무 커요."})
    ws = await ensure_default_workspace(db)
    dest = dest_dir or str(s.inbox_root)
    try:
        service.check_dest(dest, service.write_scope(ws))
    except BrokerError as e:
        raise broker_http(e) from e
    meta = service.create(service.Meta(id=str(uuid.uuid4()), filename=filename, size=0, dest_dir=dest,
                                       convert_heic=convert_heic, strip_location=strip_location,
                                       keep_original=keep_original))
    try:
        data = service.staging_root() / meta.id / "data"
        size = 0
        with data.open("wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > s.transfer_max_bytes:
                    raise BrokerError("파일이 너무 커요.", "too_large")
                f.write(chunk)
        if length is not None and size != int(length):
            raise BrokerError("아직 다 받지 못했어요.", "incomplete")
        meta.size = size
        service.save(meta)
        files, notes = await service.finalize(db, ws, meta.id)
    except BrokerError as e:
        raise _err(e) from e
    finally:
        # 연결 중단·크기 초과·파일 처리 실패 모두 임시 바이트를 지운다.
        service.cancel(meta.id)
    record(db, action="transfer.raw", detail={"by": who, "files": [f.path for f in files]}, ip=client_ip(request))
    await db.commit()
    service.flush_previews()
    return CompleteOut(files=[DeliveredOut(**f.__dict__) for f in files], notes=notes)


# ---- 업로드 전용 토큰

tokens = APIRouter(prefix="/api/transfer-tokens", tags=["transfers"])


def _tok_out(t: TransferToken, raw: str | None = None) -> TokenOut:
    return TokenOut(id=t.id, name=t.name, created_at=t.created_at.isoformat(),
                    last_used_at=t.last_used_at.isoformat() if t.last_used_at else None, token=raw)


@tokens.get("", response_model=list[TokenOut])
async def list_tokens(_: User, db: Db) -> list[TokenOut]:
    rows = await db.scalars(select(TransferToken).where(TransferToken.revoked_at.is_(None)).order_by(TransferToken.created_at))
    return [_tok_out(t) for t in rows]


@tokens.post("", response_model=TokenOut, status_code=201)
async def create_token(body: TokenIn, _: User, db: Db, request: Request) -> TokenOut:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    t = TransferToken(name=body.name.strip(), token_hash=_hash(raw))
    db.add(t)
    record(db, action="transfer_token.create", detail={"name": t.name}, ip=client_ip(request))
    await db.commit()
    return _tok_out(t, raw)


@tokens.delete("/{token_id}", status_code=204)
async def revoke_token(token_id: uuid.UUID, _: User, db: Db, request: Request) -> None:
    t = await db.get(TransferToken, token_id)
    if t is None or t.revoked_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "토큰이 없습니다.")
    t.revoked_at = utcnow()
    record(db, action="transfer_token.revoke", target_type="transfer_token", target_id=t.id, ip=client_ip(request))
    await db.commit()
