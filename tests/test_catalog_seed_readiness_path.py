"""Catalog seed -> catalog_loader -> DB -> readiness path (real loader, real adapter registry, no injected results).

The contract test in test_catalog_seed_contract.py checks the JSON. This file checks what the running system
does with it: a deprecated record must stay non-executable after loading, and the hardware-sensitive methods
must keep their explicit tuples and adapter routing once they went through the loader.
"""

import json
from pathlib import Path

from sqlalchemy import select, update

from ttakkari.db import sessionmaker
from ttakkari.research import catalog_loader
from ttakkari.research.adapters.registry import get_adapter_registry
from ttakkari.research.models import Method
from ttakkari.research.readiness import compute_execution_status

SEED_DIR = Path(catalog_loader.__file__).parent / "catalog_seed"
NOT_EXECUTABLE_STATUSES = {"hypothesis_stage", "adapter_required", "environment_unavailable"}


def _seed_method(slug):
    for p in sorted(SEED_DIR.glob("*.json")):
        if p.name == "taxonomy.json":
            continue
        for m in json.loads(p.read_text(encoding="utf-8")).get("methods", []):
            if m["slug"] == slug:
                return m
    raise AssertionError(f"seed method not found: {slug}")


async def _sync(client, auth):
    resp = await client.post("/api/research/catalog/sync?force=true", headers=auth)
    assert resp.status_code == 200, resp.text


async def test_deprecated_kokoro_stays_non_executable_through_loader_and_api(client, auth):
    await _sync(client, auth)
    resp = await client.get("/api/research/methods/tts_kokoro_pipeline_tuning", headers=auth)
    assert resp.status_code == 200, resp.text
    method = resp.json()

    # loader kept the seed lifecycle instead of forcing "validated"
    assert method["lifecycle_status"] == "deprecated"
    # nothing selectable or routable survived loading
    assert method["adapter_hints"] == []
    assert method["parameters"] == []
    assert method["compatibility"]["runtimes"] == []
    assert method["compatibility"]["hardware"] == []
    # readiness result computed by the service from the loaded row
    assert method["is_executable"] is False
    assert method["execution_status"] in NOT_EXECUTABLE_STATUSES

    listing = await client.get("/api/research/methods?lifecycle_status=deprecated&limit=100", headers=auth)
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [i["slug"] for i in items] == ["tts_kokoro_pipeline_tuning"]
    assert all(i["is_executable"] is False for i in items)

    # no live method can reach Kokoro through a relation either
    for item in (await client.get("/api/research/methods?target=tts&limit=100", headers=auth)).json()["items"]:
        detail = (await client.get(f"/api/research/methods/{item['slug']}", headers=auth)).json()
        if item["slug"] == "tts_kokoro_pipeline_tuning":
            continue
        assert all(r["to_method_slug"] != "tts_kokoro_pipeline_tuning" for r in detail["relations"])


async def test_resync_restores_deprecated_lifecycle_on_existing_rows():
    """Rows loaded before the loader honored lifecycle_status must be corrected on the next sync."""
    sm = sessionmaker()
    async with sm() as db:
        await catalog_loader.load_catalog_seeds(db)
        await db.execute(
            update(Method)
            .where(Method.scope == "system", Method.slug == "tts_kokoro_pipeline_tuning")
            .values(lifecycle_status="validated")
        )
        await db.commit()

        await catalog_loader.load_catalog_seeds(db)
        await db.commit()
        row = (
            await db.execute(
                select(Method).where(Method.scope == "system", Method.slug == "tts_kokoro_pipeline_tuning")
            )
        ).scalar_one()
        assert row.lifecycle_status == "deprecated"
        status = compute_execution_status(row, get_adapter_registry())
        assert status.is_executable is False
        assert status.matched_adapter_id is None


async def test_loader_round_trips_explicit_tuples_and_adapter_routing():
    sm = sessionmaker()
    async with sm() as db:
        await catalog_loader.load_catalog_seeds(db)
        await db.commit()
        rows = {
            r.slug: r
            for r in (
                await db.execute(
                    select(Method).where(
                        Method.scope == "system",
                        Method.slug.in_(
                            ["qlora", "kv_cache_quantization", "flash_attention", "speculative_decoding"]
                        ),
                    )
                )
            ).scalars()
        }
    assert set(rows) == {"qlora", "kv_cache_quantization", "flash_attention", "speculative_decoding"}
    for slug, row in rows.items():
        # DB row carries exactly the seed's tuples (nothing dropped or re-derived by the loader)
        assert row.compatibility["combinations"] == _seed_method(slug)["compatibility"]["combinations"]

    reg = get_adapter_registry()
    # QLoRA has no training adapter: external result registration only, never routed to the local LLM executor
    assert rows["qlora"].adapter_hints == ["external_report"]
    qlora = compute_execution_status(rows["qlora"], reg)
    assert qlora.matched_adapter_id == "external_report"
    assert qlora.status not in {"immediately_executable", "tool_integrated"}

    # flash_attention / kv_cache_quantization go to the host llama-server adapter; whether the host is ready
    # depends on the machine, so assert routing and "never adapter_required", not readiness of this host
    for slug in ("flash_attention", "kv_cache_quantization"):
        res = compute_execution_status(rows[slug], reg)
        assert res.matched_adapter_id == "llm_inference"
        assert res.status in {"tool_integrated", "environment_unavailable"}
        assert res.unsupported_params == []

    # speculative decoding: only spec_type is consumed by the adapter; the draft-model keys stay unsupported
    spec = compute_execution_status(rows["speculative_decoding"], reg)
    assert spec.matched_adapter_id == "llm_inference"
    assert "spec_type" not in spec.unsupported_params
    assert {"draft_model", "draft_tokens"} <= set(spec.unsupported_params)
