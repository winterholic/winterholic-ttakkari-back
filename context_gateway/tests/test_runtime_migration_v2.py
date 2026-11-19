from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from context_gateway.apps import _initialize_schema
from context_gateway.auth import hash_credential
from context_gateway.models import APIKey, KeyACL, Project


@pytest.mark.asyncio
async def test_runtime_ddl_adds_v2_columns_and_preserves_legacy_v1_key(db_sessions) -> None:
    factory = db_sessions
    async with factory() as session:
        project = Project(slug=f"migration-{uuid4().hex}", name="Migration fixture")
        key = APIKey(
            label="legacy v1",
            credential_hash=hash_credential("legacy synthetic key"),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            consumed_at=datetime.now(UTC),
        )
        session.add_all([project, key])
        await session.flush()
        acl = KeyACL(key_id=key.id, project_id=project.id, scopes=["read"])
        session.add(acl)
        await session.commit()
        key_id, project_id = key.id, project.id
    engine = factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.exec_driver_sql("ALTER TABLE gateway_api_keys DROP COLUMN credential_type")
        await connection.exec_driver_sql("ALTER TABLE gateway_api_keys DROP COLUMN last_used_at")
        await connection.exec_driver_sql(
            "ALTER TABLE gateway_api_keys DROP COLUMN window_started_at"
        )
        await connection.exec_driver_sql("ALTER TABLE gateway_api_keys DROP COLUMN window_count")
        await connection.exec_driver_sql("ALTER TABLE gateway_key_acls DROP COLUMN snapshot_policy")
        await connection.exec_driver_sql("ALTER TABLE gateway_key_acls DROP COLUMN snapshot_id")

    await _initialize_schema(factory)

    async with factory() as session:
        migrated = await session.get(APIKey, key_id)
        assert migrated is not None
        assert migrated.credential_type == "single_use_v1"
        assert migrated.consumed_at is not None
        assert migrated.last_used_at is None
        migrated_acl = await session.get(KeyACL, (key_id, project_id))
        assert migrated_acl is not None
        assert migrated_acl.snapshot_policy == "latest" and migrated_acl.snapshot_id is None
        columns = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='gateway_api_keys'"
            )
        )
        assert {"credential_type", "last_used_at", "window_count"} <= {row[0] for row in columns}
        trigger = await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_gateway_snapshots_immutable')"
            )
        )
        assert trigger
