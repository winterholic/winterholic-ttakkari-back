"""Both published schema branches must converge without rewriting their history."""
import json
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from scripts.platform_v2_inventory import classify_migration

ROOT = Path(__file__).resolve().parents[1]
MERGE = "0075_collection_research_merge"
COLLECTION = (
    "0072_collection_missions",
    "0073_collection_mission_runs",
    "0074_collection_single_library",
)
RESEARCH = ("0072_research_lab_v2", "0073_research_reuse_source")


def scripts():
    return ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))


def test_single_head_preserves_both_published_branch_identities():
    graph = scripts()
    assert graph.get_heads() == [MERGE]
    assert set(graph.get_revision(MERGE).down_revision) == {COLLECTION[-1], RESEARCH[-1]}
    for chain in (COLLECTION, RESEARCH):
        for parent, child in zip(("0071_library_platform_v2_merge",) + chain, chain):
            assert graph.get_revision(child).down_revision == parent
    contract = json.loads((ROOT / "deploy/generation-contract.json").read_text())
    assert contract["schema_revision"] == MERGE
    assert set(COLLECTION + RESEARCH) <= set(contract["online_migration_from"])


@pytest.mark.parametrize(("existing", "missing"), [
    (COLLECTION, RESEARCH), (RESEARCH, COLLECTION),
])
def test_upgrade_from_each_branch_only_plans_missing_branch(existing, missing):
    # Match ScriptDirectory._upgrade_revs: an upgrade must also traverse
    # unapplied sibling branches, not only descendants of the current tip.
    planned = {
        revision.revision
        for revision in scripts().iterate_revisions(MERGE, existing[-1], implicit_base=True)
    }
    assert planned == {MERGE, *missing}
    assert not set(existing) & planned


@pytest.mark.parametrize(("revision", "owner"), [
    (MERGE, "core.generation_deploy"),
    (RESEARCH[-1], "domain.research_lab"),
    ("9999_unknown_migration", "unmatched"),
])
def test_integrated_migration_inventory_uses_the_owning_domain(revision, owner):
    assert classify_migration(revision) == owner
