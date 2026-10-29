"""Single-host research ownership, independent of agent RuntimeLease rows.

The OS lock covers adapter execution and cleanup, including DB outages. A free
lock permits recording interruption, never replaying work. External cleanup
requires its own resource checkpoint and guard evidence. No heartbeat timeout
steals a live owner.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import IO

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.models import utcnow

OWNER_KEY = "research_execution_owner"
_RESERVATIONS: dict[str, str | None] = {}


def has_reservations(generation_id: uuid.UUID) -> bool:
    return str(generation_id) in _RESERVATIONS.values()


def probe(token: uuid.UUID):
    # Reuse the generation runtime's nonblocking flock implementation. Lock
    # files are never unlinked: replacing an inode would split ownership.
    return generation._lock(get_settings().home / "runtime" / "research" / f"{token}.lock")


def owner_token(snapshot: dict) -> uuid.UUID | None:
    owner = snapshot.get(OWNER_KEY)
    if not isinstance(owner, dict) or type(owner.get("contract")) is not int or owner["contract"] != 1:
        return None
    try:
        return uuid.UUID(owner["token"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def execution_owner_receipt(row) -> dict:
    """Return a fresh, per-Run observation of the persisted execution owner."""
    snapshot = row.environment_snapshot
    valid_snapshot = isinstance(snapshot, dict)
    snapshot_for_lookup = snapshot if valid_snapshot else {}
    owner = snapshot_for_lookup.get(OWNER_KEY)
    scope = {
        "study_id": str(row.study_id),
        "experiment_id": str(row.experiment_id),
        "run_id": str(row.id),
        "adapter_id": row.adapter_id,
    }
    receipt = {"contract": 1, "scope": scope, "owner": owner,
               "observed_at": utcnow().isoformat()}
    if snapshot is not None and not valid_snapshot:
        return {**receipt, "status": "unverified", "evidence": "environment_snapshot_invalid"}
    if owner is None:
        return {**receipt, "status": "unrecorded", "evidence": "owner_checkpoint_missing"}
    token = owner_token(snapshot)
    if token is None:
        return {**receipt, "status": "unverified", "evidence": "owner_checkpoint_invalid"}
    try:
        handle = probe(token)
    except OSError as exc:
        return {**receipt, "status": "unverified", "evidence": "lock_probe_failed",
                "error_type": type(exc).__name__}
    if handle is None:
        return {**receipt, "status": "owned", "evidence": "os_lock_observed"}
    try:
        handle.close()
    except OSError as exc:
        return {**receipt, "status": "unverified", "evidence": "lock_probe_close_failed",
                "error_type": type(exc).__name__}
    return {**receipt, "status": "released", "evidence": "os_lock_available"}


@dataclass
class ExecutionOwner:
    metadata: dict
    handle: IO

    @classmethod
    def acquire(cls) -> ExecutionOwner:
        token = uuid.uuid4()
        handle = probe(token)
        if handle is None:
            raise RuntimeError("Research ownership token is already locked")
        current = generation.current
        value = cls({"contract": 1, "token": str(token),
                    "generation_id": str(current.id) if current else None,
                    "accepted_at": utcnow().isoformat()}, handle)
        _RESERVATIONS[str(token)] = value.metadata["generation_id"]
        return value

    def snapshot(self, environment: dict | None, *, previous: dict | None = None) -> dict:
        from ttakkari.research.resources import RESOURCE_KEY, WORKERS_KEY

        value = {**(environment or {}), OWNER_KEY: self.metadata}
        for key in (RESOURCE_KEY, WORKERS_KEY, "research_memory_guard"):
            value.pop(key, None)  # only the fenced checkpoint writer mints these fields
            resource = (previous or {}).get(key)
            if isinstance(resource, dict) and resource.get("owner_token") == self.metadata["token"]:
                value[key] = resource
        return value

    def close(self) -> None:
        self.handle.close()
        _RESERVATIONS.pop(self.metadata["token"], None)
