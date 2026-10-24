"""TTS 전용 직렬 큐(설계 §6, §8, FND-01). STT·E5 큐는 건드리지 않는다.

- FND-01: 원문 청크는 `TtsJob.chunks_json`에 영속화되어 프로세스 재시작 및 드레인 중 복구를 지원한다.
  접수한 프로세스는 `_pending_texts`에도 캐시하며 부모는 자식 종료까지 원문을 보유한다.
- 모든 상태 전이는 `UPDATE ... WHERE id=:id AND status IN (...) [AND owner_id=:me]` 한 문장이고 영향 행 1일 때만 확정한다.
- 소유 queued 가 있는 동안 세대 퇴역(`can_retire`)을 차단하여 기수락 작업(accepted work)을 끝까지 완수한다.
- 소유자 사망은 flock 획득 성공 또는 runtime_generations.state='offline' 만 증거로 쓴다. heartbeat 만료는 증거가 아니다.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import math
import os
import time
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, text, update

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import RuntimeGeneration, utcnow
from ttakkari.tts import cache, service
from ttakkari.tts.service import Plan, TtsError
from ttakkari.tts_models import TtsJob

log = logging.getLogger(__name__)

TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("queued", "running")
# 사망 확인 뒤 running 행을 정리하기까지의 유예. 자식 부모 감시가 1초 폴링이라(model_process.watch_parent) 여유를 둔다.
DEAD_GRACE_SECONDS = 3.0
# 소유 세대가 DELETE 를 감지하는 주기. runtime_poll_seconds(기본 5초)보다 짧아야 한다.
CANCEL_POLL_SECONDS = 1.0
CACHE_PRUNE_SECONDS = 3600.0

_process_owner = uuid.uuid4()
_pending_texts: dict[uuid.UUID, list[str]] = {}
_running: dict[uuid.UUID, asyncio.Task] = {}
_dead_seen: dict[uuid.UUID, float] = {}
_task: asyncio.Task | None = None
_guard_task: asyncio.Task | None = None
_guard_lock = asyncio.Lock()
_wake = asyncio.Event()
_last_prune = 0.0


def owner_id() -> uuid.UUID:
    return generation.current.id if generation.current else _process_owner


def parse_job_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


# ------------------------------------------------------------------ 상태 전이(CAS)


async def cas(
    job_id: uuid.UUID,
    *,
    expected: tuple[str, ...],
    status: str,
    owner: uuid.UUID | None = None,
    owner_is: uuid.UUID | None = None,
    error_code: str | None = None,
    metadata: dict | None = None,
) -> bool:
    """조건부 UPDATE 한 문장. 영향 행이 1일 때만 True. 0 이면 전이에서 진 것이므로 호출자는 부수 효과를 버린다."""
    values: dict = {"status": status, "error_code": error_code}
    if status in TERMINAL:
        values["finished_at"] = utcnow()
    if metadata is not None:
        values["result_metadata"] = metadata
    stmt = update(TtsJob).where(TtsJob.id == job_id, TtsJob.status.in_(expected))
    if owner is not None:
        stmt = stmt.where(TtsJob.owner_id == owner)
    if owner_is is not None:
        stmt = stmt.where(TtsJob.owner_id == owner_is)
    async with sessionmaker()() as db:
        result = await db.execute(stmt.values(**values))
        await db.commit()
    return result.rowcount == 1


async def fail_owned(
    owner: uuid.UUID, *, expected: tuple[str, ...], error_code: str, older_than: float | None = None
) -> list[uuid.UUID]:
    """소유자 job 을 CAS 로 failed 확정한다. 확정된 id 목록을 돌려준다."""
    stmt = update(TtsJob).where(TtsJob.owner_id == owner, TtsJob.status.in_(expected))
    if older_than is not None:
        stmt = stmt.where(TtsJob.created_at < utcnow() - timedelta(seconds=older_than))
    async with sessionmaker()() as db:
        result = await db.execute(
            stmt.values(status="failed", error_code=error_code, finished_at=utcnow()).returning(TtsJob.id)
        )
        ids = list(result.scalars())
        await db.commit()
    return ids


async def _claim(me: uuid.UUID) -> uuid.UUID | None:
    """세대별 원자적 claim(설계 §8.3.4). advisory xact lock 으로 두 claim 이 동시에 running 을 보지 못하는 틈을 막는다."""
    async with sessionmaker()() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('tts_jobs_claim'))"))
        result = await db.execute(
            text(
                "UPDATE tts_jobs SET status='running', started_at=now() "
                "WHERE id = (SELECT id FROM tts_jobs WHERE status='queued' AND owner_id=:me "
                "            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "AND status='queued' AND owner_id=:me "
                "AND NOT EXISTS (SELECT 1 FROM tts_jobs WHERE status='running') "
                "RETURNING id"
            ),
            {"me": me},
        )
        row = result.first()
        await db.commit()
    return row[0] if row else None


# ------------------------------------------------------------------ guard (§8.3.3)


async def _has_owned_queued(me: uuid.UUID) -> bool:
    async with sessionmaker()() as db:
        return (
            await db.scalar(select(TtsJob.id).where(TtsJob.status == "queued", TtsJob.owner_id == me).limit(1))
        ) is not None


async def _fail_queued_draining(me: uuid.UUID) -> None:
    for job_id in await fail_owned(me, expected=("queued",), error_code="generation_draining"):
        _pending_texts.pop(job_id, None)


async def _guard_loop() -> None:
    global _guard_task
    runtime = generation.current
    assert runtime is not None
    poll = get_settings().runtime_poll_seconds
    while True:
        try:
            await runtime.refresh()
            # FND-01: draining 시 queued 작업을 fail 처리하지 않고 끝까지 수행한다.
            async with _guard_lock:
                # 새 POST 는 이 락 안에서 job 을 커밋하므로, 여기서 0건이면 guard 없이 남는 queued 가 없다.
                if not await _has_owned_queued(runtime.id):
                    _guard_task = None
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tts queued guard iteration failed")
        await asyncio.sleep(poll)


@contextlib.asynccontextmanager
async def guard_scope():
    """소유 queued 를 만들기 전에 guard 를 보장하고, 커밋이 끝날 때까지 guard 의 종료 판정을 막는다."""
    global _guard_task
    async with _guard_lock:
        if generation.current is not None and (_guard_task is None or _guard_task.done()):
            from ttakkari.runs.manager import _background

            _guard_task = asyncio.create_task(_guard_loop(), name="tts-queued-guard")
            _background.add(_guard_task)
            _guard_task.add_done_callback(_background.discard)
        yield


# ------------------------------------------------------------------ 접수


def _params(plan: Plan, *, cached: int) -> dict:
    return {
        "engine": plan.provider.id,
        "model_revision": plan.provider.model_revision(),
        "voice": plan.voice,
        "speed": plan.speed,
        "lang": plan.lang,
        "text_length": plan.text_length,
        "chunk_count": len(plan.chunks),
        "chunk_keys": plan.keys,
        "cached_chunks": cached,
    }


async def accept(plan: Plan) -> dict:
    """POST /jobs. 세대가 active 가 아니면 접수하지 않는다(503, 원문은 어디에도 남기지 않는다)."""
    runtime = generation.current
    if runtime is not None and await runtime.refresh() != "active":
        raise TtsError("generation_not_active", "배포 전환 중이에요. 잠시 후 다시 시도해 주세요.", 503, retry_after=5)
    me = owner_id()
    async with sessionmaker()() as db:
        found = await cache.lookup_many(db, plan.keys)
    cached = sum(1 for k in plan.keys if k in found)
    cache.count_lookup(len(plan.keys), cached)
    job_id = uuid.uuid4()
    all_cached = cached == len(plan.keys)
    now = utcnow()
    job = TtsJob(
        id=job_id,
        owner_id=me,
        status="succeeded" if all_cached else "queued",
        params=_params(plan, cached=cached),
        chunks_json=list(plan.chunks),
        result_metadata={"synthesized_chunks": 0, "from_cache": True} if all_cached else {},
        created_at=now,
        started_at=now if all_cached else None,
        finished_at=now if all_cached else None,
    )
    if all_cached:
        async with sessionmaker()() as db:
            db.add(job)
            await db.commit()
    else:
        async with guard_scope():
            _pending_texts[job_id] = list(plan.chunks)
            try:
                async with sessionmaker()() as db:
                    db.add(job)
                    await db.commit()
            except BaseException:
                _pending_texts.pop(job_id, None)
                raise
        kick()
    return {"job_id": str(job_id), "status": job.status, "chunks": len(plan.chunks), "cached_chunks": cached}


# ------------------------------------------------------------------ 실행


_METRIC_KEYS = ("load_seconds", "synth_seconds", "audio_seconds", "rtf", "peak_rss_bytes", "cpu_threads",
                "wall_seconds", "onnxruntime")  # fmt: skip


def _safe_metrics(raw: dict) -> dict:
    out = {}
    for key in _METRIC_KEYS:
        value = raw.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if isinstance(value, (int, float, str)):
            out[key] = value
    first, start = raw.get("first_chunk_epoch"), raw.get("worker_started_epoch")
    if isinstance(first, (int, float)) and isinstance(start, (int, float)):
        out["first_chunk_seconds"] = first - start
    return out


async def _watch_cancel(job_id: uuid.UUID, task: asyncio.Task) -> None:
    while not task.done():
        await asyncio.sleep(CANCEL_POLL_SECONDS)
        async with sessionmaker()() as db:
            status = await db.scalar(select(TtsJob.status).where(TtsJob.id == job_id))
        if status != "running":
            task.cancel()
            return


def _plan_from_job(job: TtsJob, texts: list[str]) -> Plan:
    params = job.params
    provider = service.get_provider()
    keys = list(params["chunk_keys"])
    if len(keys) != len(texts):
        raise RuntimeError("tts_job_param_mismatch")
    return Plan(
        provider, params["voice"], float(params["speed"]), params["lang"], list(texts), keys, int(params["text_length"])
    )


async def run_claimed(job_id: uuid.UUID) -> None:
    """claim 된 job 하나를 실행한다. 호출자가 실행 예산(budget)을 쥐고 있어야 한다."""
    me = owner_id()
    try:
        async with sessionmaker()() as db:
            job = await db.get(TtsJob, job_id)
        if job is None:
            return
        texts = _pending_texts.get(job_id)
        if texts is None and job.chunks_json:
            texts = list(job.chunks_json)
        if texts is None:
            await cas(job_id, expected=("running",), status="failed", owner=me, error_code="input_missing")
            return
        try:
            plan = _plan_from_job(job, texts)
        except Exception as exc:  # noqa: BLE001 -- 엔진이 사라졌거나 params 가 깨진 경우: 영구 실패로 확정
            log.warning("tts job %s cannot start: %s", job_id, type(exc).__name__)
            await cas(job_id, expected=("running",), status="failed", owner=me, error_code="worker_failed")
            return
        async with sessionmaker()() as db:
            found = await cache.lookup_many(db, plan.keys)
        seen: set[str] = set()
        missing = []
        for index, key in enumerate(plan.keys):
            if key not in found and key not in seen:
                seen.add(key)
                missing.append(index)
        metrics: dict = {}
        if missing:
            task = asyncio.create_task(service.synthesize_missing(plan, missing))
            _running[job_id] = task
            watcher = asyncio.create_task(_watch_cancel(job_id, task))
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if task.cancelled():
                return  # DELETE 가 이미 cancelled 로 확정했다. 자식은 run_json_worker 의 finally 가 kill·wait 했다.
            exc = task.exception()
            if exc is not None:
                code = "worker_timeout" if isinstance(exc, TimeoutError) else "worker_failed"
                log.warning("tts job %s failed: %s", job_id, type(exc).__name__)
                await cas(job_id, expected=("running",), status="failed", owner=me, error_code=code)
                return
            metrics = _safe_metrics(task.result().metrics)
        async with sessionmaker()() as db:
            rows = await cache.lookup_many(db, plan.keys, touch=False)
        if any(k not in rows for k in plan.keys):
            await cas(job_id, expected=("running",), status="failed", owner=me, error_code="worker_failed")
            return
        # 오디오는 이미 내용 주소 캐시에 있다. 이 UPDATE 가 0행이면(취소가 먼저) job 에는 연결하지 않는다.
        await cas(
            job_id,
            expected=("running",),
            status="succeeded",
            owner=me,
            metadata={
                "durations": [rows[k].duration_seconds for k in plan.keys],
                "synthesized_chunks": len(missing),
                "cached_chunks": len(plan.keys) - len(missing),
                "metrics": metrics,
            },
        )
    finally:
        _pending_texts.pop(job_id, None)
        _running.pop(job_id, None)


# ------------------------------------------------------------------ 소유자 생존(§8.3.5)


def _lock_is_free(path: Path) -> bool:
    """잠금 파일이 있고 flock 을 얻을 수 있으면 보유 프로세스가 없다. 없는 파일은 증거가 아니다."""
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


async def owner_dead(owner: uuid.UUID) -> bool:
    """사망 증거: <home>/runtime/<owner>.lock 의 flock 획득 성공, 또는 runtime_generations.state='offline'."""
    runtime = generation.current
    if runtime is None:
        return False
    async with sessionmaker()() as db:
        row = await db.get(RuntimeGeneration, owner)
    if row is None:
        return False
    if row.state == "offline":
        return True
    return _lock_is_free(runtime.home / "runtime" / f"{owner}.lock")


async def reap_dead_owner(owner: uuid.UUID) -> bool:
    """사망이 확인된 소유자의 non-terminal job 을 failed(owner_gone)로 확정한다. running 은 유예 뒤에."""
    if owner == owner_id() or not await owner_dead(owner):
        _dead_seen.pop(owner, None)
        return False
    first = _dead_seen.setdefault(owner, time.monotonic())
    expected = ACTIVE if time.monotonic() - first >= DEAD_GRACE_SECONDS else ("queued",)
    return bool(await fail_owned(owner, expected=expected, error_code="owner_gone"))


async def maybe_reap(job: TtsJob) -> TtsJob:
    """GET·DELETE 의 읽기 시점 규칙. 다른 소유자의 non-terminal job 이면 사망 증거를 확인하고 CAS 로 확정한다."""
    if job.status in ACTIVE and job.owner_id != owner_id() and await reap_dead_owner(job.owner_id):
        async with sessionmaker()() as db:
            fresh = await db.get(TtsJob, job.id, populate_existing=True)
        return fresh or job
    return job


# ------------------------------------------------------------------ 조회·취소


async def get_job(job_id: uuid.UUID) -> TtsJob | None:
    async with sessionmaker()() as db:
        job = await db.get(TtsJob, job_id)
    return await maybe_reap(job) if job else None


async def cancel(job_id: uuid.UUID) -> TtsJob | None:
    job = await get_job(job_id)
    if job is None:
        return None
    cancelled = job.status in ACTIVE and await cas(job_id, expected=ACTIVE, status="cancelled")
    if cancelled and job.owner_id == owner_id():
        _pending_texts.pop(job_id, None)
        task = _running.get(job_id)
        if task is not None:
            task.cancel()
    async with sessionmaker()() as db:
        return await db.get(TtsJob, job_id, populate_existing=True)


# ------------------------------------------------------------------ consumer


async def _housekeeping(me: uuid.UUID) -> None:
    global _last_prune
    ttl = get_settings().local_tts_queued_ttl_seconds
    for job_id in await fail_owned(me, expected=("queued",), error_code="input_ttl_expired", older_than=ttl):
        _pending_texts.pop(job_id, None)
    # accept 는 이 락 안에서 원문을 넣고 커밋한다. 락 밖에서 보면 아직 커밋 전인 job 의 원문을 "행 없음"으로 보고 지운다.
    async with _guard_lock:
        if _pending_texts:
            async with sessionmaker()() as db:
                live = set(
                    await db.scalars(
                        select(TtsJob.id).where(TtsJob.id.in_(list(_pending_texts)), TtsJob.status.in_(ACTIVE))
                    )
                )
            for job_id in [k for k in _pending_texts if k not in live]:
                _pending_texts.pop(job_id, None)
    if generation.current is not None:
        async with sessionmaker()() as db:
            owners = set(
                await db.scalars(select(TtsJob.owner_id).where(TtsJob.status.in_(ACTIVE), TtsJob.owner_id != me))
            )
        for owner in owners:
            await reap_dead_owner(owner)
    if time.monotonic() - _last_prune >= CACHE_PRUNE_SECONDS:
        _last_prune = time.monotonic()
        async with sessionmaker()() as db:
            await cache.prune(db)


async def tick() -> bool:
    """한 번의 consumer 반복. job 을 하나 실행했으면 True."""
    me = owner_id()
    await _housekeeping(me)
    runtime = generation.current
    if runtime is not None and await runtime.refresh() not in ("active", "draining"):
        return False  # retired / offline 은 새 job 을 시작하지 않는다.
    if not await _has_owned_queued(me):
        return False
    async with service.budget(0, allow_draining=True) as outcome:
        if outcome != "ok":
            return False
        job_id = await _claim(me)
        if job_id is None:
            return False
        await run_claimed(job_id)
    return True


async def _loop() -> None:
    poll = get_settings().runtime_poll_seconds
    while True:
        _wake.clear()
        worked = False
        try:
            worked = await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tts queue iteration failed")
        if worked:
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_wake.wait(), poll)


def kick() -> None:
    global _task
    _wake.set()
    if get_settings().local_tts_enabled and (_task is None or _task.done()):
        _task = asyncio.create_task(_loop(), name="tts-consumer")


async def start() -> None:
    if not get_settings().local_tts_enabled:
        return
    if generation.current is None:
        # 단일 프로세스를 가정한다(STT start() 가 running 을 queued 로 되돌리는 것과 같은 가정). 원문이 사라졌으므로 실패로 확정.
        async with sessionmaker()() as db:
            await db.execute(
                update(TtsJob)
                .where(TtsJob.status.in_(ACTIVE), TtsJob.owner_id != _process_owner)
                .values(status="failed", error_code="server_restarted", finished_at=utcnow())
            )
            await db.commit()
        import shutil

        shutil.rmtree(cache.staging_root(), ignore_errors=True)
    kick()


async def stop() -> None:
    global _task, _guard_task
    me = owner_id()
    for task in (_task, _guard_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    _task = _guard_task = None
    with contextlib.suppress(Exception):
        await fail_owned(me, expected=ACTIVE, error_code="owner_shutdown")
    _pending_texts.clear()
