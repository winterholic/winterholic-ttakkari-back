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


async def test_catalog_loader_full_112_matching():
    """Verify that catalog_loader loads all 112 methods, all parameters, and relations with 100% integrity."""
    from pathlib import Path
    import json
    from sqlalchemy import select
    from ttakkari.research.models import Method, MethodRelation, TaxonomyTerm

    seed_dir = Path(__file__).parent.parent / "src/ttakkari/research/catalog_seed"
    all_seed_methods = []
    total_seed_relations = 0
    total_seed_parameters = 0
    for p in sorted(seed_dir.glob("*.json")):
        if p.name == "taxonomy.json":
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for m in data.get("methods", []):
            all_seed_methods.append(m)
            total_seed_relations += len(m.get("relations", []))
            total_seed_parameters += len(m.get("parameters", []))

    assert len(all_seed_methods) == 112, "Seed files must contain exactly 112 methods"
    assert total_seed_parameters == 185, "Seed files must contain exactly 185 parameters"

    sm = sessionmaker()
    async with sm() as db:
        stats = await catalog_loader.load_catalog_seeds(db)
        await db.commit()

        assert stats["methods"] == 112
        assert stats["terms"] > 0
        assert stats["relations"] == total_seed_relations

        # Verify DB records
        db_methods = (
            await db.execute(select(Method).where(Method.scope == "system"))
        ).scalars().all()
        assert len(db_methods) == 112

        db_slugs = {m.slug: m for m in db_methods}
        for seed_m in all_seed_methods:
            slug = seed_m["slug"]
            assert slug in db_slugs, f"Missing method slug in DB: {slug}"
            db_m = db_slugs[slug]
            assert db_m.name_ko == (seed_m.get("name_ko") or seed_m.get("name"))
            assert db_m.category == seed_m.get("category", "general")
            assert db_m.parameters == seed_m.get("parameters", [])
            assert db_m.targets == seed_m.get("targets", [])
            assert db_m.goals == seed_m.get("goals", [])
            assert db_m.layers == seed_m.get("layers", [])

        # Verify relations
        db_relations = (
            await db.execute(
                select(MethodRelation).where(MethodRelation.scope == "system")
            )
        ).scalars().all()
        assert len(db_relations) == total_seed_relations
        id_to_slug = {m.id: m.slug for m in db_methods}
        db_rel_tuples = {
            (id_to_slug[rel.from_method_id], id_to_slug[rel.to_method_id], rel.relation_type)
            for rel in db_relations
        }
        seed_rel_tuples = {
            (m["slug"], r.get("target") or r.get("to_slug"), r.get("type") or r.get("relation_type", "combines_with"))
            for m in all_seed_methods
            for r in m.get("relations", [])
        }
        assert db_rel_tuples == seed_rel_tuples


async def test_readiness_strict_partial_support_for_unwhitelisted_and_unsupported_params():
    """Verify that methods not in adapter whitelist or with unsupported params are strictly partially_supported."""
    from ttakkari.research.adapters import get_adapter_registry

    reg = get_adapter_registry(populate_builtins=True)

    # 1. Method with adapter hint 'tts_synthesis' but not in whitelist (e.g. tts_kokoro_pipeline_tuning)
    kokoro_method = {
        "slug": "tts_kokoro_pipeline_tuning",
        "lifecycle_status": "validated",
        "adapter_hints": ["tts_synthesis"],
        "parameters": [
            {"key": "inference_backend"},
            {"key": "sample_rate"},
        ],
        "metrics": ["rtf", "latency"],
    }
    st_kokoro = compute_execution_status(kokoro_method, adapter_registry=reg, scope="personal")
    # Must NOT be immediately_executable or tool_integrated
    assert st_kokoro.status == "partially_supported"
    assert st_kokoro.is_executable is True
    assert "inference_backend" in st_kokoro.unsupported_params
    assert "sample_rate" in st_kokoro.unsupported_params

    # 2. Whitelisted method with 100% matched params -> immediately_executable
    whitelisted_norm = {
        "slug": "tts_korean_text_normalization",
        "lifecycle_status": "validated",
        "adapter_hints": ["tts_synthesis"],
        "parameters": [
            {"key": "handle_measure_units"},
            {"key": "english_acronym_mode"},
        ],
        "metrics": ["rtf", "normalized_char_delta"],
    }
    st_norm = compute_execution_status(whitelisted_norm, adapter_registry=reg, scope="personal")
    assert st_norm.status == "immediately_executable"
    assert len(st_norm.unsupported_params) == 0

    # 3. Method with unmapped adapter hint -> adapter_required
    unmapped_method = {
        "slug": "vlm_document_ocr",
        "lifecycle_status": "validated",
        "adapter_hints": ["vlm_eval_adapter"],
        "parameters": [{"key": "resolution"}],
        "metrics": ["accuracy"],
    }
    st_unmapped = compute_execution_status(unmapped_method, adapter_registry=reg, scope="personal")
    assert st_unmapped.status == "adapter_required"
    assert st_unmapped.is_executable is False


def test_readiness_fail_closed_when_supported_methods_undeclared_or_empty():
    """Defect F-1: adapter with missing or empty supported_methods must fail-closed and not grant Full Support."""
    from ttakkari.research.adapters.base import AdapterEnvironmentStatus

    # Mock adapter with check_environment returning ready, but missing supported_methods and supported_parameters
    class MockAdapterMissingDeclarations:
        id = "retrieval_eval"

        def check_environment(self, scope="personal"):
            return AdapterEnvironmentStatus(adapter_id="retrieval_eval", ready=True, execution_mode="in_process")

    class MockRegistry:
        def get_adapter(self, hint):
            if hint == "retrieval_eval":
                return MockAdapterMissingDeclarations()
            return None

    method = {
        "slug": "unlisted_demo",
        "lifecycle_status": "validated",
        "adapter_hints": ["retrieval_eval"],
        "parameters": [],
        "metrics": ["score"],
    }

    # 1. When supported_methods attribute is completely omitted
    reg = MockRegistry()
    st = compute_execution_status(method, adapter_registry=reg, scope="personal")
    # Must NOT be immediately_executable or tool_integrated
    assert st.status != "immediately_executable"
    assert st.status != "tool_integrated"
    assert st.status == "partially_supported"
    assert "supported_methods" in st.unavailable_reason or "직접 지원" in st.unavailable_reason

    # 2. When supported_methods is explicitly empty list []
    class MockAdapterEmptyDeclarations:
        id = "retrieval_eval"
        supported_methods = []
        supported_parameters = []

        def check_environment(self, scope="personal"):
            return AdapterEnvironmentStatus(adapter_id="retrieval_eval", ready=True, execution_mode="in_process")

    class MockEmptyRegistry:
        def get_adapter(self, hint):
            return MockAdapterEmptyDeclarations()

    st_empty = compute_execution_status(method, adapter_registry=MockEmptyRegistry(), scope="personal")
    assert st_empty.status != "immediately_executable"
    assert st_empty.status != "tool_integrated"
    assert st_empty.status == "partially_supported"

