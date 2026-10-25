"""Runtime adapters for the collector's optional local Qwen evaluator.

This module is deliberately outside ``ttakkari.collector`` so the collector's
domain package does not depend on run orchestration or a particular model.
"""
from __future__ import annotations

from typing import Any

from ttakkari.collector import precise_eval
from ttakkari.config import get_settings


def engine_status() -> dict[str, Any]:
    """Check readiness of the local bounded evaluation engine and shared slot."""
    settings = get_settings()
    if not settings.collector_teacher_enabled:
        return {
            "status": "unavailable",
            "detail": "collector_teacher_enabled is False; teacher/evaluator is disabled",
            "model": settings.desk_qwen_model,
        }
    if not settings.desk_qwen_enabled:
        return {
            "status": "unavailable",
            "detail": "desk_qwen_enabled is False",
            "model": settings.desk_qwen_model,
        }

    try:
        from ttakkari.runs import desk_qwen, qwen_slot

        endpoint = desk_qwen.endpoint()
    except (RuntimeError, ImportError, AttributeError, OSError) as exc:
        return {
            "status": "unavailable",
            "detail": f"failed to check desk_qwen endpoint: {exc}",
            "model": settings.desk_qwen_model,
        }
    if endpoint is None:
        return {
            "status": "unavailable",
            "detail": "local qwen server endpoint not ready",
            "model": settings.desk_qwen_model,
        }
    try:
        if qwen_slot.is_process_lock_busy():
            return {
                "status": "busy",
                "detail": "local qwen slot is occupied by another process",
                "model": settings.desk_qwen_model,
            }
    except (OSError, RuntimeError) as exc:
        return {
            "status": "unavailable",
            "detail": f"failed to check qwen_slot process lock: {exc}",
            "model": settings.desk_qwen_model,
        }
    return {
        "status": "ready",
        "detail": "bounded local qwen evaluator ready",
        "model": settings.desk_qwen_model,
        "endpoint": endpoint,
    }


def install() -> None:
    """Register the local Qwen readiness probe and shared slot with the collector's pure evaluator."""
    from ttakkari.runs import qwen_slot

    precise_eval.register_runtime(precise_eval.EvaluatorRuntime(
        status=engine_status,
        # Late lookup keeps the slot module the single owner of the Mac-wide lock.
        acquire_process_lock=lambda: qwen_slot.acquire_process_lock(),
        release_process_lock=lambda fd: qwen_slot.release_process_lock(fd),
    ))
