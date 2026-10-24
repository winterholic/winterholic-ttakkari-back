"""Unit tests for Platform V2 Backend Test Classification Audit (Worker C)
TDD: Verify primary category disjoint sum vs multi-label separation,
distinction between file count (257 dirty / 252 committed / 251 clean worktree / 303 c8f10156),
AST function count, security marker vs file classification heuristic,
and negative controls (NC-BACK-ROOT-FALLBACK, NC-MANIFEST-MISSING, NC-MANIFEST-MALFORMED).
"""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.platform_v2_test_audit import audit_backend_tests


@pytest.fixture(scope="module")
def historical_back_root(tmp_path_factory):
    """Historic 251-file assertions belong to the submitted C source snapshot."""
    root = tmp_path_factory.mktemp("platform-v2-c-historical")
    fixture = REPO_ROOT / "unit_tests/fixtures/platform_v2/c-b155-test-sources.tar.gz"
    with tarfile.open(fixture) as archive:
        archive.extractall(root, filter="data")
    return root


def test_baseline_manifest_257_categories_and_disjoint_sum():
    """Verify that immutable baseline manifest records 257 test files

    with strictly 9 disjoint primary categories summing to 257 (preventing the 257 + 5 => 262 error).
    """
    baseline_manifest_path = (
        REPO_ROOT / "review/platform-v2/20261024/frontend-tests/baseline-manifest.json"
    )
    assert baseline_manifest_path.exists(), "baseline-manifest.json must exist"

    raw = json.loads(baseline_manifest_path.read_text(encoding="utf-8"))
    assert len(raw) == 257, (
        f"Expected 257 test files in baseline manifest, got {len(raw)}"
    )

    categories: dict[str, int] = {}
    for item in raw:
        cat = item.get("category") or item.get("primary_category")
        categories[cat] = categories.get(cat, 0) + 1

    assert len(categories) == 9, (
        f"Expected 9 disjoint primary categories, got {len(categories)}: {list(categories.keys())}"
    )
    assert "Runtime ownership" not in categories, (
        "Runtime ownership must be a multi-label, not a 10th primary category"
    )
    assert sum(categories.values()) == 257, (
        f"Sum of primary categories ({sum(categories.values())}) must equal 257"
    )


def test_clean_worktree_251_files_and_disjoint_sum(historical_back_root):
    """Verify that clean worktree HEAD snapshot (excluding audit unit tests)

    contains 251 test files and 3,016 AST functions, with 9 disjoint categories summing to 251.
    """
    result_clean = audit_backend_tests(
        back_root=historical_back_root, baseline_manifest_path=None
    )

    assert result_clean["test_files_count"] == 251
    assert result_clean["total_test_functions_count"] == 3016
    assert result_clean["coverage_status"] == "complete"

    categories = result_clean["primary_categories"]
    assert len(categories) == 9
    assert sum(categories.values()) == 251


def test_nc_back_root_fallback_negative_control(tmp_path: Path):
    """NC-BACK-ROOT-FALLBACK: When back_root is missing files, they must be recorded as missing/incomplete,

    and NEVER silently borrowed from the shared /back tree.
    """
    empty_back_root = tmp_path / "empty_back"
    empty_back_root.mkdir()

    baseline_manifest_path = (
        REPO_ROOT / "review/platform-v2/20261024/frontend-tests/baseline-manifest.json"
    )
    result = audit_backend_tests(
        back_root=empty_back_root, baseline_manifest_path=baseline_manifest_path
    )

    assert result["coverage_status"] == "incomplete", (
        "Must be marked incomplete when files are missing"
    )
    assert result["missing_files_count"] == 257, (
        "All 257 files must be reported missing from empty root"
    )
    assert result["total_test_functions_count"] == 0, (
        "No functions can be extracted from missing files"
    )

    for tf in result["test_files"]:
        assert tf.get("status") == "missing"
        assert tf.get("test_count") == 0


def test_nc_manifest_missing_and_malformed_negative_control(tmp_path: Path):
    """NC-MANIFEST-MISSING & NC-MANIFEST-MALFORMED:

    Missing or malformed manifest must be marked incomplete and return typed error,
    never falling back to empty scan silently.
    """
    # 1. Missing manifest
    missing_manifest = tmp_path / "nonexistent_manifest.json"
    res_missing = audit_backend_tests(
        back_root=REPO_ROOT, baseline_manifest_path=missing_manifest
    )
    assert res_missing["manifest_status"] == "missing"
    assert res_missing["coverage_status"] == "incomplete"

    # 2. Malformed manifest
    malformed_manifest = tmp_path / "malformed_manifest.json"
    malformed_manifest.write_text("{ broken json: [", encoding="utf-8")
    res_malformed = audit_backend_tests(
        back_root=REPO_ROOT, baseline_manifest_path=malformed_manifest
    )
    assert res_malformed["manifest_status"] == "malformed"
    assert res_malformed["coverage_status"] == "incomplete"


def test_multi_label_separation_and_runtime_ownership(historical_back_root):
    """Verify that runtime_ownership and other cross-cutting concerns are multi-labels,

    without inflating the 9 primary categories.
    """
    result = audit_backend_tests(
        back_root=historical_back_root, baseline_manifest_path=None
    )
    multi_labels = result["multi_labels"]

    assert "runtime_ownership" in multi_labels
    assert multi_labels["runtime_ownership"]["file_count"] == 45
    assert multi_labels["runtime_ownership"]["test_count"] == 945

    # Sum of primary categories must remain exactly 251
    assert sum(result["primary_categories"].values()) == 251


def test_security_marker_vs_security_file_classification(historical_back_root):
    """Verify that security file classification count (31 files) is cleanly separated from:

    1. AST pytest.mark.security count (27 files, 366 function defs)
    2. Runtime collected cases (marked unmeasured/unknown)
    """
    result = audit_backend_tests(
        back_root=historical_back_root, baseline_manifest_path=None
    )
    sec_summary = result["security_audit"]

    assert sec_summary["classified_files_count"] == 31
    assert sec_summary["marker_files_count"] == 27
    assert sec_summary["marker_function_defs_count"] == 366
    assert (
        sec_summary["runtime_collected_cases"] == "unknown (unmeasured in static audit)"
    )


def test_c8f10156_product_baseline_distinction():
    """Verify that product baseline c8f10156 contains 303 test files and 3,539 AST test functions,

    strictly kept distinct from 257 baseline and 251 clean worktree.
    """
    c8_inv_path = (
        REPO_ROOT
        / "review/platform-v2/20261024/frontend-tests/c8f10156-test-inventory.json"
    )
    assert c8_inv_path.exists(), "c8f10156-test-inventory.json must exist"

    c8_data = json.loads(c8_inv_path.read_text(encoding="utf-8"))
    assert c8_data["test_files_count"] == 303
    assert c8_data["total_test_functions_count"] == 3539

    categories = c8_data["primary_categories"]
    assert len(categories) == 9
    assert sum(categories.values()) == 303

    # Diff ledger verification
    diff_ledger_path = (
        REPO_ROOT
        / "review/platform-v2/20261024/frontend-tests/baseline-c8f10156-diff-ledger.json"
    )
    assert diff_ledger_path.exists(), "baseline-c8f10156-diff-ledger.json must exist"
    diff_data = json.loads(diff_ledger_path.read_text(encoding="utf-8"))
    assert diff_data["test_files_delta"]["baseline_committed_count"] == 252
    assert diff_data["test_files_delta"]["target_c8_count"] == 303
    assert diff_data["test_files_delta"]["added_files_count"] == 51
