"""Unit and negative-control regression tests for Worker A's runtime/delivery evidence extractor."""

from __future__ import annotations

import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest

EVIDENCE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = EVIDENCE_DIR / "A-extract-runtime-delivery-evidence.py"


def _load_extractor():
    spec = importlib.util.spec_from_file_location("a_extractor", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["a_extractor"] = module
    spec.loader.exec_module(module)
    return module


def test_git_topology_and_snapshot_binding_are_dynamically_computed():
    mod = _load_extractor()
    evidence = mod.build_evidence(mod.REPO_ROOT)

    assert evidence["schema_version"] == "platform-v2-worker-a-evidence-v2"

    snap = evidence["snapshot_binding"]
    assert (
        snap["candidate_head_sha"] is not None and len(snap["candidate_head_sha"]) == 40
    )
    assert (
        snap["candidate_tree_sha"] is not None and len(snap["candidate_tree_sha"]) == 40
    )
    assert (
        snap["candidate_src_ttakkari_tree_sha"] is not None
        and len(snap["candidate_src_ttakkari_tree_sha"]) == 40
    )
    assert snap["audited_commit_sha"] == "c8f10156845d007ec6496a65d6d8417be19260bd"
    assert (
        snap["audited_src_ttakkari_tree_sha"]
        == "f2e5572ce8d039341eacc4dad5a70db03c61553b"
    )
    assert isinstance(snap["dirty_manifest"]["entries"], list)
    assert isinstance(snap["dirty_manifest"]["dirty_audit_doc_paths"], list)
    assert snap["dirty_manifest"]["is_clean_source_tree"] is True
    assert isinstance(snap["dirty_manifest"]["is_clean_audit_docs"], bool)
    assert snap["import_binding"]["is_bound_to_worktree_src"] is True
    assert (
        Path(snap["import_binding"]["actual_ttakkari_init_file"]).resolve()
        == (mod.REPO_ROOT / "src" / "ttakkari" / "__init__.py").resolve()
    )

    topo = evidence["git_topology_607f8bda_vs_c8f10156"]
    assert topo["merge_base_sha"] == "2522d78e29b14ffb29d8536bcf833d832b4f87e1"
    assert topo["left_right_unique_commits"] == [2, 118]
    assert topo["left_unique_commits_in_607f8bda"] == 2
    assert topo["right_unique_commits_in_c8f10156"] == 118
    assert topo["diff_stat_607f8bda_to_c8f10156"]["changed_paths"] == 366
    assert topo["diff_stat_607f8bda_to_c8f10156"]["insertions"] == 86213
    assert topo["diff_stat_607f8bda_to_c8f10156"]["deletions"] == 339
    assert topo["snapshot_at_607f8bda"] == {
        "python_files": 321,
        "source_lines": 71985,
        "migration_files": 62,
    }
    assert topo["snapshot_at_c8f10156"] == {
        "python_files": 385,
        "source_lines": 94439,
        "migration_files": 67,
    }
    assert topo["total_new_tables_in_added_migrations"] == 18
    assert topo["new_tables_by_migration"]["0064_run_executor.py"] == [
        "runtime_process_groups"
    ]

    comp = evidence["completion_status"]
    for stage in ("P0", "P1", "P2", "platform_v2_overall"):
        assert comp[stage].startswith("not_complete")
    assert comp["P3"].startswith("isolated_foundation_candidate_in_progress")

    on_disk = mod.json.loads(mod.OUT_PATH.read_text(encoding="utf-8"))
    # Avoid cyclic self-output/self-commit comparison: compare input/source specifications and metrics
    assert evidence["schema_version"] == on_disk["schema_version"]
    assert (
        evidence["git_topology_607f8bda_vs_c8f10156"]
        == on_disk["git_topology_607f8bda_vs_c8f10156"]
    )
    assert evidence["codebase_metrics"] == on_disk["codebase_metrics"]
    assert evidence["migrations"] == on_disk["migrations"]
    assert evidence["completion_status"] == on_disk["completion_status"]
    assert (
        evidence["snapshot_binding"]["audited_commit_sha"]
        == on_disk["snapshot_binding"]["audited_commit_sha"]
    )
    assert (
        evidence["snapshot_binding"]["audited_src_ttakkari_tree_sha"]
        == on_disk["snapshot_binding"]["audited_src_ttakkari_tree_sha"]
    )


def test_snapshot_binding_negative_controls_reject_invalid_root_dirty_tree_mismatch_and_parse_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    mod = _load_extractor()

    # 1. Invalid repository root
    with pytest.raises(mod.SnapshotBindingError) as exc_info:
        mod.verify_snapshot_binding(tmp_path)
    assert exc_info.value.code == "invalid_repo_root"

    # 2. Tree SHA mismatch negative control
    with pytest.raises(mod.SnapshotBindingError) as exc_info:
        mod.verify_snapshot_binding(
            mod.REPO_ROOT,
            expected_source_tree_sha="0" * 40,
        )
    assert exc_info.value.code == "tree_mismatch"

    # 3. Foreign checkout import in sys.modules negative control
    fake_pkg = types.ModuleType("ttakkari")
    fake_pkg.__file__ = str(
        tmp_path / "other_checkout" / "src" / "ttakkari" / "__init__.py"
    )
    monkeypatch.setitem(sys.modules, "ttakkari", fake_pkg)
    with pytest.raises(mod.SnapshotBindingError) as exc_info:
        mod.verify_import_origin(mod.REPO_ROOT)
    assert exc_info.value.code == "wrong_checkout_import"
    monkeypatch.undo()

    # 4. Dirty source tree negative control
    monkeypatch.setattr(
        mod,
        "compute_dirty_manifest",
        lambda _root=mod.REPO_ROOT: {
            "entries": [{"status": " M", "path": "src/ttakkari/generation.py"}],
            "dirty_source_paths": ["src/ttakkari/generation.py"],
            "dirty_audit_doc_paths": [],
            "other_dirty_paths": [],
            "is_clean_source_tree": False,
        },
    )
    with pytest.raises(mod.SnapshotBindingError) as exc_info:
        mod.verify_snapshot_binding(mod.REPO_ROOT, allow_dirty_source_paths=False)
    assert exc_info.value.code == "dirty_source_tree"
    monkeypatch.undo()

    # 5. Python AST syntax error negative control
    bad_src = tmp_path / "bad_project" / "src" / "ttakkari"
    bad_src.mkdir(parents=True)
    (bad_src / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(mod.SnapshotBindingError) as exc_info:
        mod.analyze_source_graph(bad_src)
    assert exc_info.value.code == "parse_failure"

    # 6. Malformed migration file without `revision = ...` negative control
    bad_repo = tmp_path / "bad_mig_repo"
    bad_migs = bad_repo / "alembic" / "versions"
    bad_migs.mkdir(parents=True)
    (bad_migs / "0001_bad.py").write_text("down_revision = None\n", encoding="utf-8")
    with pytest.raises(mod.SnapshotBindingError) as exc_info:
        mod.analyze_migrations(bad_repo)
    assert exc_info.value.code == "parse_failure"

    # 7. Audit-doc only dirty control: must NOT raise, and preserve dirty_audit_doc_paths
    monkeypatch.setattr(
        mod,
        "compute_dirty_manifest",
        lambda _root=mod.REPO_ROOT: {
            "entries": [
                {
                    "status": " M",
                    "path": "review/platform-v2/20261024/A-runtime-delivery.md",
                }
            ],
            "dirty_source_paths": [],
            "dirty_audit_doc_paths": [
                "review/platform-v2/20261024/A-runtime-delivery.md"
            ],
            "other_dirty_paths": [],
            "is_clean_source_tree": True,
            "is_clean_audit_docs": False,
        },
    )
    doc_dirty_snap = mod.verify_snapshot_binding(
        mod.REPO_ROOT, allow_dirty_source_paths=False
    )
    assert doc_dirty_snap["dirty_manifest"]["is_clean_source_tree"] is True
    assert doc_dirty_snap["dirty_manifest"]["is_clean_audit_docs"] is False
    assert doc_dirty_snap["dirty_manifest"]["dirty_audit_doc_paths"] == [
        "review/platform-v2/20261024/A-runtime-delivery.md"
    ]
    monkeypatch.undo()

    # 8. Transitive dependency dynamic import negative control
    fake_graph = {
        "modules": {
            "creative": {
                "files": ["src/ttakkari/creative/render.py"],
                "depends_on": ["managed_comfy"],
            },
            "managed_comfy": {
                "files": ["src/ttakkari/managed_comfy.py"],
                "depends_on": [],
            },
        },
        "file_hashes": {
            "src/ttakkari/creative/render.py": mod.sha256_bytes(b"render"),
            "src/ttakkari/managed_comfy.py": mod.sha256_bytes(b"comfy"),
        },
        "file_dynamic_imports": {
            "src/ttakkari/managed_comfy.py": ["dynamic_plugin"],
        },
    }
    _visited, _hashes, closure_ok, reasons = mod.compute_transitive_module_closure(
        "creative", fake_graph
    )
    assert closure_ok is False
    assert any("dynamic_import_in_closure:managed_comfy:" in r for r in reasons)


def _sample_valid_cache_inputs(mod) -> dict:
    h1 = mod.sha256_bytes(b"render-v1")
    h2 = mod.sha256_bytes(b"service-v1")
    h_dep = mod.sha256_bytes(b"managed-comfy-v1")
    h_test = mod.sha256_bytes(b"test-creative-v1")
    h_conf = mod.sha256_bytes(b"conftest-v1")
    return {
        "module_id": "creative",
        "candidate_git_head_sha": "c8f10156845d007ec6496a65d6d8417be19260bd",
        "candidate_git_tree_sha": "d224086fed586484437819bbe100c5b4d58195f1",
        "dirty_source_paths": [],
        "changed_file_hashes": {"src/ttakkari/creative/render.py": h1},
        "module_source_file_hashes": {
            "src/ttakkari/creative/render.py": h1,
            "src/ttakkari/creative/service.py": h2,
        },
        "transitive_dependency_file_hashes": {
            "src/ttakkari/managed_comfy.py": h_dep,
        },
        "closure_complete": True,
        "selected_test_file_hashes": {"tests/test_creative_studio.py": h_test},
        "conftest_and_helper_hashes": {"tests/conftest.py": h_conf},
        "selector_args": ["pytest", "-m", "contract", "tests/test_creative_studio.py"],
        "pyproject_sha256": mod.sha256_bytes(b"pyproject-v1"),
        "uv_lock_sha256": mod.sha256_bytes(b"uv-lock-v1"),
        "python_full_version": "3.13.2",
        "platform_tag": "darwin-arm64",
        "pytest_plugins_sha256": mod.sha256_bytes(b"plugins-v1"),
        "sanitized_env_sha256": mod.sha256_bytes(b"env-v1"),
        "models_py_sha256": mod.sha256_bytes(b"models-v1"),
        "alembic_env_sha256": mod.sha256_bytes(b"alembic-env-v1"),
        "migration_file_hashes": {
            "alembic/versions/0067.py": mod.sha256_bytes(b"0067-v1")
        },
        "migration_head": "0067_collector_outbox_retry",
        "generation_contract_sha256": mod.sha256_bytes(b"gen-contract-v1"),
        "run_executor_manifest_sha256": mod.sha256_bytes(b"exec-manifest-v1"),
    }


def test_evidence_cache_key_v2_and_receipt_fail_closed_on_1byte_edits_and_invalid_inputs():
    mod = _load_extractor()
    base_inputs = _sample_valid_cache_inputs(mod)
    bundle_v1 = mod.compute_evidence_cache_key(**base_inputs)

    expected_init = str((mod.REPO_ROOT / "src" / "ttakkari" / "__init__.py").resolve())
    receipt = mod.issue_validation_receipt(
        evidence_key_bundle=bundle_v1,
        verifier_id="verifier-1",
        import_origin_file=expected_init,
        executed_command=["pytest", "-m", "contract", "tests/test_creative_studio.py"],
        exit_code=0,
        passed_count=35,
        created_at_epoch=1000.0,
        ttl_seconds=3600,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )

    ok, reason = mod.verify_validation_receipt(
        receipt,
        base_inputs,
        expected_worktree_src_init=expected_init,
        now_epoch=1500.0,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )
    assert ok is True and reason == "verified_ok"

    # Negative Control 1: 1-byte content change to the SAME changed file path (`render.py`)
    edited_source = copy.deepcopy(base_inputs)
    h1_modified = mod.sha256_bytes(b"render-v2")
    edited_source["changed_file_hashes"]["src/ttakkari/creative/render.py"] = (
        h1_modified
    )
    edited_source["module_source_file_hashes"]["src/ttakkari/creative/render.py"] = (
        h1_modified
    )
    bundle_v2 = mod.compute_evidence_cache_key(**edited_source)
    assert bundle_v2["cache_key"] != bundle_v1["cache_key"]
    ok, reason = mod.verify_validation_receipt(
        receipt,
        edited_source,
        expected_worktree_src_init=expected_init,
        now_epoch=1500.0,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )
    assert ok is False and reason == "mismatch:changed_file_hashes"

    # Negative Control 2: 1-byte change to an untouched file in the module or transitive dependency
    edited_dep = copy.deepcopy(base_inputs)
    edited_dep["transitive_dependency_file_hashes"]["src/ttakkari/managed_comfy.py"] = (
        mod.sha256_bytes(b"managed-comfy-v2")
    )
    ok, reason = mod.verify_validation_receipt(
        receipt,
        edited_dep,
        expected_worktree_src_init=expected_init,
        now_epoch=1500.0,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )
    assert ok is False and reason == "mismatch:transitive_dependency_file_hashes"

    # Negative Control 3: 1-byte change to test file or conftest/helper
    edited_test = copy.deepcopy(base_inputs)
    edited_test["conftest_and_helper_hashes"]["tests/conftest.py"] = mod.sha256_bytes(
        b"conftest-v2"
    )
    ok, reason = mod.verify_validation_receipt(
        receipt,
        edited_test,
        expected_worktree_src_init=expected_init,
        now_epoch=1500.0,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )
    assert ok is False and reason == "mismatch:conftest_and_helper_hashes"

    # Negative Control 4: Selector, Python patch version, or migration file content change
    for field, new_val in (
        ("selector_args", ["pytest", "tests/test_creative_studio.py"]),
        ("python_full_version", "3.13.3"),
        (
            "migration_file_hashes",
            {"alembic/versions/0067.py": mod.sha256_bytes(b"0067-edited")},
        ),
        ("generation_contract_sha256", mod.sha256_bytes(b"gen-contract-v2")),
    ):
        mutated = copy.deepcopy(base_inputs)
        mutated[field] = new_val
        ok, reason = mod.verify_validation_receipt(
            receipt,
            mutated,
            expected_worktree_src_init=expected_init,
            now_epoch=1500.0,
            signing_secret=b"unit-test-only-explicit-verifier-key",
        )
        assert ok is False and reason == f"mismatch:{field}"

    # Negative Control 5: Incomplete closure, dirty source tree, or missing hash raises ValueError / fails closed
    incomplete = copy.deepcopy(base_inputs)
    incomplete["closure_complete"] = False
    with pytest.raises(ValueError, match="incomplete_dependency_closure"):
        mod.compute_evidence_cache_key(**incomplete)
    ok, reason = mod.verify_validation_receipt(
        receipt,
        incomplete,
        expected_worktree_src_init=expected_init,
        now_epoch=1500.0,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )
    assert ok is False and "incomplete_dependency_closure" in reason

    dirty = copy.deepcopy(base_inputs)
    dirty["dirty_source_paths"] = ["src/ttakkari/creative/render.py"]
    with pytest.raises(ValueError, match="dirty_source_tree"):
        mod.compute_evidence_cache_key(**dirty)

    missing_hash = copy.deepcopy(base_inputs)
    missing_hash["models_py_sha256"] = "not-a-valid-sha256"
    with pytest.raises(ValueError, match="invalid_scalar_hash:models_py_sha256"):
        mod.compute_evidence_cache_key(**missing_hash)

    # Negative Control 6: Expired receipt, untrusted import origin, or tampered signature
    ok, reason = mod.verify_validation_receipt(
        receipt,
        base_inputs,
        expected_worktree_src_init=expected_init,
        now_epoch=99999.0,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )
    assert ok is False and reason == "receipt_expired_or_future_dated"

    ok, reason = mod.verify_validation_receipt(
        receipt,
        base_inputs,
        expected_worktree_src_init="/wrong/worktree/src/ttakkari/__init__.py",
        now_epoch=1500.0,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )
    assert ok is False and reason == "untrusted_import_origin"

    tampered = copy.deepcopy(receipt)
    tampered["signature_hmac_sha256"] = "0" * 64
    ok, reason = mod.verify_validation_receipt(
        tampered,
        base_inputs,
        expected_worktree_src_init=expected_init,
        now_epoch=1500.0,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )
    assert ok is False and reason == "signature_mismatch"


def test_change_impact_classifier_promotes_core_schema_conftest_and_unknown_to_broad_gate():
    mod = _load_extractor()
    graph = mod.analyze_source_graph(mod.SRC_ROOT)

    # Module with verified complete closure (design_system) is module-scoped
    ds_impact = mod.classify_change_impact(
        ["src/ttakkari/design_system.py"],
        graph,
    )
    assert ds_impact["gate_mode"] == "module_scoped"
    assert ds_impact["touched_modules"] == ["design_system"]
    assert ds_impact["reasons"] == []

    # Module with dynamic imports in transitive dependencies (creative) fails closed to broad gate
    creative_impact = mod.classify_change_impact(
        ["src/ttakkari/creative/render.py", "tests/test_creative_studio.py"],
        graph,
    )
    assert creative_impact["gate_mode"] == "broad_fail_closed"
    assert any("incomplete_closure:creative:" in r for r in creative_impact["reasons"])

    for broad_path in (
        ["alembic/versions/0064_run_executor.py"],
        ["deploy/generation-contract.json"],
        ["tests/conftest.py"],
        ["src/ttakkari/models.py"],
        ["src/ttakkari/runs/manager.py"],
        [],
    ):
        impact = mod.classify_change_impact(broad_path, graph)
        assert impact["gate_mode"] == "broad_fail_closed"
        assert len(impact["reasons"]) > 0


def test_cache_invalidation_matrix_across_all_input_dimensions():
    """Verify that every supported input change invalidates the EvidenceCacheKey v2 and rejects receipt reuse."""
    mod = _load_extractor()
    base_inputs = _sample_valid_cache_inputs(mod)
    base_bundle = mod.compute_evidence_cache_key(**base_inputs)
    expected_init = str((mod.REPO_ROOT / "src" / "ttakkari" / "__init__.py").resolve())

    receipt = mod.issue_validation_receipt(
        evidence_key_bundle=base_bundle,
        verifier_id="test-verifier",
        import_origin_file=expected_init,
        executed_command=["pytest", "-m", "contract"],
        exit_code=0,
        passed_count=10,
        created_at_epoch=1000.0,
        ttl_seconds=3600,
        signing_secret=b"unit-test-only-explicit-verifier-key",
    )

    invalidation_cases = [
        (
            "product_source_hash",
            "changed_file_hashes",
            {"src/ttakkari/creative/render.py": mod.sha256_bytes(b"render-changed")},
        ),
        ("lock_file", "uv_lock_sha256", mod.sha256_bytes(b"uv-lock-changed")),
        (
            "selector_args",
            "selector_args",
            ["pytest", "-m", "contract", "tests/other.py"],
        ),
        (
            "selected_tests",
            "selected_test_file_hashes",
            {"tests/test_other.py": mod.sha256_bytes(b"test-other")},
        ),
        (
            "config_pyproject",
            "pyproject_sha256",
            mod.sha256_bytes(b"pyproject-changed"),
        ),
        (
            "config_alembic_env",
            "alembic_env_sha256",
            mod.sha256_bytes(b"alembic-env-changed"),
        ),
        (
            "config_generation_contract",
            "generation_contract_sha256",
            mod.sha256_bytes(b"gen-contract-changed"),
        ),
        (
            "plugin_pytest",
            "pytest_plugins_sha256",
            mod.sha256_bytes(b"plugins-changed"),
        ),
        (
            "schema_migration_files",
            "migration_file_hashes",
            {"alembic/versions/0067.py": mod.sha256_bytes(b"0067-changed")},
        ),
        ("schema_migration_head", "migration_head", "0068_next_head"),
        ("schema_models_py", "models_py_sha256", mod.sha256_bytes(b"models-changed")),
        ("env_sanitized", "sanitized_env_sha256", mod.sha256_bytes(b"env-changed")),
    ]

    for label, field_name, new_val in invalidation_cases:
        mutated = copy.deepcopy(base_inputs)
        mutated[field_name] = new_val
        # if changed_file_hashes is changed, also update module_source_file_hashes so compute_evidence_cache_key doesn't fail consistency check
        if field_name == "changed_file_hashes":
            mutated["module_source_file_hashes"]["src/ttakkari/creative/render.py"] = (
                new_val["src/ttakkari/creative/render.py"]
            )

        mutated_bundle = mod.compute_evidence_cache_key(**mutated)
        assert mutated_bundle["cache_key"] != base_bundle["cache_key"], (
            f"{label} did not change cache_key"
        )
        assert mutated_bundle["sha256"] != base_bundle["sha256"], (
            f"{label} did not change sha256"
        )

        ok, reason = mod.verify_validation_receipt(
            receipt,
            mutated,
            expected_worktree_src_init=expected_init,
            now_epoch=1500.0,
            signing_secret=b"unit-test-only-explicit-verifier-key",
        )
        assert ok is False, f"{label} unexpectedly allowed receipt reuse"
        assert f"mismatch:{field_name}" in reason, (
            f"{label} mismatch reason unexpected: {reason}"
        )


def test_receipt_prototype_requires_a_verifier_owned_signing_key():
    import pytest

    mod = _load_extractor()
    bundle = mod.compute_evidence_cache_key(**_sample_valid_cache_inputs(mod))
    with pytest.raises(ValueError, match="signing"):
        mod.issue_validation_receipt(
            evidence_key_bundle=bundle,
            verifier_id="untrusted",
            import_origin_file="irrelevant",
            executed_command=["true"],
            exit_code=0,
            passed_count=999,
            created_at_epoch=1000,
        )
