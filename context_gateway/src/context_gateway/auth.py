"""Credential primitives and database-backed authorization for the Gateway."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import APIKey, AuditEvent, KeyACL, Project, Session

KEY_LIFETIME = timedelta(hours=1)
SESSION_LIFETIME = KEY_LIFETIME
SHARE_SESSION_LIFETIME = timedelta(minutes=15)


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
    def __init__(self, status_code: int = 401, error_code: str = "unauthorized") -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(error_code if status_code == 401 else "not found")


def _credential_error(key: APIKey | None, current: datetime) -> AuthenticationError:
    if key is None:
        return AuthenticationError()
    is_v2 = key.credential_type in {"reusable_api_v2", "share_link_v2"}
    if key.revoked_at is not None:
        return AuthenticationError(401, "credential_revoked" if is_v2 else "unauthorized")
    if key.expires_at <= current:
        return AuthenticationError(401, "credential_expired" if is_v2 else "unauthorized")
    return AuthenticationError()


async def exchange_key(
    session: AsyncSession, raw_key: str, *, now: datetime | None = None
) -> tuple[str, datetime]:
    current = now or datetime.now(UTC)
    key_hash = hash_credential(raw_key)
    key = await session.scalar(
        select(APIKey).where(APIKey.credential_hash == key_hash).with_for_update()
    )
    if key is None:
        raise AuthenticationError()
    if key.revoked_at is not None or key.expires_at <= current:
        raise _credential_error(key, current)
    if key.credential_type == "share_link_v2":
        raise AuthenticationError(401, "audience_mismatch")
    consumed = await session.execute(
        update(APIKey)
        .where(
            APIKey.credential_hash == key_hash,
            or_(APIKey.consumed_at.is_(None), APIKey.credential_type != "single_use_v1"),
            APIKey.revoked_at.is_(None),
            APIKey.expires_at > current,
        )
        .values(consumed_at=current, last_used_at=current)
        .returning(APIKey.id, APIKey.expires_at, APIKey.credential_type)
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


async def exchange_share_link(
    session: AsyncSession, raw_key: str, *, now: datetime | None = None
) -> tuple[str, datetime]:
    current = now or datetime.now(UTC)
    key = await session.scalar(
        select(APIKey)
        .where(
            APIKey.credential_hash == hash_credential(raw_key),
            APIKey.credential_type == "share_link_v2",
        )
        .with_for_update()
    )
    if key is None:
        raise AuthenticationError()
    if key.revoked_at is not None or key.expires_at <= current:
        raise _credential_error(key, current)
    acl = await session.scalar(
        select(KeyACL.key_id)
        .join(Project, Project.id == KeyACL.project_id)
        .where(
            KeyACL.key_id == key.id,
            KeyACL.scopes.contains(["read"]),
            Project.sharing_revoked_at.is_(None),
        )
        .limit(1)
    )
    if acl is None:
        raise AuthenticationError()
    session_key = encode_key(secrets.token_bytes(32))
    expires_at = min(key.expires_at, current + SHARE_SESSION_LIFETIME)
    key.last_used_at = current
    session.add(
        Session(
            key_id=key.id,
            credential_hash=hash_credential(session_key),
            expires_at=expires_at,
            window_started_at=current,
            window_count=0,
        )
    )
    session.add(AuditEvent(key_id=key.id, event="share_exchanged"))
    await session.flush()
    return session_key, expires_at


async def authenticate_session(
    session: AsyncSession,
    raw_session: str,
    *,
    project_id: UUID | None,
    required_scope: str,
    rate_limit_per_minute: int,
    allow_share: bool = False,
    now: datetime | None = None,
) -> tuple[Session, list[UUID]]:
    current = now or datetime.now(UTC)
    credential_hash = hash_credential(raw_session)
    record = await session.scalar(
        select(Session).where(Session.credential_hash == credential_hash).with_for_update()
    )
    if record is None:
        raise AuthenticationError()
    key = await session.scalar(select(APIKey).where(APIKey.id == record.key_id).with_for_update())
    if record.revoked_at is not None or record.expires_at <= current:
        if key is not None and key.credential_type in {"reusable_api_v2", "share_link_v2"}:
            code = "credential_revoked" if record.revoked_at is not None else "credential_expired"
            raise AuthenticationError(401, code)
        raise AuthenticationError()
    if key is None or key.revoked_at is not None or key.expires_at <= current:
        raise _credential_error(key, current)
    if key.credential_type == "share_link_v2" and not allow_share:
        raise AuthenticationError(401, "audience_mismatch")
    if key.credential_type != "share_link_v2" and allow_share:
        raise AuthenticationError()
    key.last_used_at = current
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
    if key.window_started_at is None or key.window_started_at <= current - timedelta(minutes=1):
        key.window_started_at = current
        key.window_count = 0
    if key.window_count >= rate_limit_per_minute:
        raise AuthenticationError(429)
    key.window_count += 1
    session.add(
        AuditEvent(key_id=key.id, session_id=record.id, event="api_read", project_id=project_id)
    )
    return record, allowed_projects


async def authenticate_api_key(
    session: AsyncSession,
    raw_key: str,
    *,
    project_id: UUID | None,
    required_scope: str,
    rate_limit_per_minute: int,
    now: datetime | None = None,
) -> tuple[APIKey, list[UUID]]:
    current = now or datetime.now(UTC)
    key = await session.scalar(
        select(APIKey).where(APIKey.credential_hash == hash_credential(raw_key)).with_for_update()
    )
    if key is None:
        raise AuthenticationError()
    if key.revoked_at is not None or key.expires_at <= current:
        raise _credential_error(key, current)
    if key.credential_type == "share_link_v2":
        raise AuthenticationError(401, "audience_mismatch")
    if key.credential_type != "reusable_api_v2":
        raise AuthenticationError()
    rows = await session.execute(
        select(KeyACL.project_id, KeyACL.scopes)
        .join(Project, Project.id == KeyACL.project_id)
        .where(KeyACL.key_id == key.id, Project.sharing_revoked_at.is_(None))
    )
    allowed_projects = [pid for pid, scopes in rows if required_scope in scopes]
    if project_id is not None and project_id not in allowed_projects:
        raise AuthenticationError(404)
    if key.window_started_at is None or key.window_started_at <= current - timedelta(minutes=1):
        key.window_started_at = current
        key.window_count = 0
    if key.window_count >= rate_limit_per_minute:
        raise AuthenticationError(429)
    key.window_count += 1
    key.last_used_at = current
    session.add(AuditEvent(key_id=key.id, event="api_read", project_id=project_id))
    return key, allowed_projects


async def validate_session(
    session: AsyncSession, raw_session: str, *, now: datetime | None = None
) -> bool:
    return await session_validation_error(session, raw_session, now=now) is None


async def session_validation_error(
    session: AsyncSession, raw_session: str, *, now: datetime | None = None
) -> str | None:
    current = now or datetime.now(UTC)
    record = await session.scalar(
        select(Session).where(Session.credential_hash == hash_credential(raw_session))
    )
    if record is None:
        key = await session.scalar(
            select(APIKey).where(APIKey.credential_hash == hash_credential(raw_session))
        )
        if key is None:
            return "unauthorized"
        if key.revoked_at is not None or key.expires_at <= current:
            return _credential_error(key, current).error_code
        if key.credential_type == "share_link_v2":
            return "audience_mismatch"
        if key.credential_type != "reusable_api_v2":
            return "unauthorized"
        active = await session.scalar(
            select(KeyACL.key_id)
            .join(Project, Project.id == KeyACL.project_id)
            .where(KeyACL.key_id == key.id, Project.sharing_revoked_at.is_(None))
            .limit(1)
        )
        return None if active else "credential_revoked"
    if record.revoked_at is not None or record.expires_at <= current:
        key = await session.get(APIKey, record.key_id)
        if key is not None and key.credential_type in {"reusable_api_v2", "share_link_v2"}:
            return "credential_revoked" if record.revoked_at is not None else "credential_expired"
        return "unauthorized"
    key = await session.get(APIKey, record.key_id)
    if key is None:
        return "unauthorized"
    if key.credential_type == "share_link_v2":
        return "audience_mismatch"
    if key.revoked_at is not None or key.expires_at <= current:
        return _credential_error(key, current).error_code
    active = await session.scalar(
        select(KeyACL.key_id)
        .join(Project, Project.id == KeyACL.project_id)
        .where(KeyACL.key_id == key.id, Project.sharing_revoked_at.is_(None))
        .limit(1)
    )
    return (
        None
        if active
        else ("credential_revoked" if key.credential_type != "single_use_v1" else "unauthorized")
    )
