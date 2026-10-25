"""Test Alembic migration 0072_research_lab_v2: upgrade, v1 data migration, downgrade, and re-upgrade."""

import asyncio
import json
import uuid
import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.config import get_settings


def get_alembic_config():
    settings = get_settings()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


async def test_migration_0072_upgrade_downgrade_and_v1_data_preservation():
    cfg = get_alembic_config()
    sm = sessionmaker()

    # 1. Stamp as 0072 and downgrade to 0071 to drop v2 tables cleanly
    await asyncio.to_thread(command.stamp, cfg, "0072_research_lab_v2")
    await asyncio.to_thread(command.downgrade, cfg, "0071_library_platform_v2_merge")

    # 2. Insert v1 research study and experiment row
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    v1_receipt = {
        "origin": "server_executed",
        "result": {
            "means": {
                "bm25.recall_at_k": 0.85,
                "lexical.recall_at_k": 0.60,
                "bm25.reciprocal_rank": 0.72,
            }
        },
    }

    async with sm() as db:
        await db.execute(
            text(
                """
                INSERT INTO research_studies (id, title, goal, scope, phase, environment, criteria, next_actions, version, created_at, updated_at)
                VALUES (:id, 'v1 Study', 'Test v1 preservation', 'personal', 'planning', '{}', '[]', '[]', 1, NOW(), NOW())
                """
            ),
            {"id": study_id},
        )
        await db.execute(
            text(
                """
                INSERT INTO research_experiments (
                    id, study_id, title, hypothesis, rationale, adapter, state,
                    config, environment, budget, receipt, method_ids, created_at
                ) VALUES (
                    :id, :study_id, 'v1 Experiment Title', 'v1 Hypothesis', 'v1 Rationale',
                    'retrieval_eval', 'completed', '{"k": 5}', '{"env": "test"}', '{"seconds": 30}',
                    :receipt, '[]', NOW()
                )
                """
            ),
            {"id": exp_id, "study_id": study_id, "receipt": json.dumps(v1_receipt)},
        )
        await db.commit()

    # 3. Upgrade to 0072
    await asyncio.to_thread(command.upgrade, cfg, "0072_research_lab_v2")

    # 4. Verify v1 experiment migrated to Configuration, Run, and Measurements
    async with sm() as db:
        # Check Configuration
        cfg_row = (
            await db.execute(
                text("SELECT * FROM research_configurations WHERE experiment_id = :exp_id"),
                {"exp_id": exp_id},
            )
        ).mappings().one()
        assert cfg_row["label"] == "Migrated v1 Config"
        assert cfg_row["role"] == "baseline"

        # Check Run
        run_row = (
            await db.execute(
                text("SELECT * FROM research_runs WHERE experiment_id = :exp_id"),
                {"exp_id": exp_id},
            )
        ).mappings().one()
        assert run_row["execution_origin"] == "migrated_v1"
        assert run_row["status"] == "succeeded"

        # Check Measurements
        meas_rows = (
            await db.execute(
                text("SELECT * FROM research_measurements WHERE experiment_id = :exp_id ORDER BY metric_key"),
                {"exp_id": exp_id},
            )
        ).mappings().all()
        assert len(meas_rows) == 3
        keys = [m["metric_key"] for m in meas_rows]
        assert "bm25.recall_at_k" in keys
        assert "lexical.recall_at_k" in keys
        assert "bm25.reciprocal_rank" in keys

        bm25_m = next(m for m in meas_rows if m["metric_key"] == "bm25.recall_at_k")
        assert bm25_m["value"] == pytest.approx(0.85)

    # 5. Test downgrade back to 0071 cleanly drops new tables
    await asyncio.to_thread(command.downgrade, cfg, "0071_library_platform_v2_merge")
    async with sm() as db:
        # Check table does not exist
        res = (
            await db.execute(
                text(
                    "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'research_methods')"
                )
            )
        ).scalar()
        assert not res

    # 6. Re-upgrade back to head for subsequent tests
    await asyncio.to_thread(command.upgrade, cfg, "0072_research_lab_v2")
