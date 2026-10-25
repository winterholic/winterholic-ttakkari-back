"""Test Alembic migration 0072_research_lab_v2: upgrade, v1 data migration, downgrade, and re-upgrade."""

import asyncio
import json
import logging
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.research.models import Configuration, Experiment, Run, Study


def get_alembic_config():
    settings = get_settings()
    # These migrations run inside pytest's application process. Loading the CLI
    # .ini also runs fileConfig(), which disables existing application loggers.
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
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
    await asyncio.to_thread(command.upgrade, cfg, "0073_research_reuse_source")


async def test_migration_0073_research_reuse_source_upgrade_downgrade_and_set_null():
    cfg = get_alembic_config()
    sm = sessionmaker()

    # 1. Upgrade to 0073
    await asyncio.to_thread(command.upgrade, cfg, "0073_research_reuse_source")

    # 2. Verify 4 columns exist on research_experiments
    async with sm() as db:
        cols = (
            await db.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'research_experiments'
                      AND column_name LIKE 'source_%'
                    ORDER BY column_name
                    """
                )
            )
        ).scalars().all()
        assert set(cols) == {
            "source_study_id",
            "source_experiment_id",
            "source_configuration_id",
            "source_run_id",
        }

    # 3. Test foreign key and ON DELETE SET NULL behaviour
    std1_id = uuid.uuid4()
    exp1_id = uuid.uuid4()
    cfg1_id = uuid.uuid4()
    run1_id = uuid.uuid4()

    std2_id = uuid.uuid4()
    exp2_id = uuid.uuid4()

    async with sm() as db:
        std1 = Study(id=std1_id, title="Source Study", goal="Lineage test", scope="personal", phase="planning")
        exp1 = Experiment(
            id=exp1_id,
            study_id=std1_id,
            title="Source Exp",
            hypothesis="H1",
            rationale="R1",
            adapter="retrieval_eval",
            adapter_id="retrieval_eval",
            state="completed",
        )
        cfg1 = Configuration(
            id=cfg1_id,
            experiment_id=exp1_id,
            label="Source Cfg",
            role="baseline",
            parameters={"k": 5},
            config_hash="hash1",
        )
        run1 = Run(
            id=run1_id,
            experiment_id=exp1_id,
            configuration_id=cfg1_id,
            study_id=std1_id,
            adapter_id="retrieval_eval",
        )

        std2 = Study(id=std2_id, title="Target Study", goal="Lineage test target", scope="personal", phase="planning")
        exp2 = Experiment(
            id=exp2_id,
            study_id=std2_id,
            title="Derived Exp",
            hypothesis="H2",
            rationale="R2",
            adapter="retrieval_eval",
            adapter_id="retrieval_eval",
            state="queued",
            source_study_id=std1_id,
            source_experiment_id=exp1_id,
            source_configuration_id=cfg1_id,
            source_run_id=run1_id,
        )
        db.add(std1)
        await db.flush()
        db.add(exp1)
        await db.flush()
        db.add(cfg1)
        await db.flush()
        db.add(run1)
        await db.flush()
        db.add(std2)
        await db.flush()
        db.add(exp2)
        await db.commit()

    # Delete source run and verify SET NULL on exp2.source_run_id
    async with sm() as db:
        await db.execute(text("DELETE FROM research_runs WHERE id = :id"), {"id": run1_id})
        await db.commit()
        exp2_row = (
            await db.execute(
                text("SELECT source_run_id, source_configuration_id FROM research_experiments WHERE id = :id"),
                {"id": exp2_id},
            )
        ).mappings().one()
        assert exp2_row["source_run_id"] is None
        assert exp2_row["source_configuration_id"] == cfg1_id

    # 4. Downgrade to 0072 and verify columns dropped
    await asyncio.to_thread(command.downgrade, cfg, "0072_research_lab_v2")
    async with sm() as db:
        cols_after_down = (
            await db.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'research_experiments'
                      AND column_name LIKE 'source_%'
                    """
                )
            )
        ).scalars().all()
        assert len(cols_after_down) == 0

    # 5. Re-upgrade back to 0073
    await asyncio.to_thread(command.upgrade, cfg, "0073_research_reuse_source")
    async with sm() as db:
        cols_after_up = (
            await db.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'research_experiments'
                      AND column_name LIKE 'source_%'
                    """
                )
            )
        ).scalars().all()
        assert len(cols_after_up) == 4


async def test_programmatic_migration_preserves_application_diagnostic_logging(caplog):
    """Migration tests must not disable the application loggers used by later tests."""
    from ttakkari.transfer.diagnostics import log

    marker = f"migration-log-continuity-{uuid.uuid4().hex}"
    caplog.set_level(logging.INFO, logger=log.name)
    await asyncio.to_thread(command.stamp, get_alembic_config(), "0073_research_reuse_source")
    log.info(marker)
    assert any(record.name == log.name and record.getMessage() == marker for record in caplog.records)

