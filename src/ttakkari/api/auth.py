from __future__ import annotations

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


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, response: Response, db: Db) -> TokenOut:
    ip = client_ip(request) or "unknown"
    wait = limiter.check(ip)
    if wait:
        record(db, action="auth.login", outcome="denied", detail={"reason": "locked"}, ip=ip)
        await db.commit()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"로그인 시도가 너무 많습니다. {wait}초 후 다시 시도하세요.",
                            headers={"Retry-After": str(wait)})
    cred = await db.get(Credential, 1)
    if cred is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "비밀번호가 설정되지 않았습니다. 서버에서 `ttakkari set-password` 를 실행하세요.")
    if not verify_password(cred.password_hash, body.password):
        limiter.fail(ip)
        record(db, action="auth.login", outcome="denied", ip=ip, user_agent=request.headers.get("user-agent"))
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "비밀번호가 올바르지 않습니다.")
    limiter.success(ip)
    if needs_rehash(cred.password_hash):
        cred.password_hash = hash_password(body.password)
    out = await _issue(db, request, response)
    record(db, action="auth.login", ip=ip, user_agent=request.headers.get("user-agent"))
    await db.commit()
    return out


@router.post("/refresh", response_model=TokenOut)
async def refresh(request: Request, response: Response, db: Db,
                  ttk_refresh: Annotated[str | None, Cookie()] = None) -> TokenOut:
    if not ttk_refresh:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 없습니다.")
    rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh(ttk_refresh)))
    now = utcnow()
    if rt is None or rt.expires_at < now:
        _clear_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 만료되었습니다.")
    if rt.revoked_at is not None:
        # 이미 교체된 토큰이 다시 쓰였다 = 탈취 가능성. 같은 계열 전부 폐기한다.
        await db.execute(update(RefreshToken).where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
                         .values(revoked_at=now))
        record(db, action="auth.refresh_reuse", outcome="denied", ip=client_ip(request), target_id=rt.family_id)
        await db.commit()
        _clear_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "세션이 무효화되었습니다. 다시 로그인하세요.")
    rt.revoked_at = now
    out = await _issue(db, request, response, family=rt.family_id)
    await db.commit()
    return out


@router.post("/logout", status_code=204)
async def logout(response: Response, db: Db, ttk_refresh: Annotated[str | None, Cookie()] = None) -> None:
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
