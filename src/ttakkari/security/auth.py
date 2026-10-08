from __future__ import annotations

import hashlib
import secrets
import time
import uuid
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
        decode(creds.credentials, "api")
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "토큰이 만료되었습니다.", headers={"WWW-Authenticate": "Bearer"}) from e
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "유효하지 않은 토큰입니다.", headers={"WWW-Authenticate": "Bearer"}) from e
    return "owner"


User = Annotated[str, Depends(require_user)]


def client_ip(request: Request) -> str | None:
    header = get_settings().trust_proxy_ip_header
    if header:
        v = request.headers.get(header)
        if v:
            return v.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


class LoginLimiter:
    """IP 별 실패 횟수 제한. 단일 프로세스라 메모리로 충분하다. 전역 카운터는 IP 를 바꿔가며 하는 추측도 늦춘다."""

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._global: list[float] = []

    def _prune(self, items: list[float], window: float, now: float) -> list[float]:
        return [t for t in items if now - t < window]

    def check(self, ip: str) -> int | None:
        s = get_settings()
        now = time.monotonic()
        fails = self._prune(self._fails.get(ip, []), s.login_lock_seconds, now)
        self._fails[ip] = fails
        if len(fails) >= s.login_max_failures:
            return int(s.login_lock_seconds - (now - fails[0])) + 1
        self._global = self._prune(self._global, 60, now)
        if len(self._global) >= s.login_max_failures * 4:
            return 60
        return None

    def fail(self, ip: str) -> None:
        now = time.monotonic()
        self._fails.setdefault(ip, []).append(now)
        self._global.append(now)

    def success(self, ip: str) -> None:
        self._fails.pop(ip, None)

    def reset(self) -> None:
        self._fails.clear()
        self._global.clear()


limiter = LoginLimiter()
