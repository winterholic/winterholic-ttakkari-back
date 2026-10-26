"""Single-host research ownership, independent of agent RuntimeLease rows.

The OS lock covers adapter execution and cleanup, including DB outages. A free
lock permits recording interruption, never replaying work or claiming that an
external GPU/TTS child was stopped. No heartbeat timeout steals a live owner.
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

    def snapshot(self, environment: dict | None) -> dict:
        return {**(environment or {}), OWNER_KEY: self.metadata}

    def close(self) -> None:
        self.handle.close()
        _RESERVATIONS.pop(self.metadata["token"], None)
