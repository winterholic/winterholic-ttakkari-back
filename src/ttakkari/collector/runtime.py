"""Runtime operator switches stored in the database (CP-S2-10).

`collector_control_enabled` (settings) decides whether the managed-source loop exists in a generation at all and can
only change with a restart. These flags change without one: a pause stops new scheduling and new job claims on the very
next tick, in every generation, and resuming restores them. In-flight deliveries and already stored data are untouched.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from ttakkari.collector.budget_models import CollectorRuntimeFlag

PAUSED = "paused"
PILOT = "pilot"


async def get(db, name: str) -> dict | None:
    row = await db.get(CollectorRuntimeFlag, name)
    return dict(row.value) if row else None


async def put(db, name: str, value: dict) -> None:
    stmt = insert(CollectorRuntimeFlag).values(name=name, value=value, updated_at=datetime.now(UTC))
    await db.execute(stmt.on_conflict_do_update(index_elements=["name"], set_={"value": value, "updated_at": datetime.now(UTC)}))


async def is_paused(db) -> bool:
    return bool((await get(db, PAUSED) or {}).get("paused"))


async def set_paused(db, paused: bool, reason: str) -> dict:
    value = {"paused": paused, "reason": reason[:200], "at": datetime.now(UTC).isoformat()}
    await put(db, PAUSED, value)
    return value
