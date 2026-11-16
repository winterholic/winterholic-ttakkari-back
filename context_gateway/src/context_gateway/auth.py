"""Credential primitives and database-backed authorization for the Gateway."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import APIKey, AuditEvent, KeyACL, Project, Session

KEY_LIFETIME = timedelta(hours=1)
SESSION_LIFETIME = KEY_LIFETIME


def encode_key(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_key(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid credential") from exc


def hash_credential(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def credentials_match(value: str, expected_hash: str) -> bool:
    try:
        actual = hash_credential(value)
    except (UnicodeError, ValueError):
        actual = hashlib.sha256(b"invalid credential").hexdigest()
    return hmac.compare_digest(actual, expected_hash)


def issue_key(*, now: datetime | None = None) -> tuple[str, str, datetime]:
    issued_at = now or datetime.now(UTC)
    raw_key = encode_key(secrets.token_bytes(32))
    return raw_key, hash_credential(raw_key), issued_at + KEY_LIFETIME


class AuthenticationError(Exception):
    def __init__(self, status_code: int = 401) -> None:
        self.status_code = status_code
        super().__init__("unauthorized" if status_code == 401 else "not found")


async def exchange_key(
    session: AsyncSession, raw_key: str, *, now: datetime | None = None
) -> tuple[str, datetime]:
    current = now or datetime.now(UTC)
    key_hash = hash_credential(raw_key)
    consumed = await session.execute(
        update(APIKey)
        .where(
            APIKey.credential_hash == key_hash,
            APIKey.consumed_at.is_(None),
            APIKey.revoked_at.is_(None),
            APIKey.expires_at > current,
        )
        .values(consumed_at=current)
        .returning(APIKey.id, APIKey.expires_at)
    )
    record = consumed.first()
    if record is None:
        raise AuthenticationError()
    has_active_grant = await session.scalar(
        select(KeyACL.key_id)
        .join(Project, Project.id == KeyACL.project_id)
        .where(KeyACL.key_id == record.id, Project.sharing_revoked_at.is_(None))
        .limit(1)
    )
    if has_active_grant is None:
        raise AuthenticationError()
    session_key = encode_key(secrets.token_bytes(32))
    expires_at = min(record.expires_at, current + SESSION_LIFETIME)
    session.add(
        Session(
            key_id=record.id,
            credential_hash=hash_credential(session_key),
            expires_at=expires_at,
            window_started_at=current,
            window_count=0,
        )
    )
    session.add(AuditEvent(key_id=record.id, event="key_exchanged"))
    await session.flush()
    return session_key, expires_at


async def authenticate_session(
    session: AsyncSession,
    raw_session: str,
    *,
    project_id: UUID | None,
    required_scope: str,
    rate_limit_per_minute: int,
    now: datetime | None = None,
) -> tuple[Session, list[UUID]]:
    current = now or datetime.now(UTC)
    credential_hash = hash_credential(raw_session)
    record = await session.scalar(
        select(Session).where(Session.credential_hash == credential_hash).with_for_update()
    )
    if record is None or record.revoked_at is not None or record.expires_at <= current:
        raise AuthenticationError()
    key = await session.get(APIKey, record.key_id)
    if key is None or key.revoked_at is not None or key.expires_at <= current:
        raise AuthenticationError()
    acl_rows = await session.execute(
        select(KeyACL.project_id, KeyACL.scopes)
        .join(Project, Project.id == KeyACL.project_id)
        .where(KeyACL.key_id == key.id, Project.sharing_revoked_at.is_(None))
    )
    allowed_projects: list[UUID] = []
    allowed = False
    for acl_project_id, scopes in acl_rows:
        if required_scope in scopes:
            allowed_projects.append(acl_project_id)
            allowed = allowed or acl_project_id == project_id
    if project_id is not None and not allowed:
        raise AuthenticationError(404)
    if record.window_started_at <= current - timedelta(minutes=1):
        record.window_started_at = current
        record.window_count = 0
    if record.window_count >= rate_limit_per_minute:
        raise AuthenticationError(429)
    record.window_count += 1
    session.add(
        AuditEvent(key_id=key.id, session_id=record.id, event="api_read", project_id=project_id)
    )
    return record, allowed_projects


async def validate_session(
    session: AsyncSession, raw_session: str, *, now: datetime | None = None
) -> bool:
    current = now or datetime.now(UTC)
    record = await session.scalar(
        select(Session).where(Session.credential_hash == hash_credential(raw_session))
    )
    if record is None or record.revoked_at is not None or record.expires_at <= current:
        return False
    key = await session.get(APIKey, record.key_id)
    if key is None or key.revoked_at is not None or key.expires_at <= current:
        return False
    return bool(
        await session.scalar(
            select(KeyACL.key_id)
            .join(Project, Project.id == KeyACL.project_id)
            .where(KeyACL.key_id == key.id, Project.sharing_revoked_at.is_(None))
            .limit(1)
        )
    )
