from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.db import get_db
from ttakkari.models import Credential, RefreshToken, utcnow
from ttakkari.schemas import LoginIn, MeOut, TokenOut
from ttakkari.security import login_throttle
from ttakkari.security.auth import (
    User,
    client_ip,
    hash_refresh,
    issue_access_token,
    limiter,
    new_refresh_token,
)
from ttakkari.security.passwords import hash_password, needs_rehash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
COOKIE = "ttk_refresh"
COOKIE_PATH = "/api/auth"
Db = Annotated[AsyncSession, Depends(get_db)]


def _set_cookie(resp: Response, raw: str) -> None:
    s = get_settings()
    # 프론트(Vercel)와 백엔드(터널 도메인)가 다른 사이트라 운영에서는 SameSite=None 이어야 쿠키가 실린다.
    resp.set_cookie(
        COOKIE, raw, max_age=s.refresh_token_ttl_days * 86400, path=COOKIE_PATH, httponly=True,
        secure=s.is_prod, samesite="none" if s.is_prod else "lax",
    )


def _clear_cookie(resp: Response) -> None:
    s = get_settings()
    resp.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, secure=s.is_prod, samesite="none" if s.is_prod else "lax")


def _clear_cookie_header() -> str:
    response = Response()
    _clear_cookie(response)
    return response.headers["set-cookie"]


async def _issue(db: AsyncSession, request: Request, resp: Response, family: uuid.UUID | None = None) -> TokenOut:
    s = get_settings()
    raw, h = new_refresh_token()
    rt = RefreshToken(
        family_id=family or uuid.uuid4(), token_hash=h, expires_at=utcnow() + timedelta(days=s.refresh_token_ttl_days),
        user_agent=(request.headers.get("user-agent") or "")[:512], ip=client_ip(request),
    )
    db.add(rt)
    await db.flush()
    _set_cookie(resp, raw)
    token, ttl = issue_access_token()
    return TokenOut(access_token=token, expires_in=ttl)


def _check_csrf(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin not in get_settings().cors_origins:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "허용되지 않은 요청 출처입니다.")
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site == "cross-site" and not origin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "교차 사이트 요청은 허용되지 않습니다.")


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, response: Response, db: Db) -> TokenOut:
    ip = client_ip(request) or "unknown"
    async with limiter.admission(ip):
        # SEC2-LGN-01 S1: 새 제한 설계를 같은 시도에 가상 적용해 DB 에 남긴다. 비차단·예외 비전파라 판정·응답·지연이 달라지지 않는다.
        shadow = login_throttle.begin(ip)
        cred = await db.get(Credential, 1)
        if cred is None:
            limiter.refund()
            login_throttle.finish(shadow, None)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "비밀번호가 설정되지 않았습니다. 서버에서 `ttakkari set-password` 를 실행하세요.",
            )
        verified = await asyncio.to_thread(verify_password, cred.password_hash, body.password)
        if not verified:
            limiter.fail(ip)
            login_throttle.finish(shadow, False)
            record(db, action="auth.login", outcome="denied", ip=ip, user_agent=request.headers.get("user-agent"))
            await db.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "비밀번호가 올바르지 않습니다.")
        limiter.success(ip)
        login_throttle.finish(shadow, True)
        if needs_rehash(cred.password_hash):
            # updated_at 은 비밀번호 변경 시각이라 재해시로 올리면 다른 기기의 refresh 세션이 모두 무효화된다.
            new_hash = await asyncio.to_thread(hash_password, body.password)
            await db.execute(
                update(Credential).where(Credential.id == 1)
                .values(password_hash=new_hash, updated_at=Credential.updated_at)
            )
        out = await _issue(db, request, response)
        record(db, action="auth.login", ip=ip, user_agent=request.headers.get("user-agent"))
        await db.commit()
        return out


@router.post("/refresh", response_model=TokenOut)
async def refresh(request: Request, response: Response, db: Db,
                  ttk_refresh: Annotated[str | None, Cookie()] = None) -> TokenOut:
    _check_csrf(request)
    if not ttk_refresh:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 없습니다.")
    token_hash = hash_refresh(ttk_refresh)
    rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    now = utcnow()
    if rt is None or rt.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 만료되었습니다.",
                            headers={"Set-Cookie": _clear_cookie_header()})
    # Serialize refresh against both the same-token rotation and password changes.
    cred = await db.scalar(select(Credential).where(Credential.id == 1).with_for_update())
    rt = await db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        .execution_options(populate_existing=True)
    )
    if rt is None or rt.expires_at < utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 만료되었습니다.",
                            headers={"Set-Cookie": _clear_cookie_header()})
    if cred is not None and cred.updated_at and rt.created_at < cred.updated_at:
        rt.revoked_at = now
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "비밀번호가 변경되어 세션이 무효화되었습니다. 다시 로그인하세요.",
                            headers={"Set-Cookie": _clear_cookie_header()})
    if rt.revoked_at is not None:
        # 이미 교체된 토큰이 다시 쓰였다 = 탈취 가능성. 같은 계열 전부 폐기한다.
        await db.execute(update(RefreshToken).where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
                         .values(revoked_at=now))
        record(db, action="auth.refresh_reuse", outcome="denied", ip=client_ip(request), target_id=rt.family_id)
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 무효화되었습니다. 다시 로그인하세요.",
                            headers={"Set-Cookie": _clear_cookie_header()})
    rt.revoked_at = now
    out = await _issue(db, request, response, family=rt.family_id)
    await db.commit()
    return out


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: Db, ttk_refresh: Annotated[str | None, Cookie()] = None) -> None:
    _check_csrf(request)
    if ttk_refresh:
        rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh(ttk_refresh)))
        if rt is not None:
            await db.execute(update(RefreshToken).where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
                             .values(revoked_at=utcnow()))
            await db.commit()
    _clear_cookie(response)


@router.post("/logout-all", status_code=204)
async def logout_all(_: User, response: Response, db: Db) -> None:
    await db.execute(update(RefreshToken).where(RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    record(db, action="auth.logout_all")
    await db.commit()
    _clear_cookie(response)


@router.get("/me", response_model=MeOut)
async def me(_: User) -> MeOut:
    return MeOut()
