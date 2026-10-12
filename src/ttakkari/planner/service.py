"""메모·일정 공통 로직(ADR 0008). 사용자 API 와 에이전트 API 가 같이 쓴다."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import rrule as du_rrule
from fastapi import HTTPException, status
from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import CalendarEvent, Memo

KST = ZoneInfo("Asia/Seoul")
MAX_SPAN_DAYS = 400
MAX_OCCURRENCES = 500
ALLOWED_FREQ = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
EXCERPT = 160


def bad(message: str, code: str = "validation", http: int = status.HTTP_422_UNPROCESSABLE_CONTENT) -> HTTPException:
    return HTTPException(http, {"code": code, "message": message})


def like(q: str) -> str:
    return f"%{q.replace(chr(92), chr(92) * 2).replace('%', r'\%').replace('_', r'\_')}%"


# ---- 메모

def clean_tags(tags: Iterable[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = t.strip().lstrip("#").strip()
        if t and len(t) <= 30 and t not in out:
            out.append(t)
    if len(out) > 20:
        raise bad("태그는 20개까지예요.")
    return out


def memo_out(m: Memo, *, body: bool = True) -> dict[str, Any]:
    text = m.body or ""
    d: dict[str, Any] = {"id": str(m.id), "title": m.title, "tags": m.tags, "pinned": m.pinned, "source": m.source,
                         "rev": m.rev, "excerpt": re.sub(r"\s+", " ", text[:EXCERPT * 2]).strip()[:EXCERPT],
                         "created_at": m.created_at.isoformat(), "updated_at": m.updated_at.isoformat()}
    if body:
        d["body"] = text
    return d


def memo_query(q: str | None = None, tag: str | None = None, pinned: bool | None = None):
    stmt = select(Memo).where(Memo.deleted_at.is_(None))
    if tag:
        stmt = stmt.where(Memo.tags.contains([tag]))
    if pinned is not None:
        stmt = stmt.where(Memo.pinned.is_(pinned))
    if q:
        lk = like(q.strip())
        stmt = stmt.where(or_(Memo.title.ilike(lk), Memo.body.ilike(lk), cast(Memo.tags, String).ilike(lk)))
    return stmt.order_by(Memo.pinned.desc(), Memo.updated_at.desc())


async def get_memo(db: AsyncSession, memo_id: Any, *, include_deleted: bool = False) -> Memo:
    m = await db.get(Memo, memo_id)
    if m is None or (m.deleted_at is not None and not include_deleted):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "메모가 없어요."})
    return m


def check_rev(m: Memo, rev: int | None) -> None:
    """저장하려는 쪽이 본 버전이 낡았으면 덮어쓰지 않는다."""
    if rev is not None and rev != m.rev:
        raise bad("다른 곳에서 먼저 수정됐어요. 새로 불러온 뒤 다시 저장하세요.", "conflict", status.HTTP_409_CONFLICT)


# ---- 일정 시각

def parse_local(value: str, all_day: bool) -> datetime:
    """KST 벽시계 문자열을 KST 시각으로. 종일은 날짜, 시간 일정은 날짜+시각(오프셋이 있으면 KST 로 환산)."""
    try:
        if all_day:
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=KST)
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise bad("날짜 형식이 올바르지 않아요. 종일은 YYYY-MM-DD, 시간은 YYYY-MM-DDTHH:MM 이에요.") from e
    return dt.astimezone(KST) if dt.tzinfo else dt.replace(tzinfo=KST)


def fmt_local(dt: datetime, all_day: bool) -> str:
    local = dt.astimezone(KST)
    return local.date().isoformat() if all_day else local.strftime("%Y-%m-%dT%H:%M")


def to_range(start: str, end: str, all_day: bool) -> tuple[datetime, datetime]:
    """API 의 start/end 를 저장 값으로. 종일의 end 는 그날을 포함하므로 하루를 더해 끝 제외로 저장한다."""
    s, e = parse_local(start, all_day), parse_local(end, all_day)
    if all_day:
        e += timedelta(days=1)
    if e < s:
        raise bad("끝이 시작보다 빠를 수 없어요.")
    return s, e


def span(ev: CalendarEvent) -> tuple[str, str]:
    s = fmt_local(ev.start_at, ev.all_day)
    e = fmt_local(ev.end_at - timedelta(days=1) if ev.all_day else ev.end_at, ev.all_day)
    return s, e


# ---- 반복(RRULE)

_UNTIL = re.compile(r"UNTIL=(\d{8})(T(\d{6})(Z?))?", re.IGNORECASE)


def normalize_rrule(rule: str, dtstart: datetime) -> str:
    """RRULE 문자열을 검사하고 정규화한다. UNTIL 은 KST 벽시계(Z 없는 값)로 맞춘다."""
    r = rule.strip()
    if r.upper().startswith("RRULE:"):
        r = r[6:]
    if not r or "\n" in r or "DTSTART" in r.upper():
        raise bad("반복 규칙이 올바르지 않아요.")

    def fix_until(m: re.Match[str]) -> str:
        day, _, clock, z = m.groups()
        if clock is None:
            return f"UNTIL={day}T235959"
        if z:
            utc = datetime.strptime(day + clock, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("UTC"))
            return "UNTIL=" + utc.astimezone(KST).strftime("%Y%m%dT%H%M%S")
        return f"UNTIL={day}T{clock}"

    r = _UNTIL.sub(fix_until, r)
    try:
        parsed = du_rrule.rrulestr(r, dtstart=dtstart.replace(tzinfo=None))
    except (ValueError, TypeError, KeyError) as e:
        raise bad("반복 규칙을 해석하지 못했어요.") from e
    if not isinstance(parsed, du_rrule.rrule) or du_rrule.FREQNAMES[parsed._freq] not in ALLOWED_FREQ:
        raise bad("반복은 매일·매주·매월·매년만 쓸 수 있어요.")
    if parsed.after(dtstart.replace(tzinfo=None), inc=True) is None:
        raise bad("이 규칙으로는 일정이 한 번도 생기지 않아요.")
    return r


def occurrences(ev: CalendarEvent, lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    """[lo, hi) 와 겹치는 발생 구간(KST)."""
    start, end = ev.start_at.astimezone(KST), ev.end_at.astimezone(KST)
    if not ev.rrule:
        return [(start, end)] if start < hi and end > lo else []
    dur = end - start
    rule = du_rrule.rrulestr(ev.rrule, dtstart=start.replace(tzinfo=None))
    out: list[tuple[datetime, datetime]] = []
    for n in rule.between(lo.replace(tzinfo=None) - dur, hi.replace(tzinfo=None), inc=False):
        s = n.replace(tzinfo=KST)
        out.append((s, s + dur))
        if len(out) >= MAX_OCCURRENCES:
            break
    return out


def event_out(ev: CalendarEvent, occ: tuple[datetime, datetime] | None = None) -> dict[str, Any]:
    """occ 가 있으면 start/end 는 그 발생 구간이다. 없으면 일정(시리즈) 자체의 구간이다."""
    s, e = (occ[0], occ[1]) if occ else (ev.start_at, ev.end_at)
    end_shown = e - timedelta(days=1) if ev.all_day else e
    return {"id": str(ev.id), "title": ev.title, "description": ev.description, "location": ev.location,
            "all_day": ev.all_day, "start": fmt_local(s, ev.all_day), "end": fmt_local(end_shown, ev.all_day),
            "rrule": ev.rrule, "recurring": bool(ev.rrule), "source": ev.source,
            "created_at": ev.created_at.isoformat(), "updated_at": ev.updated_at.isoformat()}


def window(frm: date, to: date) -> tuple[datetime, datetime]:
    if to < frm:
        raise bad("to 가 from 보다 빠를 수 없어요.")
    if (to - frm).days > MAX_SPAN_DAYS:
        raise bad(f"한 번에 {MAX_SPAN_DAYS}일까지 조회할 수 있어요.")
    return (datetime.combine(frm, time.min, tzinfo=KST), datetime.combine(to + timedelta(days=1), time.min, tzinfo=KST))


async def list_events(db: AsyncSession, frm: date, to: date, q: str | None = None) -> list[dict[str, Any]]:
    lo, hi = window(frm, to)
    stmt = select(CalendarEvent).where(CalendarEvent.deleted_at.is_(None), CalendarEvent.start_at < hi)
    # 반복 일정은 시작이 구간 앞이어도 구간 안에서 생길 수 있어 끝 조건을 걸지 않는다.
    stmt = stmt.where(or_(CalendarEvent.rrule.is_not(None), CalendarEvent.end_at > lo))
    if q:
        lk = like(q.strip())
        stmt = stmt.where(or_(CalendarEvent.title.ilike(lk), CalendarEvent.description.ilike(lk),
                              CalendarEvent.location.ilike(lk)))
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for ev in await db.scalars(stmt):
        for occ in occurrences(ev, lo, hi):
            rows.append((occ[0], event_out(ev, occ)))
    rows.sort(key=lambda r: (r[0], r[1]["title"]))
    return [r[1] for r in rows]


async def get_event(db: AsyncSession, event_id: Any, *, include_deleted: bool = False) -> CalendarEvent:
    ev = await db.get(CalendarEvent, event_id)
    if ev is None or (ev.deleted_at is not None and not include_deleted):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "일정이 없어요."})
    return ev


def apply_event_fields(ev: CalendarEvent, fields: dict[str, Any]) -> None:
    """검증을 거쳐 일정에 반영한다. fields 는 지정된 것만 담는다(PATCH). 종일 전환은 start/end 를 같이 받는다."""
    all_day = fields.get("all_day", ev.all_day)
    if "all_day" in fields and fields["all_day"] != ev.all_day and not ("start" in fields and "end" in fields):
        raise bad("종일 여부를 바꿀 때는 시작과 끝을 함께 보내 주세요.")
    if "start" in fields or "end" in fields or "all_day" in fields:
        cur_s, cur_e = span(ev)
        ev.start_at, ev.end_at = to_range(fields.get("start", cur_s), fields.get("end", cur_e), all_day)
        ev.all_day = all_day
    for k in ("title", "description", "location"):
        if k in fields:
            setattr(ev, k, fields[k].strip() if k == "title" else fields[k])
    if "rrule" in fields:
        ev.rrule = normalize_rrule(fields["rrule"], ev.start_at) if fields["rrule"] else None
    elif ev.rrule:
        # 시작이 옮겨졌을 수 있으니 규칙을 새 시작에 맞춰 다시 검사한다.
        ev.rrule = normalize_rrule(ev.rrule, ev.start_at)
