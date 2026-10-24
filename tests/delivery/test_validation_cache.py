"""Unit tests for immutable candidate snapshot identity and validation cache policy."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from ttakkari.delivery.validation import (
    CandidateBindingError,
    CandidateSnapshotIdentity,
    ValidationCachePolicy,
    ValidationKey,
    ValidationReceipt,
)
from ttakkari.delivery.worktree import run_git

TEST_SECRET = b"test-local-validation-secret-key"


def _sample_valid_inputs() -> dict:
    h_render = hashlib.sha256(b"render-v1").hexdigest()
    h_service = hashlib.sha256(b"service-v1").hexdigest()
    h_dep = hashlib.sha256(b"dep-v1").hexdigest()
    h_test = hashlib.sha256(b"test-v1").hexdigest()
    h_conf = hashlib.sha256(b"conf-v1").hexdigest()

    return {
        "candidate_head_sha": "a" * 40,
        "candidate_tree_sha": "b" * 40,
        "candidate_src_tree_sha": "c" * 40,
        "changed_files": {"src/ttakkari/design_system/service.py": h_service},
        "module_source_files": {
            "src/ttakkari/design_system/service.py": h_service,
            "src/ttakkari/design_system/render.py": h_render,
        },
        "transitive_dependency_files": {"src/ttakkari/common.py": h_dep},
        "selected_test_files": {"tests/test_design_system.py": h_test},
        "conftest_helper_files": {"tests/conftest.py": h_conf},
        "selector_args": ["pytest", "-m", "contract", "tests/test_design_system.py"],
        "pyproject_sha256": hashlib.sha256(b"pyproject").hexdigest(),
        "uv_lock_sha256": hashlib.sha256(b"uv.lock").hexdigest(),
        "python_full_version": "3.13.2",
        "platform_tag": "darwin-arm64",
        "pytest_plugins_sha256": hashlib.sha256(b"pytest-plugins").hexdigest(),
        "sanitized_env_sha256": hashlib.sha256(b"sanitized-env").hexdigest(),
        "models_py_sha256": hashlib.sha256(b"models.py").hexdigest(),
        "migration_head": "0067_collector_outbox_retry",
        "closure_complete": True,
        "has_dynamic_imports": False,
    }


def test_candidate_snapshot_identity_capture(tmp_path: Path):
    candidate = tmp_path / "repo"
    candidate.mkdir()
    (candidate / "src" / "ttakkari").mkdir(parents=True)
    (candidate / "src" / "ttakkari" / "__init__.py").write_text("# ttakkari\n")
    (candidate / "review" / "platform-v2").mkdir(parents=True)
    (candidate / "review" / "platform-v2" / "note.md").write_text("# doc\n")

    run_git(["init", "-b", "main"], candidate)
    run_git(["config", "user.name", "Test"], candidate)
    run_git(["config", "user.email", "test@example.com"], candidate)
    run_git(["add", "."], candidate)
    run_git(["commit", "-m", "init"], candidate)

    # 1. Clean checkout
    snap = CandidateSnapshotIdentity.capture(candidate)
    assert len(snap.candidate_head_sha) == 40
    assert len(snap.candidate_tree_sha) == 40
    assert len(snap.candidate_src_tree_sha) == 40
    assert snap.is_clean_source_tree is True
    assert snap.is_clean_audit_docs is True
    assert len(snap.dirty_source_paths) == 0

    # 2. Modify audit doc only -> source remains clean, audit doc is dirty
    (candidate / "review" / "platform-v2" / "note.md").write_text("# doc edited\n")
    snap_doc_dirty = CandidateSnapshotIdentity.capture(candidate)
    assert snap_doc_dirty.is_clean_source_tree is True
    assert snap_doc_dirty.is_clean_audit_docs is False
    assert snap_doc_dirty.dirty_audit_doc_paths == ("review/platform-v2/note.md",)

    # 3. Modify source file -> source is dirty
    (candidate / "src" / "ttakkari" / "__init__.py").write_text("# modified\n")
    snap_src_dirty = CandidateSnapshotIdentity.capture(candidate)
    assert snap_src_dirty.is_clean_source_tree is False
    assert "src/ttakkari/__init__.py" in snap_src_dirty.dirty_source_paths

    # 4. Invalid directory raises CandidateBindingError
    with pytest.raises(CandidateBindingError):
        CandidateSnapshotIdentity.capture(tmp_path / "nonexistent")


def test_validation_key_digest_stability_and_sensitivity():
    inputs1 = _sample_valid_inputs()
    key1 = ValidationKey.create(**inputs1)
    d1 = key1.compute_digest()

    # Identical inputs produce identical digest
    inputs2 = copy.deepcopy(inputs1)
    key2 = ValidationKey.create(**inputs2)
    assert key2.compute_digest() == d1

    # Any single input field change mutates the digest
    mutations = [
        ("candidate_head_sha", "1" * 40),
        ("uv_lock_sha256", hashlib.sha256(b"uv-new").hexdigest()),
        ("pytest_plugins_sha256", hashlib.sha256(b"plugins-new").hexdigest()),
        ("sanitized_env_sha256", hashlib.sha256(b"env-new").hexdigest()),
        ("models_py_sha256", hashlib.sha256(b"models-new").hexdigest()),
        ("migration_head", "0068_next"),
        ("selector_args", ["pytest", "-m", "other"]),
    ]
    for field_name, new_val in mutations:
        mutated = copy.deepcopy(inputs1)
        mutated[field_name] = new_val
        k_mut = ValidationKey.create(**mutated)
        assert k_mut.compute_digest() != d1, (
            f"Digest did not change on mutating {field_name}"
        )


def test_validation_cache_policy_positive_and_negative_reuse(tmp_path, monkeypatch):
    inputs = _sample_valid_inputs()
    candidate = tmp_path / "candidate"
    for field in (
        "changed_files",
        "module_source_files",
        "transitive_dependency_files",
        "selected_test_files",
        "conftest_helper_files",
    ):
        for rel, digest in inputs[field].items():
            path = candidate / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(digest)
            inputs[field][rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    run_git(["init", "-b", "main"], candidate)
    run_git(["config", "user.name", "Test"], candidate)
    run_git(["config", "user.email", "test@example.com"], candidate)
    run_git(["add", "."], candidate)
    run_git(["commit", "-m", "immutable validation inputs"], candidate)
    for field in (
        "changed_files",
        "module_source_files",
        "transitive_dependency_files",
        "selected_test_files",
        "conftest_helper_files",
    ):
        inputs[field] = {
            rel: hashlib.sha256((candidate / rel).read_bytes()).hexdigest()
            for rel in inputs[field]
        }
    snapshot = CandidateSnapshotIdentity.capture(candidate)
    inputs.update(
        candidate_head_sha=snapshot.candidate_head_sha,
        candidate_tree_sha=snapshot.candidate_tree_sha,
        candidate_src_tree_sha=snapshot.candidate_src_tree_sha,
        snapshot=snapshot,
    )
    key = ValidationKey.create(**inputs)
    digest = key.compute_digest()

    receipt = ValidationReceipt.issue(
        key_digest=digest,
        verifier_id="worker-test-1",
        exit_code=0,
        passed_count=20,
        created_at_epoch=1000.0,
        ttl_seconds=3600,
        secret=TEST_SECRET,
    )

    # 1. Exact match within TTL -> can reuse
    reusable, reason = ValidationCachePolicy.can_reuse(
        key, receipt, now_epoch=1500.0, secret=TEST_SECRET
    )
    assert reusable is True
    assert reason == "reusable"

    with monkeypatch.context() as changed_env:
        changed_env.setenv("TTAKKARI_VALIDATION_RUNTIME_INPUT", "changed")
        reusable, reason = ValidationCachePolicy.can_reuse(
            key, receipt, now_epoch=1500.0, secret=TEST_SECRET
        )
        assert reusable is False
        assert reason == "runtime_changed"

    # 2. Dynamic import present -> rejected (unverified closure)
    dynamic_inputs = copy.deepcopy(inputs)
    dynamic_inputs["has_dynamic_imports"] = True
    dynamic_key = ValidationKey.create(**dynamic_inputs)
    reusable, reason = ValidationCachePolicy.can_reuse(
        dynamic_key, receipt, now_epoch=1500.0, secret=TEST_SECRET
    )
    assert reusable is False
    assert reason == "unverified_dynamic_imports"

    # 3. Incomplete closure -> rejected
    incomplete_inputs = copy.deepcopy(inputs)
    incomplete_inputs["closure_complete"] = False
    incomplete_key = ValidationKey.create(**incomplete_inputs)
    reusable, reason = ValidationCachePolicy.can_reuse(
        incomplete_key, receipt, now_epoch=1500.0, secret=TEST_SECRET
    )
    assert reusable is False
    assert reason == "incomplete_dependency_closure"

    # 4. Input key digest mismatch -> rejected
    changed_inputs = copy.deepcopy(inputs)
    changed_inputs["pyproject_sha256"] = hashlib.sha256(
        b"pyproject-changed"
    ).hexdigest()
    changed_key = ValidationKey.create(**changed_inputs)
    reusable, reason = ValidationCachePolicy.can_reuse(
        changed_key, receipt, now_epoch=1500.0, secret=TEST_SECRET
    )
    assert reusable is False
    assert reason == "key_digest_mismatch"

    # 5. Tampered signature -> rejected
    tampered_receipt = ValidationReceipt(
        key_digest=receipt.key_digest,
        verifier_id=receipt.verifier_id,
        exit_code=receipt.exit_code,
        passed_count=receipt.passed_count,
        created_at_epoch=receipt.created_at_epoch,
        ttl_seconds=receipt.ttl_seconds,
        signature="0" * 64,
    )
    reusable, reason = ValidationCachePolicy.can_reuse(
        key, tampered_receipt, now_epoch=1500.0, secret=TEST_SECRET
    )
    assert reusable is False
    assert reason == "signature_invalid"

    # 6. Expired receipt -> rejected
    reusable, reason = ValidationCachePolicy.can_reuse(
        key, receipt, now_epoch=5000.0, secret=TEST_SECRET
    )
    assert reusable is False
    assert reason == "receipt_expired"

    # 7. Failed or empty test receipt -> rejected
    failed_receipt = ValidationReceipt.issue(
        key_digest=digest,
        verifier_id="worker-test-1",
        exit_code=1,
        passed_count=0,
        created_at_epoch=1000.0,
        ttl_seconds=3600,
        secret=TEST_SECRET,
    )
    reusable, reason = ValidationCachePolicy.can_reuse(
        key, failed_receipt, now_epoch=1500.0, secret=TEST_SECRET
    )
    assert reusable is False
    assert reason == "receipt_failed_or_empty"


def test_caller_supplied_hashes_are_not_snapshot_evidence():
    key = ValidationKey.create(**_sample_valid_inputs())
    receipt = ValidationReceipt.issue(
        key_digest=key.compute_digest(),
        verifier_id="review",
        exit_code=0,
        passed_count=1,
        created_at_epoch=1000,
        ttl_seconds=3600,
        secret=TEST_SECRET,
    )
    assert (
        ValidationCachePolicy.can_reuse(
            key,
            receipt,
            now_epoch=1500,
            secret=TEST_SECRET,
        )[0]
        is False
    )


def test_empty_closure_cannot_authorize_reuse():
    inputs = _sample_valid_inputs()
    inputs["transitive_dependency_files"] = {}
    key = ValidationKey.create(**inputs)
    receipt = ValidationReceipt.issue(
        key_digest=key.compute_digest(),
        verifier_id="review",
        exit_code=0,
        passed_count=1,
        created_at_epoch=1000,
        ttl_seconds=3600,
        secret=TEST_SECRET,
    )
    assert (
        ValidationCachePolicy.can_reuse(
            key,
            receipt,
            now_epoch=1500,
            secret=TEST_SECRET,
        )[0]
        is False
    )


def test_dirty_bytes_change_snapshot_identity(tmp_path):
    test_candidate_snapshot_identity_capture(tmp_path)
    candidate = tmp_path / "repo"
    source = candidate / "src/ttakkari/__init__.py"
    a = CandidateSnapshotIdentity.capture(candidate)
    source.write_text("# different dirty bytes\n")
    b = CandidateSnapshotIdentity.capture(candidate)
    assert a != b


def test_capture_rejects_source_changed_after_status(tmp_path, monkeypatch):
    from ttakkari.delivery import validation

    test_candidate_snapshot_identity_capture(tmp_path)
    root = tmp_path / "repo"
    original = validation.subprocess.run
    mutated = False

    def mutate_after_status(args, *pargs, **kwargs):
        nonlocal mutated
        result = original(args, *pargs, **kwargs)
        if "status" in args and not mutated:
            mutated = True
            (root / "src/ttakkari/new.py").write_text("after status\n")
        return result

    monkeypatch.setattr(validation.subprocess, "run", mutate_after_status)
    with pytest.raises(CandidateBindingError, match="snapshot_changed"):
        CandidateSnapshotIdentity.capture(root)
