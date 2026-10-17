"""수집: 배포 job·Run 실패/폴백(DB)·프로세스 로그 파일을 이벤트로 바꿔 기록한다. 모든 경로가 멱등이다."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import traceback as tb
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import OpsEvent, OpsIngestCursor, Run, RunEvent
from ttakkari.oplog import deploy_logs
from ttakkari.oplog.normalize import (
    exception_fingerprint,
    fingerprint,
    normalize_message,
    parse_traceback,
)
from ttakkari.oplog.store import EventIn, advisory_lock, record

log = logging.getLogger("ttakkari.oplog")

BACKFILL_DAYS = 14
FILE_BACKFILL_BYTES = 2 * 1024 * 1024
FILE_CHUNK_BYTES = 1024 * 1024
MAX_BLOCK_LINES = 60
FRESH_SECONDS = 20
_BREADCRUMB_TYPES = ("run.status", "run.waiting", "run.resumed", "engine.fallback", "account.limited", "worker.routing", "checkpoint", "log")

_TS = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?)\s+(?P<level>[A-Z]+)\b")
_UVICORN = re.compile(r"^(?P<level>CRITICAL|ERROR|WARNING|INFO|DEBUG):\s+")
_START = re.compile(r"^(?:Traceback \(most recent call last\):|(?:\d{4}-\d\d-\d\d[ T][\d:.,]+\s+)?(?:ERROR|CRITICAL|FATAL)\b[: ]|(?:ERROR|CRITICAL):\s)")
_SKIP_ASGI = re.compile(r"Exception in ASGI application")
_SKIP_DEPLOYER = re.compile(r"deployment [0-9a-f-]{36} failed|generation deployment failed")
_EXC_TAIL = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Timeout|Failure)\b")

_last_sync = 0.0
_release_cache: str | None = None


def current_release() -> str | None:
    global _release_cache
    if _release_cache is None:
        try:
            from ttakkari.deployment import revision

            _release_cache = revision()
        except Exception:  # noqa: BLE001 -- 릴리스 태그는 있으면 좋은 정보다
            _release_cache = ""
    return _release_cache or None


_LONG_SEGMENT = re.compile(r"/[A-Za-z0-9_\-]{24,}")


def safe_path(path: str) -> str:
    """경로에 박힌 공유 토큰·ID 같은 긴 조각을 가려 태그에 남긴다."""
    return normalize_message(_LONG_SEGMENT.sub("/<id>", path), 120)


def _parse_iso(value: str | None) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if value else datetime.now(UTC)
    except ValueError:
        parsed = datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def release_at(jobs: list[dict], when: datetime) -> str | None:
    """그 시각에 운영 중이던 배포 SHA. 성공(또는 draining)한 마지막 배포로 추정한다."""
    best = None
    for job in jobs:
        if job.get("status") in {"succeeded", "draining"} and _parse_iso(job.get("updated_at")) <= when:
            best = job.get("activated_revision") or job.get("revision")
    return best


async def _cursor(db, key: str) -> OpsIngestCursor | None:
    return await db.get(OpsIngestCursor, key)


async def _save_cursor(db, key: str, position: int, inode: int | None = None) -> None:
    row = await _cursor(db, key)
    if row is None:
        db.add(OpsIngestCursor(key=key, position=position, inode=inode, updated_at=datetime.now(UTC)))
    else:
        row.position, row.inode, row.updated_at = position, inode, datetime.now(UTC)


# ---- 배포 job

async def sync_deploy_jobs(db) -> int:
    n = 0
    seen = set(await db.scalars(select(OpsEvent.dedupe_key).where(OpsEvent.dedupe_key.like("deploy:%"))))
    jobs = deploy_logs.list_jobs()
    for job in jobs:
        if job.get("status") not in deploy_logs.FAILED_STATUSES:
            continue
        error_hash = hashlib.sha1((job.get("error") or "").encode()).hexdigest()[:8]
        key = f"deploy:{job['id']}:{job['status']}:{error_hash}"
        if key in seen:
            continue
        lines, _ = deploy_logs.read_log(job["id"])
        stage, sig, message = deploy_logs.headline(job, lines)
        sections = deploy_logs.error_sections(lines, max_sections=3)
        crumbs = [{"ts": job.get("created_at"), "message": f"requested revision {str(job.get('revision'))[:10]} "
                   f"(strategy={job.get('strategy')}, skip_tests={job.get('skip_tests')})"}]
        for field in ("previous_revision", "old_pid", "candidate_slot", "activated_revision"):
            if job.get(field) is not None:
                crumbs.append({"message": f"{field}={job[field]}"})
        crumbs.append({"ts": job.get("updated_at"), "message": f"status {job['status']}"})
        result = await record(db, EventIn(
            source="deploy", level="error", message=message, title=message,
            fingerprint=fingerprint("deploy", stage, normalize_message(sig)),
            culprit=f"deploy:{stage}", occurred_at=_parse_iso(job.get("updated_at")), release=job.get("revision"),
            tags={"job_id": job["id"], "stage": stage, "strategy": job.get("strategy"), "status": job["status"]},
            breadcrumbs=crumbs, excerpt="\n\n".join(s["text"] for s in sections) or (job.get("error") or ""),
            ref=f"deploy_job:{job['id']}", dedupe_key=key))
        n += bool(result)
    return n


# ---- Run 실패·폴백

def _crumb(ev: RunEvent) -> dict[str, Any]:
    p = ev.payload or {}
    if ev.type == "log":
        text = f"[{p.get('stream')}] {p.get('text', '')}"
    else:
        keep = {k: p[k] for k in ("status", "reason", "account", "engine", "model", "to", "to_engine", "to_model", "detail", "waiting", "ref")
                if k in p and p[k] not in (None, "")}
        text = f"{ev.type} {keep}"
    return {"ts": ev.created_at.isoformat() if ev.created_at else None, "message": text[:200]}


async def _run_breadcrumbs(db, run_id) -> list[dict[str, Any]]:
    rows = list(await db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type.in_(_BREADCRUMB_TYPES))
                                 .order_by(RunEvent.seq.desc()).limit(12)))
    return [_crumb(e) for e in reversed(rows)]


def _run_signature(error: str) -> str:
    lines = [normalize_message(l) for l in (error or "").splitlines() if l.strip()]
    if not lines:
        return "(no error text)"
    return lines[0] if len(lines[0]) >= 25 or len(lines) == 1 else f"{lines[0]} | {lines[1]}"


async def sync_runs(db) -> int:
    n = 0
    jobs = deploy_logs.list_jobs()
    since_default = int((datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)).timestamp() * 1000)
    cur = await _cursor(db, "runs:failed")
    since = datetime.fromtimestamp((cur.position if cur else since_default) / 1000, UTC)
    runs = list(await db.scalars(select(Run).where(Run.status == "failed", Run.finished_at > since).order_by(Run.finished_at).limit(200)))
    last = since
    for run in runs:
        when = run.finished_at if run.finished_at.tzinfo else run.finished_at.replace(tzinfo=UTC)
        last = max(last, when)
        error = run.error or "(no error text)"
        result = await record(db, EventIn(
            source="run", level="error", message=error, title=f"{run.engine}: {error.splitlines()[0][:160]}",
            fingerprint=fingerprint("run", run.engine, _run_signature(error)), culprit=f"run:{run.engine}",
            occurred_at=when, release=release_at(jobs, when),
            tags={"engine": run.engine, "model": run.model, "profile": run.profile, "effort": run.effort,
                  "origin": run.origin, "exit_code": run.exit_code, "run_id": str(run.id), "chat_session": str(run.session_id),
                  "release_inferred": True},
            breadcrumbs=await _run_breadcrumbs(db, run.id), ref=f"run:{run.id}", dedupe_key=f"run:{run.id}"))
        n += bool(result)
    if runs:
        await _save_cursor(db, "runs:failed", int(last.timestamp() * 1000))
    ev_cur = await _cursor(db, "runs:events")
    query = select(RunEvent).where(RunEvent.type.in_(("engine.fallback", "account.limited"))).order_by(RunEvent.id).limit(300)
    query = (query.where(RunEvent.id > ev_cur.position) if ev_cur
             else query.where(RunEvent.created_at > datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)))
    events = list(await db.scalars(query))
    for ev in events:
        p = ev.payload or {}
        when = ev.created_at if ev.created_at.tzinfo else ev.created_at.replace(tzinfo=UTC)
        if ev.type == "engine.fallback":
            pair = f"{p.get('from_engine')}/{p.get('from_model')} -> {p.get('to_engine')}/{p.get('to_model')}"
            message = f"engine fallback ({p.get('reason')}): {pair}. {p.get('detail') or ''}".strip()
            fp = fingerprint("fallback", str(p.get("from_engine")), str(p.get("to_engine")), str(p.get("reason")), normalize_message(p.get("detail") or ""))
        else:
            message = f"account limited ({p.get('engine')}/{p.get('model')}): {p.get('detail') or ''}".strip()
            fp = fingerprint("limited", str(p.get("engine")), normalize_message(p.get("detail") or ""))
        result = await record(db, EventIn(
            source="worker", level="warning", message=message, title=message[:160], fingerprint=fp,
            culprit=ev.type, occurred_at=when, release=release_at(jobs, when),
            tags={"event": ev.type, "run_id": str(ev.run_id), "reason": p.get("reason"), "release_inferred": True},
            ref=f"run:{ev.run_id}", dedupe_key=f"runevent:{ev.id}"))
        n += bool(result)
    if events:
        await _save_cursor(db, "runs:events", events[-1].id)
    return n


# ---- 프로세스 로그 파일

def _local_ts(value: str) -> datetime | None:
    try:
        naive = datetime.fromisoformat(value.replace(",", ".").replace(" ", "T"))
    except ValueError:
        return None
    return naive.astimezone() if naive.tzinfo else naive.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(UTC)


def split_blocks(data: bytes, base_offset: int, *, final: bool) -> tuple[list[tuple[int, list[str]]], int]:
    """바이트 덩어리를 오류 블록으로 나눈다. (블록 시작 오프셋, 줄들) 목록과 다음에 읽을 오프셋을 돌려준다."""
    end = data.rfind(b"\n") + 1
    raw = data[:end].split(b"\n")[:-1] if end else []
    offsets, pos = [], base_offset
    for line in raw:
        offsets.append(pos)
        pos += len(line) + 1
    lines = [l.decode("utf-8", "replace").rstrip("\r") for l in raw]
    blocks: list[tuple[int, list[str]]] = []
    i, consumed_to = 0, base_offset + end
    while i < len(lines):
        if not _START.match(lines[i]):
            i += 1
            continue
        start, block, in_tb = i, [lines[i]], lines[i].startswith("Traceback")
        i += 1
        while i < len(lines) and len(block) < MAX_BLOCK_LINES:
            line = lines[i]
            if line.startswith("Traceback"):
                in_tb = True
            elif in_tb and line and not line[0].isspace() and _EXC_TAIL.match(line):
                block.append(line)
                i += 1
                in_tb = False
                if i < len(lines) and lines[i].strip() in {"", "During handling of the above exception, another exception occurred:",
                                                          "The above exception was the direct cause of the following exception:"}:
                    block.append(lines[i])
                    i += 1
                    in_tb = i < len(lines) and lines[i].startswith("Traceback")
                    continue
                break
            elif line and not line[0].isspace() and not in_tb or not line and not in_tb:
                break
            block.append(line)
            i += 1
        open_block = i >= len(lines) and not final
        if open_block:
            consumed_to = offsets[start]
            break
        blocks.append((offsets[start], block))
    return blocks, consumed_to


def _block_event(name: str, inode: int, offset: int, block: list[str], mtime: datetime, backfill: bool, jobs: list[dict]) -> EventIn | None:
    text = "\n".join(block).rstrip()
    if _SKIP_ASGI.search(text) or (name.startswith("deployer") and _SKIP_DEPLOYER.search(text)):
        return None
    header = block[0]
    m = _TS.match(header)
    parsed_ts = _local_ts(m["ts"]) if m else None
    when = parsed_ts or (mtime if backfill else datetime.now(UTC))
    level_word = (m["level"] if m else (_UVICORN.match(header) or [None, "ERROR"])[1]) or "ERROR"
    level = "fatal" if level_word in {"CRITICAL", "FATAL"} else "error"
    trace = parse_traceback(text)
    stem = re.sub(r"\d+", "N", Path(name).stem)
    if trace:
        message = f"{trace.exc_type}: {trace.exc_message}".strip()
        fp, culprit, stack = exception_fingerprint(trace), (trace.app_frames[-1] if trace.app_frames else None), text
    else:
        message = re.sub(r"^(?:\d{4}-\d\d-\d\d[ T][\d:.,]+\s+)?(?:[A-Z]+:?\s+)", "", header).strip() or header
        fp, culprit, stack = fingerprint("proc", stem, normalize_message(message)), f"log:{name}", None
    return EventIn(
        source="process", level=level, message=message, title=message[:200], fingerprint=fp, culprit=culprit,
        occurred_at=when, release=release_at(jobs, when),
        tags={"file": name, "time_estimated": parsed_ts is None, "release_inferred": True},
        stack=stack, excerpt=text if not trace else None, ref=f"file:{name}", dedupe_key=f"file:{name}:{inode}:{offset}")


async def sync_process_logs(db) -> int:
    root = get_settings().home / "logs"
    if not root.is_dir():
        return 0
    jobs = deploy_logs.list_jobs()
    n = 0
    for path in sorted(root.glob("*.log")):
        try:
            st = path.stat()
        except OSError:
            continue
        key = f"file:{path.name}"
        cur = await _cursor(db, key)
        # 처음 보는 파일은 끝부분 일부만(백필), 교체(inode 변경)·잘림(크기 감소)은 처음부터, 그 외는 이어서 읽는다.
        if cur is None:
            pos, backfill, mid_file = max(0, st.st_size - FILE_BACKFILL_BYTES), True, True
        elif cur.inode != st.st_ino or cur.position > st.st_size:
            pos, backfill, mid_file = 0, True, False
        else:
            pos, backfill, mid_file = cur.position, False, False
        try:
            with path.open("rb") as f:
                f.seek(pos)
                if mid_file and pos:
                    f.readline()  # 잘린 첫 줄 버림
                    pos = f.tell()
                data = f.read(FILE_CHUNK_BYTES)
        except OSError:
            continue
        mtime = datetime.fromtimestamp(st.st_mtime, UTC)
        final = (time.time() - st.st_mtime) > 10 or len(data) >= FILE_CHUNK_BYTES
        blocks, next_pos = split_blocks(data, pos, final=final)
        for offset, block in blocks:
            ev = _block_event(path.name, st.st_ino, offset, block, mtime, backfill, jobs)
            if ev is not None:
                n += bool(await record(db, ev))
        await _save_cursor(db, key, next_pos, st.st_ino)
    return n


# ---- 진입점

async def sync_all(*, force: bool = False) -> dict[str, int] | None:
    """증분 인입 한 번. 다른 세대가 돌고 있으면 건너뛴다(None). 읽기 도구는 호출 직전에 `ensure_fresh`로 부른다."""
    global _last_sync
    result: dict[str, int] = {}
    async with sessionmaker()() as db:
        if not await advisory_lock(db, "sync"):
            return None
        for name, step in (("deploy", sync_deploy_jobs), ("runs", sync_runs), ("process", sync_process_logs)):
            try:
                result[name] = await step(db)
            except Exception:
                log.warning("ops-log sync step %s failed", name, exc_info=True)
                await db.rollback()
                if not await advisory_lock(db, "sync"):
                    return result
        await db.commit()
    _last_sync = time.monotonic()
    return result


async def ensure_fresh(max_age: float = FRESH_SECONDS) -> None:
    global _last_sync
    if time.monotonic() - _last_sync >= max_age:
        _last_sync = time.monotonic()  # 실패·건너뜀에도 다음 조회가 바로 다시 두드리지 않게 한다
        try:
            await sync_all()
        except Exception:
            log.warning("ops-log refresh failed", exc_info=True)


async def sync_loop(interval: float = 60) -> None:
    while True:
        try:
            await sync_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("ops-log sync failed", exc_info=True)
        await asyncio.sleep(interval)


async def capture_exception(exc: BaseException, *, source: str = "code", level: str = "error",
                            tags: dict[str, Any] | None = None, breadcrumbs: list[Any] | None = None) -> int | None:
    """코드에서 잡은 예외를 기록한다(FastAPI 핸들러도 이걸 쓴다). 기록 실패는 호출한 쪽에 번지지 않는다."""
    try:
        text = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))
        trace = parse_traceback(text)
        if trace:
            fp, message = exception_fingerprint(trace), f"{trace.exc_type}: {trace.exc_message}".strip()
            culprit = trace.app_frames[-1] if trace.app_frames else None
        else:
            fp, message, culprit = fingerprint("exc", type(exc).__name__, normalize_message(str(exc))), f"{type(exc).__name__}: {exc}", None
        async with sessionmaker()() as db:
            res = await record(db, EventIn(
                source=source, level=level, message=message, title=message[:200], fingerprint=fp, culprit=culprit,
                occurred_at=datetime.now(UTC), release=current_release(), tags=tags or {}, breadcrumbs=breadcrumbs or [], stack=text))
            await db.commit()
        return res.issue_id if res else None
    except Exception:
        log.warning("ops-log capture failed", exc_info=True)
        return None
