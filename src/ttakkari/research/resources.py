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
WORKERS_KEY = "research_model_workers"
MODEL_MODULES = frozenset({"ttakkari.tts.worker_supertonic", "ttakkari.meetings.local_stt.worker"})
_checkpoint: ContextVar[Callable[[dict], Awaitable[None]] | None] = ContextVar(
    "research_resource_checkpoint", default=None,
)


@contextmanager
def resource_checkpoints(write: Callable[[dict], Awaitable[None]]) -> Iterator[None]:
    from ttakkari.model_process import worker_observations

    async def observe_worker(event: dict) -> None:
        if event.get("module") in MODEL_MODULES:
            await write({**event, "kind": "model_worker"})
        else:
            logger.warning("No research receipt contract for model worker module %s", event.get("module"))

    token = _checkpoint.set(write)
    try:
        with worker_observations(observe_worker):
            yield
    finally:
        _checkpoint.reset(token)


async def checkpoint_resource(resource: dict) -> None:
    """Adapters outside a Research Run have no sink and keep their existing API."""
    write = _checkpoint.get()
    if write is not None:
        await write(resource)


def merge_worker_checkpoint(previous: dict, event: dict, token: str) -> dict:
    """Called under the Run's fenced row lock; concurrent children merge there."""
    from ttakkari.host_gpu_guard import proc_start

    worker_id = str(uuid.UUID(event["worker_id"]))
    if (event.get("module") not in MODEL_MODULES
            or any(type(event.get(k)) is not int or event[k] <= 0 for k in ("pid", "parent_pid"))
            or event.get("state") not in ("running", "exited")
            or (event["state"] == "exited" and type(event.get("returncode")) is not int)):
        raise ValueError("Invalid model worker observation")
    existing = previous.get(WORKERS_KEY) or {}
    if existing and (existing.get("contract") != 1 or existing.get("owner_token") != token):
        raise ValueError("Model worker checkpoint owner changed")
    workers = dict(existing.get("workers") or {})
    old = workers.get(worker_id) or {}
    if old and any(old.get(k) != event[k] for k in ("module", "pid", "parent_pid")):
        raise ValueError("Model worker identity changed")
    if old.get("state") == "exited" and event["state"] != "exited":
        raise ValueError("Model worker exit cannot be reversed")
    workers[worker_id] = {k: event[k] for k in ("module", "pid", "parent_pid", "state", "returncode")}
    workers[worker_id]["started_at"] = old.get("started_at") if old else proc_start(event["pid"])
    return {"contract": 1, "owner_token": token, "workers": workers,
            "state": "released" if all(w["state"] == "exited" for w in workers.values()) else "owned"}


async def recover_model_workers(snapshot: dict) -> dict:
    """Observe recorded children only; a released owner never authorizes PID signals."""
    from ttakkari.host_gpu_guard import ProcState, process_state

    resource, token = snapshot.get(WORKERS_KEY), owner_token(snapshot)
    if (not isinstance(resource, dict) or type(resource.get("contract")) is not int
            or resource["contract"] != 1 or token is None or resource.get("owner_token") != str(token)
            or not isinstance(resource.get("workers"), dict) or not resource["workers"]):
        return {"status": "unverified", "reason": "no_verified_model_worker_checkpoint"}
    workers = resource["workers"]
    for key, child in workers.items():
        try:
            uuid.UUID(key)
        except (ValueError, TypeError, AttributeError):
            return {"status": "unverified", "reason": "invalid_model_worker_identity"}
        if (not isinstance(child, dict) or child.get("module") not in MODEL_MODULES
                or any(type(child.get(k)) is not int or child[k] <= 0 for k in ("pid", "parent_pid"))
                or child.get("state") not in ("running", "exited")
                or (child["state"] == "exited" and type(child.get("returncode")) is not int)):
            return {"status": "unverified", "reason": "invalid_model_worker_identity"}
    evidence = {}
    for key, child in workers.items():
        observed = (ProcState.ABSENT if child["state"] == "exited"
                    else await asyncio.to_thread(process_state, child["pid"], child.get("started_at")))
        evidence[key] = {"pid": child["pid"], "module": child["module"], "state": observed.value,
                         "basis": "owned_process_wait" if child["state"] == "exited" else "recorded_process_observation"}
    return {"status": "verified" if all(e["state"] == "absent" for e in evidence.values()) else "unverified",
            "evidence": {"workers": evidence, "signals_sent": False}}


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
    from ttakkari.host_gpu import HostGpu
    from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter

    settings = get_settings()
    launch = _owned_gpu_launch(snapshot)
    observed_intent = False
    if launch is None:
        resource, token = snapshot.get(RESOURCE_KEY), owner_token(snapshot)
        if (not isinstance(resource, dict) or type(resource.get("contract")) is not int
                or resource["contract"] != 1 or token is None or resource.get("owner_token") != str(token)
                or resource.get("kind") != "host_gpu" or resource.get("state") not in ("starting", "cleanup_unverified")
                or not isinstance(resource.get("launch"), dict)):
            return {"status": "unverified", "reason": "no_verified_host_gpu_checkpoint"}
        planned_id = resource["launch"].get("launch_id")
        try:
            uuid.UUID(planned_id)
            current = await asyncio.to_thread(LlmInferenceAdapter._read_launch, HostGpu(settings))
        except Exception as exc:
            logger.warning("Research startup intent observation failed", exc_info=True)
            return {"status": "unverified", "reason": type(exc).__name__}
        if current.get("launch_id") != planned_id:
            return {"status": "unverified", "reason": "startup_launch_missing_or_replaced"}
        launch = _owned_gpu_launch({**snapshot, RESOURCE_KEY: {**resource, "launch": current}})
        if launch is None:
            return {"status": "unverified", "reason": "startup_process_identity_unverified"}
        observed_intent = True
    try:
        result = await asyncio.to_thread(
            LlmInferenceAdapter._cleanup_owned_launch, HostGpu(settings), settings.data_dir, launch,
        )
    except Exception as exc:
        logger.warning("Research orphan resource cleanup could not be verified", exc_info=True)
        return {"status": "unverified", "reason": type(exc).__name__, "launch_id": launch["launch_id"]}
    if observed_intent:
        result = {**result, "startup_identity": launch, "basis": "precommitted_launch_id_and_process_identity"}
    return {"status": "verified" if result.get("success") is True else "unverified", "evidence": result}
