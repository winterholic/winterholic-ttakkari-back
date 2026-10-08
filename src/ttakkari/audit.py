from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import AuditLog

log = logging.getLogger("ttakkari.audit")


def _clean(v: Any) -> Any:
    # PostgreSQL JSONB 는 \u0000 을 거부한다. 공격 입력이 감사 기록까지 실패시키지 않게 지운다.
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def record(
    db: AsyncSession,
    *,
    action: str,
    outcome: str = "ok",
    actor: str = "user",
    target_type: str | None = None,
    target_id: Any = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """감사 로그는 호출한 쪽 트랜잭션과 함께 커밋된다."""
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            outcome=outcome,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=_clean(detail or {}),
            ip=ip,
            user_agent=(user_agent or "")[:512] or None,
        )
    )
    log.info("audit action=%s outcome=%s target=%s:%s", action, outcome, target_type, target_id)
