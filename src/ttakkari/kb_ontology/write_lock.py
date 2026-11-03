"""Serialize short identity writes within an ontology namespace, across processes.

Extraction/model work stays outside the transaction. A namespace lock also fixes opposite
entity-order deadlocks; separate locks per entity would retain those cycles. PostgreSQL
automatically releases it on commit/rollback, including failed or cancelled jobs.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_identity_writes(db: AsyncSession, tenant_id: str) -> None:
    # Resolver unit tests use an independent, single-writer SQLite in-memory database.
    # Distributed identity serialization is a PostgreSQL runtime contract.
    if db.get_bind().dialect.name == "sqlite":
        return
    # Both visibility classes can resolve the same global identity, so visibility is not
    # part of the key. Tenant namespaces remain independent. Reentrant within a transaction.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:namespace, 0))"),
        {"namespace": f"kb-ontology-identity:{tenant_id}"},
    )
