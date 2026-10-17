"""이벤트 기록: 정규화 → 마스킹 → 이슈 upsert → 회귀 판정 → 시간별 통계 → 샘플링 한도."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import OpsEvent, OpsIssue, OpsIssueStat
from ttakkari.oplog.normalize import level_rank
from ttakkari.oplog.scrub import scrub, scrub_value

EVENTS_PER_ISSUE_PER_HOUR = 30
MAX_TITLE = 200
MAX_MESSAGE = 2000
MAX_STACK = 8000
MAX_EXCERPT = 6000
MAX_TAGS = 20
MAX_TAG_VALUE = 200
MAX_BREADCRUMBS = 20
MAX_BREADCRUMB_TEXT = 200


@dataclass
class EventIn:
    source: str
    level: str
    message: str
    fingerprint: str
    occurred_at: datetime
    title: str | None = None
    culprit: str | None = None
    release: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    breadcrumbs: list[Any] = field(default_factory=list)
    stack: str | None = None
    excerpt: str | None = None
    ref: str | None = None
    dedupe_key: str | None = None


@dataclass
class Recorded:
    issue_id: int
    stored: bool
    regressed: bool
    created: bool


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = scrub(value).replace("\x00", "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def clean_tags(tags: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in list(scrub_value(tags).items())[:MAX_TAGS]:
        out[str(k)[:40]] = v if isinstance(v, bool | int | float) or v is None else str(v)[:MAX_TAG_VALUE]
    return out


def clean_breadcrumbs(items: list[Any]) -> list[Any]:
    out = []
    for item in items[-MAX_BREADCRUMBS:]:
        if isinstance(item, dict):
            item = {**item, **({"message": str(item["message"])} if "message" in item else {})}
        item = scrub_value(item)
        if isinstance(item, dict):
            item = {k: (v[:MAX_BREADCRUMB_TEXT] if isinstance(v, str) else v) for k, v in item.items()}
        else:
            item = str(item)[:MAX_BREADCRUMB_TEXT]
        out.append(item)
    return out


async def record(db: AsyncSession, ev: EventIn) -> Recorded | None:
    """호출자가 커밋한다. 같은 dedupe_key 가 이미 있으면 아무것도 바꾸지 않고 None 을 돌려준다."""
    if level_rank(ev.level) < level_rank("warning"):
        return None
    if ev.dedupe_key and await db.scalar(select(OpsEvent.id).where(OpsEvent.dedupe_key == ev.dedupe_key)):
        return None
    when = _utc(ev.occurred_at)
    level = ev.level if ev.level in {"warning", "error", "fatal"} else "error"
    message = _clip(ev.message, MAX_MESSAGE) or ""
    first_line = ev.message.strip().splitlines()[0] if ev.message.strip() else "(empty)"
    title = _clip(ev.title or first_line, MAX_TITLE)
    tags = clean_tags(ev.tags)
    try:
        async with db.begin_nested():
            created = False
            issue = await db.scalar(select(OpsIssue).where(OpsIssue.fingerprint == ev.fingerprint).with_for_update())
            if issue is None:
                await db.execute(insert(OpsIssue).values(
                    fingerprint=ev.fingerprint, title=title, culprit=_clip(ev.culprit, 200), source=ev.source,
                    level=level, status="unresolved", first_seen=when, last_seen=when, event_count=0,
                    first_release=ev.release, last_release=ev.release, regressed_count=0, notes=[], tags={},
                ).on_conflict_do_nothing(index_elements=["fingerprint"]))
                issue = await db.scalar(select(OpsIssue).where(OpsIssue.fingerprint == ev.fingerprint).with_for_update())
                created = True
            regressed = False
            if issue.status == "resolved" and issue.resolved_at is not None and when > issue.resolved_at:
                issue.status = "unresolved"
                issue.regressed_count += 1
                issue.last_regressed_at = when
                tags["regression"] = True
                regressed = True
            issue.event_count += 1
            if when < _utc(issue.first_seen):
                issue.first_seen = when
                issue.first_release = ev.release or issue.first_release
            if when >= _utc(issue.last_seen):
                issue.last_seen = when
                issue.last_release = ev.release or issue.last_release
                issue.tags = tags
            if issue.first_release is None:
                issue.first_release = ev.release
            if level_rank(level) > level_rank(issue.level):
                issue.level = level
            bucket = when.replace(minute=0, second=0, microsecond=0)
            count = await db.scalar(
                insert(OpsIssueStat).values(issue_id=issue.id, bucket=bucket, count=1)
                .on_conflict_do_update(index_elements=["issue_id", "bucket"], set_={"count": OpsIssueStat.count + 1})
                .returning(OpsIssueStat.count))
            stored = count <= EVENTS_PER_ISSUE_PER_HOUR
            if stored:
                db.add(OpsEvent(
                    issue_id=issue.id, occurred_at=when, source=ev.source, level=level, message=message,
                    release=ev.release, tags=tags, breadcrumbs=clean_breadcrumbs(ev.breadcrumbs),
                    stack=_clip(ev.stack, MAX_STACK), excerpt=_clip(ev.excerpt, MAX_EXCERPT), ref=_clip(ev.ref, 200),
                    dedupe_key=ev.dedupe_key))
            await db.flush()
    except IntegrityError:
        return None
    return Recorded(issue_id=issue.id, stored=stored, regressed=regressed, created=created)


async def advisory_lock(db: AsyncSession, name: str) -> bool:
    """트랜잭션 동안 유지되는 잠금. 여러 세대가 같은 파일을 동시에 읽지 않게 한다."""
    return bool(await db.scalar(text("select pg_try_advisory_xact_lock(hashtext(:n))"), {"n": f"ttakkari-oplog-{name}"}))
