"""Execution-scoped external-resource checkpoints and conservative crash cleanup."""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ttakkari.config import get_settings
from ttakkari.research.ownership import owner_token, probe

logger = logging.getLogger(__name__)
RESOURCE_KEY = "research_external_resource"
RECOVERY_KEY = "research_resource_recovery"
_checkpoint: ContextVar[Callable[[dict], Awaitable[None]] | None] = ContextVar(
    "research_resource_checkpoint", default=None,
)


@contextmanager
def resource_checkpoints(write: Callable[[dict], Awaitable[None]]) -> Iterator[None]:
    token = _checkpoint.set(write)
    try:
        yield
    finally:
        _checkpoint.reset(token)


async def checkpoint_resource(resource: dict) -> None:
    """Adapters outside a Research Run have no sink and keep their existing API."""
    write = _checkpoint.get()
    if write is not None:
        await write(resource)


def observe_execution_owners(rows, *, truncated: bool) -> tuple[list[dict], dict]:
    """Observe execution ownership; this is not a GPU/TTS/CPU mutex claim."""
    locks, unknown, observations = [], [], {}
    for row in rows:
        token = owner_token(row.environment_snapshot or {})
        if token is None:
            if row.status in ("running", "queued"):
                unknown.append(str(row.id))
            continue
        if token not in observations:
            try:
                handle = probe(token)
                observations[token] = "held" if handle is None else "released"
                if handle is not None:
                    handle.close()
            except OSError:
                logger.warning("Research owner lock observation failed", exc_info=True)
                observations[token] = "unverified"
        if observations[token] == "held":
            locks.append({"lock": "research_execution_owner", "adapter_id": row.adapter_id,
                          "run_id": str(row.id), "experiment_id": str(row.experiment_id),
                          "evidence": "os_lock_observed", "owner_token": str(token)})
        elif observations[token] == "unverified":
            unknown.append(str(row.id))
    return locks, {"basis": "execution_owner_os_lock", "candidate_limit": 500, "truncated": truncated,
                   "unverified_run_ids": unknown, "resource_lock_status": "unverified"}


def _owned_gpu_launch(snapshot: dict) -> dict | None:
    resource = snapshot.get(RESOURCE_KEY)
    token = owner_token(snapshot)
    if (not isinstance(resource, dict) or type(resource.get("contract")) is not int
            or resource["contract"] != 1 or token is None
            or resource.get("owner_token") != str(token) or resource.get("kind") != "host_gpu"):
        return None
    launch = resource.get("launch")
    if not isinstance(launch, dict):
        return None
    try:
        uuid.UUID(launch["launch_id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    for role in ("supervisor", "server"):
        pid, started = launch.get(f"{role}_pid"), launch.get(f"{role}_start")
        if role == "server" and pid is None and started is None:
            continue  # the supervisor may still be starting llama-server
        if (type(pid) is not int or pid <= 0 or isinstance(started, bool)
                or not isinstance(started, (int, float)) or not 0 < started < math.inf):
            return None
    return launch


async def recover_resource(snapshot: dict) -> dict:
    """Never stop by status, model name, PID alone, or an unknown resource kind."""
    launch = _owned_gpu_launch(snapshot)
    if launch is None:
        return {"status": "unverified", "reason": "no_verified_host_gpu_checkpoint"}
    from ttakkari.host_gpu import HostGpu
    from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter

    settings = get_settings()
    try:
        result = await asyncio.to_thread(
            LlmInferenceAdapter._cleanup_owned_launch, HostGpu(settings), settings.data_dir, launch,
        )
    except Exception as exc:
        logger.warning("Research orphan resource cleanup could not be verified", exc_info=True)
        return {"status": "unverified", "reason": type(exc).__name__, "launch_id": launch["launch_id"]}
    return {"status": "verified" if result.get("success") is True else "unverified", "evidence": result}
