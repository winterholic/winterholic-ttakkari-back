"""A database left at multiple branch heads must not pass a single-head contract."""
from pathlib import Path

import pytest

from ttakkari import generation_deployment as deployment
from ttakkari.deployment import JobStore


def database_with_heads(heads):
    class Rows:
        def all(self):
            return heads

        def __iter__(self):
            return iter(heads)

    class Database:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def scalar(self, statement):
            return heads[0]

        async def scalars(self, statement):
            return Rows()

    return Database


@pytest.mark.parametrize("heads", [
    ["0068_library_intent", "0070_background_consumer_queue"],
    ["0070_background_consumer_queue", "0068_library_intent"],
    ["0071_library_platform_v2_merge", "unknown_branch"],
    ["0074_collection_single_library", "0073_research_reuse_source"],
])
async def test_multiple_database_heads_refuse_deployment_without_choosing_first(tmp_path, monkeypatch, heads):
    Database = database_with_heads(heads)
    monkeypatch.setattr(deployment, "sessionmaker", lambda: Database)
    controller = deployment.GenerationController(JobStore(tmp_path))
    contract = deployment.Releases.contract(Path(__file__).resolve().parents[1])
    with pytest.raises(deployment.StageError, match="multiple database schema heads") as raised:
        await controller.schema_plan(contract)
    assert raised.value.stage == "schema_contract"


@pytest.mark.parametrize(("heads", "expected"), [
    (["0068_library_intent"], ("online", "0068_library_intent")),
    (["0071_library_platform_v2_merge"], ("online", "0071_library_platform_v2_merge")),
    (["0072_collection_missions"], ("online", "0072_collection_missions")),
    (["0073_collection_mission_runs"], ("online", "0073_collection_mission_runs")),
    (["0074_collection_single_library"], ("online", "0074_collection_single_library")),
    (["0072_research_lab_v2"], ("online", "0072_research_lab_v2")),
    (["0073_research_reuse_source"], ("online", "0073_research_reuse_source")),
    (["0075_collection_research_merge"], ("current", "0075_collection_research_merge")),
    (["unknown_branch"], ("idle", "unknown_branch")),
    ([], ("idle", None)),
])
async def test_single_or_missing_database_head_keeps_existing_policy(tmp_path, monkeypatch, heads, expected):
    Database = database_with_heads(heads)
    monkeypatch.setattr(deployment, "sessionmaker", lambda: Database)
    controller = deployment.GenerationController(JobStore(tmp_path))
    contract = deployment.Releases.contract(Path(__file__).resolve().parents[1])
    assert await controller.schema_plan(contract) == expected
