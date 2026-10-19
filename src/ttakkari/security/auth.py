from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ttakkari.config import get_settings

ALGO = "HS256"
ISSUER = "ttakkari"
_bearer = HTTPBearer(auto_error=False)


def _now() -> datetime:
    return datetime.now(UTC)


def issue_access_token() -> tuple[str, int]:
    s = get_settings()
    now = _now()
    payload = {"iss": ISSUER, "aud": "api", "sub": "owner", "iat": now,
               "exp": now + timedelta(seconds=s.access_token_ttl_seconds), "jti": uuid.uuid4().hex}
    return jwt.encode(payload, s.jwt_secret, algorithm=ALGO), s.access_token_ttl_seconds


def issue_download_token(artifact_id: uuid.UUID, variant: str) -> tuple[str, datetime]:
    s = get_settings()
    exp = _now() + timedelta(seconds=s.download_token_ttl_seconds)
    payload = {"iss": ISSUER, "aud": "download", "sub": str(artifact_id), "var": variant, "exp": exp,
               "jti": uuid.uuid4().hex}
    return jwt.encode(payload, s.jwt_secret, algorithm=ALGO), exp


def decode(token: str, audience: str) -> dict[str, Any]:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGO], audience=audience, issuer=ISSUER)


def new_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh(raw)


def hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def require_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증이 필요합니다.", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode(creds.credentials, "api")
        if claims.get("sub") != "owner":
            raise jwt.InvalidTokenError("owner subject required")
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "토큰이 만료되었습니다.", headers={"WWW-Authenticate": "Bearer"}) from e
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "유효하지 않은 토큰입니다.", headers={"WWW-Authenticate": "Bearer"}) from e
    return "owner"


User = Annotated[str, Depends(require_user)]

_TRUSTED_PROXY_PEERS = frozenset({"127.0.0.1", "::1", "localhost"})


def client_ip(request: Request) -> str | None:
    peer = request.client.host if request.client else None
    header = get_settings().trust_proxy_ip_header
    if header and peer in _TRUSTED_PROXY_PEERS:
        v = request.headers.get(header)
        if v:
            forwarded = v.split(",")[0].strip()[:64]
            if forwarded:
                return forwarded
    return peer


def _rate_limit_exc(wait: int) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"로그인 시도가 너무 많습니다. {wait}초 후 다시 시도하세요.",
        headers={"Retry-After": str(wait)},
    )


class LoginLimiter:
    """IP 별 실패 횟수와 프로세스 내 로그인 검증 동시성·대기열·전역 실패 검증 처리량을 제한한다."""

    MAX_TRACKED_IPS = 4096
    PRUNE_EVERY_FAILURES = 128

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._failure_count = 0
        self._inflight = 0
        self._sem: asyncio.Semaphore | None = None
        self._sem_size = 0
        self._tokens: float | None = None
        self._last_refill = time.monotonic()

    def _prune(self, items: list[float], window: float, now: float) -> list[float]:
        return [t for t in items if now - t < window]

    def _get_sem(self, concurrency: int) -> asyncio.Semaphore:
        if self._sem is None or (self._sem_size != concurrency and self._inflight <= 1):
            self._sem = asyncio.Semaphore(concurrency)
            self._sem_size = concurrency
        return self._sem

    def _refill_tokens(self, now: float, burst: int, rate: float) -> float:
        if self._tokens is None:
            self._tokens = float(burst)
            self._last_refill = now
            return self._tokens
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._tokens = min(float(burst), self._tokens + elapsed * rate)
        return self._tokens

    def _peek_token_wait(self, now: float) -> int | None:
        s = get_settings()
        tokens = self._refill_tokens(now, s.login_verify_burst, s.login_verify_rate_per_second)
        if tokens >= 1.0:
            return None
        return max(1, math.ceil((1.0 - tokens) / s.login_verify_rate_per_second))

    def _consume_token(self, now: float) -> int | None:
        s = get_settings()
        tokens = self._refill_tokens(now, s.login_verify_burst, s.login_verify_rate_per_second)
        if tokens < 1.0:
            return max(1, math.ceil((1.0 - tokens) / s.login_verify_rate_per_second))
        self._tokens = tokens - 1.0
        return None

    def refund(self) -> None:
        s = get_settings()
        now = time.monotonic()
        tokens = self._refill_tokens(now, s.login_verify_burst, s.login_verify_rate_per_second)
        self._tokens = min(float(s.login_verify_burst), tokens + 1.0)

    def check(self, ip: str) -> int | None:
        s = get_settings()
        now = time.monotonic()
        fails = self._prune(self._fails.get(ip, []), s.login_lock_seconds, now)
        if fails:
            self._fails[ip] = fails
        else:
            self._fails.pop(ip, None)
        if len(fails) >= s.login_max_failures:
            return int(s.login_lock_seconds - (now - fails[0])) + 1
        return None

    @asynccontextmanager
    async def admission(self, ip: str) -> AsyncIterator[None]:
        if (wait := self.check(ip)) is not None:
            raise _rate_limit_exc(wait)
        s = get_settings()
        if self._inflight >= s.login_max_inflight:
            raise _rate_limit_exc(1)
        if (wait := self._peek_token_wait(time.monotonic())) is not None:
            raise _rate_limit_exc(wait)
        self._inflight += 1
        sem = self._get_sem(s.login_verify_concurrency)
        try:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=s.login_queue_timeout_seconds)
            except TimeoutError as e:
                raise _rate_limit_exc(1) from e
            try:
                if (wait := self.check(ip)) is not None:
                    raise _rate_limit_exc(wait)
                if (wait := self._consume_token(time.monotonic())) is not None:
                    raise _rate_limit_exc(wait)
                yield
            finally:
                sem.release()
        finally:
            self._inflight -= 1

    def fail(self, ip: str) -> None:
        now = time.monotonic()
        if self._tokens is not None:
            self._tokens = float(math.floor(self._tokens))
        self._last_refill = now
        self._failure_count += 1
        if self._failure_count % self.PRUNE_EVERY_FAILURES == 0:
            window = get_settings().login_lock_seconds
            self._fails = {
                key: recent for key, values in self._fails.items()
                if (recent := self._prune(values, window, now))
            }
        if ip not in self._fails and len(self._fails) >= self.MAX_TRACKED_IPS:
            self._fails.pop(next(iter(self._fails)))
        self._fails.setdefault(ip, []).append(now)

    def success(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self.refund()

    def reset(self) -> None:
        self._fails.clear()
        self._failure_count = 0
        self._inflight = 0
        self._sem = None
        self._sem_size = 0
        self._tokens = None
        self._last_refill = time.monotonic()


limiter = LoginLimiter()
