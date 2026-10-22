import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_collector_migration_follows_the_concurrent_delivery_head():
    path = ROOT / "alembic/versions/0063_collector_stage_items.py"
    spec = importlib.util.spec_from_file_location("collector_stage_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    contract = json.loads((ROOT / "deploy/generation-contract.json").read_text())
    assert migration.revision == "0063_collector_stage_items"
    assert migration.down_revision == "0062_delivery_change_sets"
    assert contract["schema_revision"] == migration.revision
    assert migration.down_revision in contract["online_migration_from"]
