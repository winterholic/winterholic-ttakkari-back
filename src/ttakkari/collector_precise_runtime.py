"""Runtime adapter for bounded precise evaluation through Ttakkari subscription agents."""
from __future__ import annotations

from typing import Any

from ttakkari.collector import precise_eval
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_pipeline.l2_adapters import SubscriptionCliL2Adapter


def _translate_transport_error(exc: Exception) -> precise_eval.PreciseEvaluatorTransportError | None:
    from ttakkari.kb_pipeline.subscription_transport import L2TransportError

    if not isinstance(exc, L2TransportError):
        return None
    return precise_eval.PreciseEvaluatorTransportError(
        exc.diagnosis,
        str(exc),
        retry_after=exc.retry_after,
        consumed=exc.consumed,
        engine=exc.engine,
        account=exc.account,
        model=exc.model,
    )


def engine_status() -> dict[str, Any]:
    settings = get_settings()
    engine = settings.kb_l2_subscription_engine
    model = settings.kb_l2_subscription_model or "profile default"
    if engine not in {"codex", "claude"}:
        return {"status": "unavailable", "detail": "unsupported subscription engine", "engine": engine}
    return {
        "status": "ready",
        "engine": "subscription_cli",
        "provider_engine": engine,
        "model": model,
        "detail": "bounded subscription profile availability is checked for each document scope",
    }


def _adapter(scope: str) -> SubscriptionCliL2Adapter:
    pool = precise_eval.precise_eval_pool(scope)
    visibility = "global" if pool == "personal" else "work_only"
    settings = get_settings()
    return SubscriptionCliL2Adapter(
        sessionmaker(),
        engine=settings.kb_l2_subscription_engine,
        model=settings.kb_l2_subscription_model,
        effort=settings.kb_l2_subscription_effort,
        timeout=settings.kb_l2_subscription_timeout_seconds,
        concurrency=settings.kb_l2_subscription_concurrency,
        origin_scope=pool,
        visibility=visibility,
    )


async def availability(*, scope: str) -> tuple[bool, str | None, float | None]:
    try:
        adapter = _adapter(scope)
        available, reason = await adapter.availability()
        return available, reason, adapter.retry_after
    except Exception as exc:
        translated = _translate_transport_error(exc)
        if translated is None:
            raise
        return False, translated.diagnosis, translated.retry_after


async def evaluate(*, prompt: str, schema: dict[str, Any], system_prompt: str,
                   scope: str, **_context: Any) -> tuple[dict[str, Any], dict[str, str]]:
    adapter = _adapter(scope)
    try:
        return await adapter.call_json(
            prompt,
            schema,
            system_prompt=system_prompt,
            validator=precise_eval.validate_evaluator_result,
            max_response_bytes=128_000,
        )
    except Exception as exc:
        translated = _translate_transport_error(exc)
        if translated is None:
            raise
        raise translated from exc


def install() -> None:
    precise_eval.register_runtime(precise_eval.EvaluatorRuntime(
        status=engine_status, evaluate=evaluate, availability=availability,
    ))
