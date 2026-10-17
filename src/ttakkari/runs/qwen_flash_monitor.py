"""Read-only monitoring of the standalone Flash Next server, separate from intake.

Counters are llama.cpp server-lifetime counters, including any clients of that
server. They are never added to operational intake's per-call database totals.
Increments are durably recorded to provide restart-safe daily and weekly usage windows.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.models import QwenFlashUsage

KST = ZoneInfo("Asia/Seoul")

# Public placeholder used by qwen-local-harness; this is not a cloud credential.
_LOCAL_PLACEHOLDER = "qwen-local-dummy-token"
_METRICS = {
    "input_tokens": "llamacpp:prompt_tokens_total",
    "cached_input_tokens": "llamacpp:prompt_tokens_cached_total",
    "output_tokens": "llamacpp:tokens_predicted_total",
    "requests_processing": "llamacpp:requests_processing",
    "requests_deferred": "llamacpp:requests_deferred",
}


def period_bounds(now: datetime | None = None) -> dict[str, tuple[datetime, datetime]]:
    local = (now or datetime.now(UTC)).astimezone(KST)
    today = datetime.combine(local.date(), time.min, KST)
    monday = today - timedelta(days=local.weekday())
    current = local.astimezone(UTC)
    return {"today": (today.astimezone(UTC), current), "week": (monday.astimezone(UTC), current)}


async def record_observation(
    db: AsyncSession, observation: dict, recorded_at: datetime | None = None
) -> QwenFlashUsage | None:
    server = observation.get("server") or {}
    metrics_status = observation.get("metrics_status")
    # Only record when server is running and metrics are available/partial
    if server.get("status") != "running" and metrics_status not in ("available", "partial"):
        return None

    raw_p = observation.get("input_tokens")
    raw_c = observation.get("cached_input_tokens")
    raw_o = observation.get("output_tokens")
    if raw_p is None and raw_o is None:
        return None

    stmt = select(QwenFlashUsage).order_by(QwenFlashUsage.recorded_at.desc(), QwenFlashUsage.id.desc()).limit(1)
    last_row = (await db.execute(stmt)).scalar_one_or_none()

    curr_p = int(raw_p) if raw_p is not None else 0
    curr_c = int(raw_c) if raw_c is not None else 0
    curr_o = int(raw_o) if raw_o is not None else 0

    if last_row is None or last_row.raw_prompt_tokens is None:
        delta_p = curr_p
        delta_c = curr_c
        delta_o = curr_o
    else:
        prev_p = int(last_row.raw_prompt_tokens or 0)
        prev_c = int(last_row.raw_cached_tokens or 0)
        prev_o = int(last_row.raw_output_tokens or 0)

        if curr_p >= prev_p and curr_o >= prev_o:
            delta_p = curr_p - prev_p
            delta_c = max(0, curr_c - prev_c)
            delta_o = curr_o - prev_o
        else:
            # llama.cpp server restarted; reset counter to 0 baseline
            delta_p = curr_p
            delta_c = curr_c
            delta_o = curr_o

    if delta_p == 0 and delta_c == 0 and delta_o == 0 and last_row is not None:
        return None

    at = recorded_at or datetime.now(UTC)
    row = QwenFlashUsage(
        recorded_at=at,
        input_tokens=delta_p,
        cached_input_tokens=delta_c,
        output_tokens=delta_o,
        total_tokens=delta_p + delta_o,
        raw_prompt_tokens=raw_p,
        raw_cached_tokens=raw_c,
        raw_output_tokens=raw_o,
    )
    db.add(row)
    await db.commit()
    return row


async def _period(db: AsyncSession, start: datetime, end: datetime) -> dict:
    rows = await db.execute(
        select(
            func.coalesce(func.sum(QwenFlashUsage.input_tokens), 0),
            func.coalesce(func.sum(QwenFlashUsage.cached_input_tokens), 0),
            func.coalesce(func.sum(QwenFlashUsage.output_tokens), 0),
            func.coalesce(func.sum(QwenFlashUsage.total_tokens), 0),
            func.max(QwenFlashUsage.recorded_at),
        ).where(QwenFlashUsage.recorded_at >= start, QwenFlashUsage.recorded_at <= end)
    )
    inputs, cached, outputs, total, last = rows.one()
    return {
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "input_tokens": inputs,
        "cached_input_tokens": cached,
        "output_tokens": outputs,
        "total_tokens": total,
        "last_used_at": last.isoformat() if last else None,
    }


async def summary(db: AsyncSession, *, now: datetime | None = None, live: dict | None = None) -> dict:
    bounds = period_bounds(now)
    collected = await db.scalar(select(func.min(QwenFlashUsage.recorded_at)))
    today = await _period(db, *bounds["today"])
    week = await _period(db, *bounds["week"])
    live_dict = live or {}
    return {
        "model": live_dict.get("model", "Qwen3.8-Flash-Next Q2_K"),
        "scope": "server_lifetime",
        "timezone": "Asia/Seoul",
        "observed_at": live_dict.get("observed_at", (now or datetime.now(UTC)).isoformat()),
        "collection_started_at": collected.isoformat() if collected else None,
        "server": live_dict.get("server", {"status": "unavailable", "detail": None}),
        "metrics_status": live_dict.get("metrics_status", "unavailable"),
        "requests_processing": live_dict.get("requests_processing"),
        "requests_deferred": live_dict.get("requests_deferred"),
        "input_tokens": live_dict.get("input_tokens"),
        "cached_input_tokens": live_dict.get("cached_input_tokens"),
        "output_tokens": live_dict.get("output_tokens"),
        "today": today,
        "week": week,
    }


async def read(client: httpx.AsyncClient, *, expected_model: str) -> dict:
    result = {
        "model": "Qwen3.8-Flash-Next Q2_K",
        "scope": "server_lifetime",
        "observed_at": datetime.now(UTC).isoformat(),
        "server": {"status": "unavailable", "detail": None},
        "metrics_status": "unavailable",
        **dict.fromkeys(_METRICS),
    }
    try:
        props = await client.get("/props")
        if props.status_code == 503:
            result["server"]["status"] = "starting"
            return result
        props.raise_for_status()
        identity = props.json()
        if not isinstance(identity, dict) or identity.get("model_alias") != expected_model:
            result["server"]["status"] = "model_mismatch"
            return result
        result["server"]["status"] = "running"
        response = await client.get("/metrics")
        response.raise_for_status()
        values = {}
        for line in response.text.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] not in _METRICS.values():
                continue
            try:
                number = float(parts[1])
            except ValueError:
                continue
            if math.isfinite(number) and number >= 0 and number.is_integer():
                values[parts[0]] = int(number)
        for key, metric in _METRICS.items():
            result[key] = values.get(metric)
        known = sum(result[key] is not None for key in _METRICS)
        result["metrics_status"] = "available" if known == len(_METRICS) else "partial" if known else "unavailable"
    except (httpx.HTTPError, ValueError):
        # Keep unknown values null; do not expose URLs, response bodies or auth headers.
        pass
    return result


async def snapshot() -> dict:
    settings = get_settings()
    # Port-only configuration keeps monitoring on loopback; no proxy, redirects,
    # model launch, inference calls or process lifecycle actions are involved.
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{settings.qwen_flash_monitor_port}",
        headers={"Authorization": f"Bearer {_LOCAL_PLACEHOLDER}"},
        timeout=2.0,
        trust_env=False,
    ) as client:
        return await read(client, expected_model=settings.qwen_flash_monitor_model)


async def snapshot_with_history(db: AsyncSession, *, now: datetime | None = None) -> dict:
    live = await snapshot()
    await record_observation(db, live, recorded_at=now)
    return await summary(db, now=now, live=live)
