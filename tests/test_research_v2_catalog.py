"""Tests for Research Lab v2 catalog loader, 6-axis search, facets, and F-axis readiness."""

import pytest
import pytest_asyncio
from ttakkari.db import sessionmaker
from ttakkari.research import catalog_loader
from ttakkari.research.readiness import compute_execution_status


async def test_catalog_loader_idempotent():
    sm = sessionmaker()
    async with sm() as db:
        # Load once
        stats1 = await catalog_loader.load_catalog_seeds(db)
        await db.commit()
        assert stats1["terms"] > 0
        assert stats1["methods"] > 50

        # Load again (idempotent upsert)
        stats2 = await catalog_loader.load_catalog_seeds(db)
        await db.commit()
        assert stats2["terms"] == stats1["terms"]
        assert stats2["methods"] == stats1["methods"]


async def test_catalog_search_filters_and_facets(client, auth):
    # Sync catalog via API
    resp = await client.post("/api/research/catalog/sync?force=true", headers=auth)
    assert resp.status_code == 200

    # 1. Search taxonomy
    tax_resp = await client.get("/api/research/taxonomy", headers=auth)
    assert tax_resp.status_code == 200
    axes = tax_resp.json()["axes"]
    assert "target" in axes
    assert "layer" in axes
    assert "goal" in axes

    # 2. Search methods with query and target filter
    methods_resp = await client.get(
        "/api/research/methods?target=llm&limit=100", headers=auth
    )
    assert methods_resp.status_code == 200
    data = methods_resp.json()
    assert data["total"] > 0
    assert len(data["items"]) > 0

    # Verify facet counts
    facets = data["facets"]
    assert "targets" in facets
    assert "goals" in facets
    assert "execution_status" in facets
    assert facets["targets"].get("llm", 0) > 0

    # Check each item has runtime execution_status
    for item in data["items"]:
        assert "execution_status" in item
        assert "execution_status_label_ko" in item
        assert isinstance(item["is_executable"], bool)


async def test_readiness_axis_f_computation():
    # 1. Hypothesis stage
    hypo_method = {
        "slug": "crazy_idea",
        "lifecycle_status": "hypothesis",
        "parameters": [],
        "metrics": [],
        "adapter_hints": [],
    }
    st1 = compute_execution_status(hypo_method)
    assert st1.status == "hypothesis_stage"
    assert not st1.is_executable

    # 2. Adapter required
    no_adapter_method = {
        "slug": "custom_kernel",
        "lifecycle_status": "validated",
        "parameters": [{"key": "threads", "type": "integer"}],
        "metrics": ["tps"],
        "adapter_hints": ["non_existent_gpu_adapter"],
    }
    st2 = compute_execution_status(no_adapter_method)
    assert st2.status == "adapter_required"
    assert not st2.is_executable

    # 3. Environment unavailable (CUDA on macOS or scope=work for llm_inference)
    llm_work_method = {
        "slug": "kv_quant",
        "lifecycle_status": "validated",
        "parameters": [{"key": "cache_type_k", "type": "enum"}],
        "metrics": ["decode_tps"],
        "adapter_hints": ["llm_inference"],
        "compatibility": {"hardware": ["apple_silicon_metal"]},
    }
    st3 = compute_execution_status(llm_work_method, scope="work")
    assert st3.status == "environment_unavailable"
    assert "호스트 GPU" in (st3.unavailable_reason or "")

    # 4. Immediately executable (retrieval_eval)
    rag_method = {
        "slug": "bm25_plus",
        "lifecycle_status": "validated",
        "parameters": [
            {"key": "bm25_k1", "type": "number"},
            {"key": "bm25_b", "type": "number"},
        ],
        "metrics": ["recall_at_k"],
        "adapter_hints": ["retrieval_eval"],
    }
    st4 = compute_execution_status(rag_method, scope="personal")
    assert st4.status in ("immediately_executable", "partially_supported")
    assert st4.is_executable
