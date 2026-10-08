from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import Run, RunEvent

_signals: dict[uuid.UUID, asyncio.Event] = {}


def signal_for(run_id: uuid.UUID) -> asyncio.Event:
    ev = _signals.get(run_id)
    if ev is None:
        ev = _signals[run_id] = asyncio.Event()
    return ev


def notify(run_id: uuid.UUID) -> None:
    """대기 중인 SSE 구독자를 깨운다. 구독자는 DB 에서 이어서 읽으므로 신호 자체는 내용이 없다."""
    ev = _signals.pop(run_id, None)
    if ev is not None:
        ev.set()


def append(db: AsyncSession, run: Run, type_: str, payload: dict[str, Any] | None = None) -> RunEvent:
    # Run 하나의 이벤트는 그 Run 을 실행하는 태스크 하나만 쓴다. 그래서 seq 증가에 잠금이 필요 없다.
    run.last_seq += 1
    ev = RunEvent(run_id=run.id, seq=run.last_seq, type=type_, payload=payload or {})
    db.add(ev)
    return ev
