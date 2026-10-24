"""Unit tests for Worker A's reproducible runtime/delivery evidence extractor."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

EVIDENCE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = EVIDENCE_DIR / "A-extract-runtime-delivery-evidence.py"


def _load_extractor():
    spec = importlib.util.spec_from_file_location("a_extractor", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extractor_builds_valid_evidence_and_verifies_single_migration_head():
    mod = _load_extractor()
    evidence = mod.build_evidence(mod.REPO_ROOT)

    assert evidence["schema_version"] == "platform-v2-worker-a-evidence-v1"
    assert evidence["git_revisions"]["audited_origin_main_sha"] == "c8f10156845d007ec6496a65d6d8417be19260bd"
    assert evidence["git_revisions"]["live_deployed_sha"] == "9f82c9c16cd604e81ddf43180b0be2e895ec85cb"
    assert evidence["migrations"]["single_head"] is True
    assert evidence["migrations"]["heads"] == ["0067_collector_outbox_retry"]
    assert evidence["migrations"]["migration_file_count"] == 67
    assert evidence["contracts_and_manifests"]["generation_contract"]["run_executor"]["activation"] == "disabled"
    assert evidence["contracts_and_manifests"]["run_executor_manifest"]["activation"] == "disabled"
    assert evidence["contracts_and_manifests"]["maintenance_job_count"] == 15


def test_change_impact_classifier_separates_module_scoped_and_fail_closed_broad_gate():
    mod = _load_extractor()
    graph = mod.analyze_source_graph(mod.SRC_ROOT)

    creative_impact = mod.classify_change_impact(
        ["src/ttakkari/creative/render.py", "tests/test_creative_studio.py"],
        graph,
    )
    assert creative_impact["gate_mode"] == "module_scoped"
    assert creative_impact["touched_modules"] == ["creative"]
    assert creative_impact["reasons"] == []

    schema_impact = mod.classify_change_impact(
        ["alembic/versions/0064_run_executor.py"],
        graph,
    )
    assert schema_impact["gate_mode"] == "broad_fail_closed"
    assert any(r.startswith("broad_gate_trigger:alembic/") for r in schema_impact["reasons"])

    unknown_impact = mod.classify_change_impact(
        ["deploy/generation-contract.json"],
        graph,
    )
    assert unknown_impact["gate_mode"] == "broad_fail_closed"


def test_evidence_cache_key_is_deterministic_and_sensitive_to_contract_changes():
    mod = _load_extractor()
    key_a = mod.compute_evidence_cache_key_preview(
        module_id="creative",
        changed_files=["src/ttakkari/creative/render.py", "src/ttakkari/creative/service.py"],
        dependency_closure_hashes={"b": "222", "a": "111"},
        uv_lock_sha256="lock-1",
        migration_head="0064_run_executor",
        generation_contract_sha256="contract-1",
        suite_selector="creative-suite",
    )
    key_b = mod.compute_evidence_cache_key_preview(
        module_id="creative",
        changed_files=["src/ttakkari/creative/service.py", "src/ttakkari/creative/render.py"],
        dependency_closure_hashes={"a": "111", "b": "222"},
        uv_lock_sha256="lock-1",
        migration_head="0064_run_executor",
        generation_contract_sha256="contract-1",
        suite_selector="creative-suite",
    )
    assert key_a["cache_key"] == key_b["cache_key"]

    key_c = mod.compute_evidence_cache_key_preview(
        module_id="creative",
        changed_files=["src/ttakkari/creative/render.py", "src/ttakkari/creative/service.py"],
        dependency_closure_hashes={"a": "111", "b": "222"},
        uv_lock_sha256="lock-1",
        migration_head="0065_new_head",
        generation_contract_sha256="contract-1",
        suite_selector="creative-suite",
    )
    assert key_a["cache_key"] != key_c["cache_key"]


def test_generated_json_file_matches_extractor_output():
    mod = _load_extractor()
    mod.main()
    assert mod.OUTPUT_PATH.is_file()
    loaded = json.loads(mod.OUTPUT_PATH.read_text(encoding="utf-8"))
    assert loaded["migrations"]["heads"] == ["0067_collector_outbox_retry"]
    assert loaded["codebase_metrics"]["total_python_files"] == 385
