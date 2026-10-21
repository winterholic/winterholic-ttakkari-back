"""조회: 요약 우선·페이지네이션·크기 상한. 응답은 나가기 직전에 한 번 더 마스킹한다."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import OpsEvent, OpsIssue, OpsIssueStat
from ttakkari.oplog import deploy_logs
from ttakkari.oplog.ingest import current_release
from ttakkari.oplog.limits import fit
from ttakkari.oplog.scrub import scrub, scrub_value

STATUSES = ("unresolved", "resolved", "ignored")
SORTS = {"last_seen": OpsIssue.last_seen, "first_seen": OpsIssue.first_seen, "count": OpsIssue.event_count}
MAX_LIMIT = 50
TITLE_CHARS = 120


class NotFound(LookupError):
    pass


def parse_issue_id(value: str | int) -> int:
    m = re.fullmatch(r"(?:LOG-)?(\d+)", str(value).strip(), re.IGNORECASE)
    if not m:
        raise ValueError("이슈 ID는 LOG-12 또는 12 형식이어야 합니다.")
    return int(m[1])


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def _short(sha: str | None) -> str | None:
    return sha[:8] if sha else None


def _page(limit: int | None, cursor: int | None) -> tuple[int, int]:
    return max(1, min(limit or 10, MAX_LIMIT)), max(0, cursor or 0)


def _tag_filter(column, tag: str | None):
    if not tag:
        return None
    key, _, value = tag.partition(":")
    if not key or not value:
        raise ValueError("tag는 key:value 형식이어야 합니다.")
    return column[key].astext == value


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def issue_row(i: OpsIssue) -> dict[str, Any]:
    row = {
        "id": f"LOG-{i.id}", "title": i.title[:TITLE_CHARS], "source": i.source, "level": i.level, "status": i.status,
        "count": i.event_count, "first_seen": _iso(i.first_seen), "last_seen": _iso(i.last_seen),
        "release": _short(i.last_release), "culprit": i.culprit,
    }
    if i.regressed_count:
        row["regressed"] = i.regressed_count
    return row


def _out(payload: dict[str, Any], max_chars: int | None, hint: str = "") -> dict[str, Any]:
    return fit(scrub_value(payload), max_chars, hint=hint)


def _out_page(items: list[dict], total: int, offset: int, max_chars: int | None, hint: str) -> dict:
    # 상한 적용 뒤 실제 반환한 행 수로 커서를 계산해야 덜어 낸 행을 건너뛰지 않는다.
    payload = {"total": total, "items": items, "next_cursor": offset + len(items) if offset + len(items) < total else None}
    while True:
        out = _out(payload, max_chars, hint)
        if "items" not in out or len(out["items"]) == len(payload["items"]):
            return out
        out["next_cursor"] = offset + len(out["items"])
        payload = out


async def list_issues(db: AsyncSession, *, status: str | None = "unresolved", source: str | None = None, level: str | None = None,
                      query: str | None = None, release: str | None = None, tag: str | None = None, since_hours: int | None = None,
                      sort: str = "last_seen", limit: int | None = 10, cursor: int | None = 0, max_chars: int | None = None) -> dict:
    limit, offset = _page(limit, cursor)
    if status and status != "all" and status not in STATUSES:
        raise ValueError("status는 unresolved, resolved, ignored, all 중 하나입니다.")
    if sort not in SORTS:
        raise ValueError("sort는 last_seen, first_seen, count 중 하나입니다.")
    cond = []
    if status and status != "all":
        cond.append(OpsIssue.status == status)
    if source:
        cond.append(OpsIssue.source == source)
    if level:
        cond.append(OpsIssue.level == level)
    if release:
        cond.append(or_(OpsIssue.last_release.startswith(release), OpsIssue.first_release.startswith(release)))
    if since_hours:
        cond.append(OpsIssue.last_seen >= datetime.now(UTC) - timedelta(hours=since_hours))
    if query:
        cond.append(or_(OpsIssue.title.ilike(_like(query)), OpsIssue.culprit.ilike(_like(query))))
    if (t := _tag_filter(OpsIssue.tags, tag)) is not None:
        cond.append(t)
    where = and_(*cond) if cond else True
    total = await db.scalar(select(func.count()).select_from(OpsIssue).where(where))
    rows = list(await db.scalars(select(OpsIssue).where(where).order_by(SORTS[sort].desc(), OpsIssue.id.desc()).offset(offset).limit(limit)))
    return _out_page([issue_row(r) for r in rows], total, offset, max_chars,
                     "cursor=next_cursor 로 다음 쪽을 읽거나 status·source·since_hours 로 좁히세요.")


def _event_full(e: OpsEvent, *, stack_chars: int = 2500, excerpt_chars: int = 2000) -> dict[str, Any]:
    out: dict[str, Any] = {
        "event_id": e.id, "at": _iso(e.occurred_at), "level": e.level, "release": e.release, "message": e.message[:500],
        "tags": e.tags, "ref": e.ref,
    }
    if e.stack:
        out["stack"] = e.stack[-stack_chars:]
    if e.excerpt:
        out["excerpt"] = e.excerpt[:excerpt_chars]
    if e.breadcrumbs:
        out["breadcrumbs"] = e.breadcrumbs[-10:]
    return out


async def issue_detail(db: AsyncSession, issue: str | int, *, events: int = 3, max_chars: int | None = None) -> dict:
    issue_id = parse_issue_id(issue)
    row = await db.get(OpsIssue, issue_id)
    if row is None:
        raise NotFound(f"LOG-{issue_id} 이슈가 없습니다.")
    n = max(0, min(events, 10))
    recent = list(await db.scalars(select(OpsEvent).where(OpsEvent.issue_id == issue_id).order_by(OpsEvent.occurred_at.desc(), OpsEvent.id.desc()).limit(max(1, n))))
    releases = (await db.execute(
        select(OpsEvent.release, func.count()).where(OpsEvent.issue_id == issue_id).group_by(OpsEvent.release)
        .order_by(func.count().desc()).limit(5))).all()
    hours = (await db.execute(
        select(OpsIssueStat.bucket, OpsIssueStat.count).where(OpsIssueStat.issue_id == issue_id)
        .order_by(OpsIssueStat.bucket.desc()).limit(24))).all()
    detail = {
        "issue": {**issue_row(row), "first_release": _short(row.first_release), "resolved_at": _iso(row.resolved_at),
                  "resolved_release": _short(row.resolved_release), "last_regressed_at": _iso(row.last_regressed_at),
                  "tags": row.tags, "notes": row.notes[-5:]},
        "latest_event": _event_full(recent[0]) if recent else None,
        "earlier_events": [{"event_id": e.id, "at": _iso(e.occurred_at), "release": _short(e.release), "message": e.message[:140]}
                           for e in recent[1:n]],
        "releases": [{"release": _short(r), "events": c} for r, c in releases],
        "recent_hours": [{"hour": _iso(b), "count": c} for b, c in reversed(hours)],
    }
    return _out(detail, max_chars, "events 수를 줄이거나 ops_search_logs 로 특정 이벤트만 찾으세요.")


def _snippet(text: str, q: str | None, width: int = 160) -> str:
    text = text.replace("\n", " ⏎ ")
    if q:
        at = text.lower().find(q.lower())
        if at > width // 2:
            start = at - width // 2
            return "…" + text[start:start + width]
    return text[:width]


async def search_events(db: AsyncSession, *, query: str | None = None, since_hours: int | None = 72, until_hours: int | None = None,
                        level: str | None = None, source: str | None = None, release: str | None = None, issue: str | int | None = None,
                        tag: str | None = None, limit: int | None = 10, cursor: int | None = 0, max_chars: int | None = None) -> dict:
    limit, offset = _page(limit, cursor)
    cond = []
    now = datetime.now(UTC)
    if since_hours:
        cond.append(OpsEvent.occurred_at >= now - timedelta(hours=since_hours))
    if until_hours:
        cond.append(OpsEvent.occurred_at <= now - timedelta(hours=until_hours))
    if level:
        cond.append(OpsEvent.level == level)
    if source:
        cond.append(OpsEvent.source == source)
    if release:
        cond.append(OpsEvent.release.startswith(release))
    if issue:
        cond.append(OpsEvent.issue_id == parse_issue_id(issue))
    if query:
        like = _like(query)
        cond.append(or_(OpsEvent.message.ilike(like), OpsEvent.excerpt.ilike(like), OpsEvent.stack.ilike(like)))
    if (t := _tag_filter(OpsEvent.tags, tag)) is not None:
        cond.append(t)
    where = and_(*cond) if cond else True
    total = await db.scalar(select(func.count()).select_from(OpsEvent).where(where))
    rows = list(await db.scalars(select(OpsEvent).where(where).order_by(OpsEvent.occurred_at.desc(), OpsEvent.id.desc()).offset(offset).limit(limit)))
    items = []
    for e in rows:
        body = e.message if (not query or query.lower() in e.message.lower()) else (e.excerpt or e.stack or e.message)
        items.append({"event_id": e.id, "issue": f"LOG-{e.issue_id}", "at": _iso(e.occurred_at), "level": e.level, "source": e.source,
                      "release": _short(e.release), "text": _snippet(body, query), "ref": e.ref})
    return _out_page(items, total, offset, max_chars,
                     "cursor=next_cursor 로 다음 쪽을, ops_get_issue 로 이슈 상세를 읽으세요.")


async def trend(db: AsyncSession, *, hours: int = 168, unit: str = "hour", issue: str | int | None = None) -> dict:
    hours = max(1, min(hours, 24 * 30))
    if unit not in {"hour", "day"}:
        raise ValueError("unit은 hour 또는 day 입니다.")
    now = datetime.now(UTC)
    since = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    bucket = func.date_trunc(unit, func.timezone("UTC", OpsIssueStat.bucket))
    q = select(bucket, func.sum(OpsIssueStat.count)).where(OpsIssueStat.bucket >= since).group_by(bucket).order_by(bucket)
    if issue:
        q = q.where(OpsIssueStat.issue_id == parse_issue_id(issue))
    found = {b.replace(tzinfo=UTC): int(c) for b, c in (await db.execute(q)).all()}
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    cur = since if unit == "hour" else since.replace(hour=0)
    points = []
    while cur <= now:
        points.append({"at": _iso(cur), "count": found.get(cur, 0)})
        cur += step
    return {"unit": unit, "hours": hours, "points": points, "total": sum(p["count"] for p in points)}


async def update_issue(db: AsyncSession, issue: str | int, *, status: str | None = None, note: str | None = None,
                       release: str | None = None, actor: str = "user") -> dict:
    row = await db.get(OpsIssue, parse_issue_id(issue), with_for_update=True)
    if row is None:
        raise NotFound("이슈가 없습니다.")
    if status is not None:
        if status not in STATUSES:
            raise ValueError("status는 unresolved, resolved, ignored 중 하나입니다.")
        row.status = status
        if status == "resolved":
            row.resolved_at = datetime.now(UTC)
            row.resolved_release = release or current_release()
        elif status == "unresolved":
            row.resolved_at = row.resolved_release = None
    if note:
        row.notes = [*row.notes[-49:], {"at": _iso(datetime.now(UTC)), "by": actor, "text": scrub(note.strip())[:500]}]
    await db.commit()
    return {"issue": issue_row(row), "notes": row.notes[-3:]}


async def deploys(db: AsyncSession, *, limit: int | None = 10, status: str | None = None, max_chars: int | None = None) -> dict:
    limit, _ = _page(limit, 0)
    jobs = deploy_logs.list_jobs()
    if status:
        jobs = [j for j in jobs if j.get("status") == status]
    jobs = list(reversed(jobs))[:limit]
    refs = {f"deploy_job:{j['id']}" for j in jobs}
    linked: dict[str, tuple[int, str, dict]] = {}
    if refs:
        for e in await db.scalars(select(OpsEvent).where(OpsEvent.ref.in_(refs)).order_by(OpsEvent.occurred_at)):
            linked[e.ref] = (e.issue_id, e.message, e.tags)
    items = []
    for j in jobs:
        hit = linked.get(f"deploy_job:{j['id']}")
        item = {"job_id": j["id"], "revision": _short(j.get("revision")), "status": j.get("status"), "created_at": j.get("created_at"),
                "updated_at": j.get("updated_at"), "strategy": j.get("strategy")}
        if hit:
            item.update({"issue": f"LOG-{hit[0]}", "cause": hit[1][:200], "stage": hit[2].get("stage")})
        elif j.get("error"):
            item["error"] = j["error"][:200]
        item.update(deploy_logs.wait_state(j))
        items.append(item)
    return _out({"items": items}, max_chars, "limit 을 줄이거나 status 로 거르세요. 로그는 ops_deploy_log 로 읽습니다.")


def deploy_log(ref: str, mode: str = "summary", lines: int = 60, offset: int | None = None, max_chars: int | None = None) -> dict:
    job = deploy_logs.resolve_job(ref)
    if job is None:
        raise NotFound("해당 배포 job을 찾지 못했습니다. job id·SHA 접두사·latest·latest_failed 를 쓰세요.")
    return _out(deploy_logs.view(job, mode, lines=lines, offset=offset), max_chars,
                "mode=range, offset=<줄 번호>, lines 로 필요한 구간만 읽으세요.")
