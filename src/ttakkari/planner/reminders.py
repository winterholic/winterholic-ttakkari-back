"""일정 알림(ADR 0008). 기본은 꺼짐이고, 사용자가 일정마다 하나를 고른다.

예약은 따로 두지 않고 `remind_at`(보낼 시각)을 저장한 뒤 서버가 주기적으로 훑는다(lifespan 의 _reminder_loop).
- 중복 방지: 보낼 일정을 `UPDATE … SET reminded_at = now WHERE reminded_at IS NULL … RETURNING` 으로 먼저 차지하고
  커밋한 다음 보낸다. 같은 일정을 두 번 차지할 수 없어서 재시작·겹친 스캔에도 한 번만 나간다(최대 한 번).
- 누락 방지: 서버가 꺼져 있던 사이에 지난 알림은 다시 떴을 때 늦게라도 보낸다. 단 일정 시작(또는 알림 시각) 뒤
  LATE_LIMIT 가 지났으면 쓸모가 없어 보내지 않고 건너뛴 것으로 표시한다.
- 남는 틈: 차지를 커밋한 직후 보내기 전에 프로세스가 죽으면 그 알림은 빠진다(중복보다 누락을 택했다).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from ttakkari.db import sessionmaker
from ttakkari.models import CalendarEvent, utcnow
from ttakkari.planner.service import KST, bad, fmt_local
from ttakkari.push.service import PushKind, notify

log = logging.getLogger(__name__)

# 시작 몇 분 전에 보낼지. 종일 일정의 시작은 그날 0시라 "당일 오전 9시"는 -9시간(시작 뒤)이다.
TIMED = {"at_start": 0, "10m": 10, "30m": 30, "1h": 60, "1d": 24 * 60}
ALL_DAY = {"day_9am": -9 * 60, "prev_day_9am": 15 * 60}
LABELS = {"at_start": "정시", "10m": "10분 전", "30m": "30분 전", "1h": "1시간 전", "1d": "하루 전",
          "day_9am": "당일 오전 9시", "prev_day_9am": "전날 오전 9시"}
LATE_LIMIT = timedelta(hours=1)
WEEKDAYS = "월화수목금토일"


def check(reminder: str | None, all_day: bool) -> None:
    if reminder is None:
        return
    allowed = ALL_DAY if all_day else TIMED
    if reminder not in allowed:
        kind = "종일" if all_day else "시간"
        raise bad(f"{kind} 일정의 알림은 {', '.join(allowed)} 중 하나예요.")


def schedule(ev: CalendarEvent, now: datetime | None = None) -> None:
    """알림 시각을 다시 계산하고 '아직 안 보냄'으로 되돌린다. 이미 시작한 일정의 지난 알림은 보내지 않는다."""
    now = now or utcnow()
    if not ev.reminder:
        ev.remind_at = ev.reminded_at = None
        return
    minutes = (ALL_DAY if ev.all_day else TIMED)[ev.reminder]
    ev.remind_at = ev.start_at - timedelta(minutes=minutes)
    ev.reminded_at = now if ev.remind_at <= now and ev.start_at <= now else None


def message(ev: CalendarEvent, now: datetime) -> tuple[str, str]:
    start = ev.start_at.astimezone(KST)
    day = f"{start.month}월 {start.day}일({WEEKDAYS[start.weekday()]})"
    if ev.all_day:
        days = (start.date() - now.astimezone(KST).date()).days
        when = {0: "오늘", 1: "내일"}.get(days, day) + " 종일"
    else:
        mins = round((ev.start_at - now).total_seconds() / 60)
        rel = "지금" if abs(mins) < 1 else f"{mins}분 후" if 0 < mins < 60 else f"{mins // 60}시간 후" if 0 < mins < 24 * 60 \
            else f"{-mins}분 지남" if mins < 0 else ""
        when = f"{day} {start:%H:%M}" + (f" · {rel}" if rel else "")
    body = when + (f" · {ev.location}" if ev.location else "")
    return ev.title, body


def url_for(ev: CalendarEvent) -> str:
    return f"/calendar?event={ev.id}&date={fmt_local(ev.start_at, True)}"


async def send_due(now: datetime | None = None) -> int:
    """때가 된 알림을 차지한 뒤 보낸다. 보낸(보내려 한) 개수를 돌려준다."""
    now = now or utcnow()
    due = (CalendarEvent.reminded_at.is_(None), CalendarEvent.deleted_at.is_(None), CalendarEvent.remind_at <= now)
    latest = func.greatest(CalendarEvent.start_at, CalendarEvent.remind_at)
    # 알림 차지는 일정 수정이 아니라서 updated_at 을 그대로 둔다(onupdate 를 막는다).
    mark = {"reminded_at": now, "updated_at": CalendarEvent.updated_at}
    async with sessionmaker()() as db:
        await db.execute(update(CalendarEvent).where(*due, latest <= now - LATE_LIMIT).values(**mark))
        ids = list(await db.scalars(update(CalendarEvent).where(*due, latest > now - LATE_LIMIT).values(**mark)
                                    .returning(CalendarEvent.id)))
        await db.commit()
        events = list(await db.scalars(select(CalendarEvent).where(CalendarEvent.id.in_(ids)))) if ids else []
    for ev in events:
        title, body = message(ev, now)
        try:
            await notify(PushKind.calendar_reminder, title, body, url=url_for(ev), tag=f"event-{ev.id}", target_id=ev.id)
        except Exception as e:  # noqa: BLE001 - 한 일정의 발송 실패가 다른 일정을 막지 않게 한다.
            log.warning("calendar reminder failed: %s", type(e).__name__)
    return len(events)

