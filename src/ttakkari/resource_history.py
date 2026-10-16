"""Always-on bounded history for system resource metrics."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete

from ttakkari import sysinfo
from ttakkari.db import sessionmaker
from ttakkari.models import SystemResourceSample

log = logging.getLogger(__name__)
SAMPLE_INTERVAL_SECONDS = 30
RAW_RETENTION_DAYS = 90
RETENTION_SWEEP_SECONDS = 3600
_last_retention_sweep = 0.0


def _number(value: Any, *, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if result < 0 or (maximum is not None and result > maximum):
        return None
    return result


async def collect_once() -> None:
    """Read the same cached collector as the live resource page and persist one row."""
    global _last_retention_sweep
    now = datetime.now(UTC)
    try:
        snapshot = await sysinfo.snapshot()
        collected = snapshot.get("collected_at")
        sampled_at = datetime.fromtimestamp(float(collected), UTC) if collected is not None else now
        gpu = snapshot.get("gpu") if isinstance(snapshot.get("gpu"), dict) else {}
        gpu_status = gpu.get("status")
        if gpu_status not in {"available", "unavailable", "unsupported"}:
            gpu_status = "unavailable"
        gpu_percent = _number(gpu.get("percent"), maximum=100) if gpu_status == "available" else None
        gpu_memory = _number(gpu.get("memory_used_bytes")) if gpu_status == "available" else None
        cpu = snapshot.get("cpu") if isinstance(snapshot.get("cpu"), dict) else {}
        memory = snapshot.get("memory") if isinstance(snapshot.get("memory"), dict) else {}
        cpu_percent = _number(cpu.get("percent"), maximum=100)
        memory_used = _number(memory.get("used"))
        collection_status = "success"
    except Exception:  # noqa: BLE001 -- record collection failure as null metrics
        sampled_at = now
        gpu_status = "unavailable"
        gpu_percent = gpu_memory = cpu_percent = memory_used = None
        collection_status = "failed"

    async with sessionmaker()() as db:
        db.add(SystemResourceSample(
            sampled_at=sampled_at,
            gpu_percent=gpu_percent,
            gpu_memory_used_bytes=int(gpu_memory) if gpu_memory is not None else None,
            gpu_status=gpu_status,
            collection_status=collection_status,
            cpu_percent=cpu_percent,
            memory_used_bytes=int(memory_used) if memory_used is not None else None,
        ))
        monotonic_now = time.monotonic()
        sweep_retention = monotonic_now - _last_retention_sweep >= RETENTION_SWEEP_SECONDS
        if sweep_retention:
            await db.execute(delete(SystemResourceSample).where(
                SystemResourceSample.sampled_at < now - timedelta(days=RAW_RETENTION_DAYS)
            ))
        await db.commit()
        if sweep_retention:
            _last_retention_sweep = monotonic_now
    if collection_status == "failed":
        log.warning("system resource collection failed; stored null metrics")


async def run(interval_seconds: int = SAMPLE_INTERVAL_SECONDS) -> None:
    """Sample immediately, then at a fixed interval while the backend lifespan is active."""
    while True:
        started = time.monotonic()
        try:
            await collect_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("system resource sample persistence failed")
        await asyncio.sleep(max(0.0, interval_seconds - (time.monotonic() - started)))
