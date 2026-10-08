"""Run 종료 Web Push. 실패해도 Run 기록에는 영향을 주지 않는다(호출한 manager 가 예외를 잡는다)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select

from ttakkari import audit
from ttakkari.db import sessionmaker
from ttakkari.models import PushSubscription, Run, RunStatus, utcnow
from ttakkari.push import vapid

log = logging.getLogger(__name__)

TITLES = {
    RunStatus.succeeded: "작업 완료",
    RunStatus.failed: "작업 실패",
    RunStatus.cancelled: "작업 취소됨",
    RunStatus.interrupted: "작업 중단됨",
}
BODY_LIMIT = 80
GONE = {404, 410}


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def build_payload(run: Run, session_title: str | None) -> dict[str, str]:
    """푸시 서버를 거치므로 경로·에러 원문은 넣지 않는다. 성공 때만 결과 첫 줄을 붙인다."""
    parts = [session_title or "새 세션"]
    if run.status == RunStatus.succeeded:
        first = _first_line(run.result_text)
        if first:
            parts.append(first)
    body = " · ".join(parts)
    if len(body) > BODY_LIMIT:
        body = body[: BODY_LIMIT - 1] + "…"
    sid = str(run.session_id)
    return {"title": TITLES.get(run.status, "작업 종료"), "body": body, "url": f"/chat/{sid}", "tag": sid}


def _send_sync(sub: dict[str, Any], payload: str) -> int | None:
    """성공하면 None, 서버가 거절하면 HTTP 상태, 그 밖의 예외는 그대로 올린다."""
    key, claims = vapid.signer()
    try:
        webpush(
            {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=payload, vapid_private_key=key, vapid_claims=dict(claims), ttl=3600, timeout=10,
        )
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status is None:
            raise
        return status
    return None


async def send_to_all(payload: dict[str, str], *, action: str, target_id: Any = None) -> dict[str, int]:
    """모든 구독에 보낸다. 404/410 이면 구독을 지운다. 결과 집계를 돌려준다."""
    async with sessionmaker()() as db:
        subs = [
            {"id": s.id, "endpoint": s.endpoint, "p256dh": s.p256dh, "auth": s.auth}
            for s in await db.scalars(select(PushSubscription))
        ]
    if not subs:
        return {"sent": 0, "removed": 0, "failed": 0}
    data = json.dumps(payload, ensure_ascii=False)

    async def one(sub: dict[str, Any]) -> tuple[uuid.UUID, str]:
        try:
            status = await asyncio.to_thread(_send_sync, sub, data)
        except Exception as e:  # noqa: BLE001 - 한 기기 실패가 다른 기기 발송을 막지 않게 한다.
            log.warning("web push failed: %s", type(e).__name__)
            return sub["id"], "failed"
        if status is None:
            return sub["id"], "sent"
        if status in GONE:
            return sub["id"], "gone"
        log.warning("web push rejected: status=%s", status)
        return sub["id"], "failed"

    results = await asyncio.gather(*(one(s) for s in subs))
    gone = [i for i, r in results if r == "gone"]
    ok = [i for i, r in results if r == "sent"]
    counts = {"sent": len(ok), "removed": len(gone), "failed": len(results) - len(ok) - len(gone)}
    async with sessionmaker()() as db:
        if gone:
            await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
        if ok:
            for s in await db.scalars(select(PushSubscription).where(PushSubscription.id.in_(ok))):
                s.last_success_at = utcnow()
        audit.record(db, actor="system", action=action, outcome="ok" if not counts["failed"] else "error",
                     target_id=target_id, detail=counts)
        await db.commit()
    return counts


async def notify_run_finished(run: Run, session_title: str | None) -> None:
    await send_to_all(build_payload(run, session_title), action="push.run_finished", target_id=run.id)
