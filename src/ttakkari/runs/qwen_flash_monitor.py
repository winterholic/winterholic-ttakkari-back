"""Read-only monitoring of the standalone Flash Next server, separate from intake.

Counters are llama.cpp server-lifetime counters, including any clients of that
server. They are never added to operational intake's per-call database totals.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime

import httpx

from ttakkari.config import get_settings

# Public placeholder used by qwen-local-harness; this is not a cloud credential.
_LOCAL_PLACEHOLDER = "qwen-local-dummy-token"
_METRICS = {
    "input_tokens": "llamacpp:prompt_tokens_total",
    "cached_input_tokens": "llamacpp:prompt_tokens_cached_total",
    "output_tokens": "llamacpp:tokens_predicted_total",
    "requests_processing": "llamacpp:requests_processing",
    "requests_deferred": "llamacpp:requests_deferred",
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
        timeout=2.0, trust_env=False,
    ) as client:
        return await read(client, expected_model=settings.qwen_flash_monitor_model)
