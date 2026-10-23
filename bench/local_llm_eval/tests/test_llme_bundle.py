"""Sealed bundle commitments and loader contracts.

These tests never need the answers: the mechanism is exercised on synthetic bundles built in tmp_path,
and the committed commitments are checked for consistency. The one test that touches the real bundle only
hashes its bytes and skips on a host that does not have it. The self-check that runs gold, mutants and
negatives against the real bundle is `python3 -m local_llm_eval.bundle_selfcheck`, deliberately outside
repository tests so repository CI cannot see the answers.
"""

import json
import os
import re
from pathlib import Path

import pytest
from local_llm_eval import bundle, codes, sandbox
from local_llm_eval.bundle_paths import (
    BUNDLE_ID,
    COMMITMENTS,
    HOLDOUT_IDS,
    SEALED_ROOT,
)
from local_llm_eval.model import Attempt

HEX = re.compile(r"[0-9a-f]{64}")
TASK_ID = "B4-SGL-H"

# A deliberately tiny task: the loader only needs the four sealed modules and a public tree.
SYNTHETIC = {
    f"tasks/{TASK_ID}/gold.py": "def gold_ends(base):\n    return [dict(base, **{'a.py': 'x = 2\\n'})]\n",
    f"tasks/{TASK_ID}/hidden.py": (
        "from local_llm_eval import codes\n"
        "from local_llm_eval.model import Check\n"
        "def checks():\n"
        "    return [Check('H1', codes.KIND_DELTA, 'R1', probe='rec(\"v\", 1)', judge=lambda o, f: None)]\n"
    ),
    f"tasks/{TASK_ID}/mutants.py": "def negatives():\n    return []\n",
    f"tasks/{TASK_ID}/task.py": (
        "from local_llm_eval import codes\n"
        "from local_llm_eval.model import Stage, Task\n"
        "def build():\n"
        "    stage = Stage('S1', change_class=codes.CHANGE_DIFF, prompt=PROMPTS['S1'], checks=checks(),\n"
        "                  required_change_paths=('a.py',))\n"
        f"    return Task(id='{TASK_ID}', type='single_file', prompt=PROMPTS['S1'], base_files=dict(PUBLIC),\n"
        "                stages=[stage], gold=gold_ends, negatives=negatives())\n"
    ),
    f"tasks/{TASK_ID}/public/prompts.json": json.dumps({"S1": "fix a.py"}),
    f"tasks/{TASK_ID}/public/workspace/a.py": "x = 1\n",
}


def make_bundle(tmp_path: Path, files: dict[str, str] | None = None):
    """Write a synthetic sealed root (0700) and matching commitments; return (root, commitments_path)."""
    files = dict(SYNTHETIC if files is None else files)
    root = tmp_path / "sealed"
    root.mkdir(mode=0o700)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
    for directory in {p.parent for p in root.rglob("*")} | {root}:
        directory.chmod(0o700)
    hashes = {
        bundle.commitment_key(rel): bundle.sha256_bytes(text.encode("utf-8"))
        for rel, text in files.items()
    }
    from local_llm_eval.manifest import manifest_of_files, tree_hash

    base = {"a.py": "x = 1\n"}
    commitments = {
        "schema": bundle.SCHEMA,
        "bundle_id": BUNDLE_ID,
        "hash": "sha256",
        "bundle_sha256": bundle.bundle_digest(hashes),
        "tasks": {TASK_ID: {"base_tree_sha256": tree_hash(manifest_of_files(base))}},
        "files": hashes,
    }
    path = tmp_path / "commitments.json"
    path.write_text(json.dumps(commitments), encoding="utf-8")
    return root, path


# ---- committed commitments ------------------------------------------------------------------


def test_committed_commitments_are_consistent_and_hold_only_hashes_and_counts():
    commitments = bundle.load_commitments(COMMITMENTS)
    assert commitments["bundle_id"] == BUNDLE_ID
    assert commitments["sealed_root"] == str(SEALED_ROOT)
    assert commitments["model_runs_before_seal"] == 0
    assert sorted(commitments["tasks"]) == sorted(HOLDOUT_IDS)
    files = commitments["files"]
    assert all(HEX.fullmatch(digest) for digest in files.values())
    assert HEX.fullmatch(commitments["bundle_sha256"])
    for task_id in HOLDOUT_IDS:
        for module in bundle.SEALED_MODULES:
            assert f"tasks/{task_id}/{module}.py" in files
        assert f"tasks/{task_id}/public/prompts.json" in files
        assert any(p.startswith(f"tasks/{task_id}/public/workspace/") for p in files)
        facts = commitments["tasks"][task_id]
        assert HEX.fullmatch(facts["base_tree_sha256"])
        assert set(facts) == {
            "base_tree_sha256",
            "checks",
            "requirements",
            "negatives",
            "suite_mutants",
            "public_files",
            "sealed_files",
        }
    assert set(files) - {bundle.MANIFEST_NAME} == {
        p for p in files if p.startswith("tasks/")
    }


def test_commitment_keys_reveal_no_fixture_file_names():
    """Workspace file paths would name the holdout's packages and modules, so they are committed by path hash."""
    generic = re.compile(
        r"tasks/B4-[A-Z]{3}-H/(gold|hidden|mutants|task)\.py"
        r"|tasks/B4-[A-Z]{3}-H/public/prompts\.json"
        r"|manifest\.sealed\.json"
    )
    hashed = re.compile(r"tasks/B4-[A-Z]{3}-H/public/workspace/[0-9a-f]{64}")
    for key in bundle.load_commitments(COMMITMENTS)["files"]:
        assert generic.fullmatch(key) or hashed.fullmatch(key), key
    assert bundle.commitment_key("tasks/B4-XXX-H/public/workspace/pkg/mod.py") == (
        "tasks/B4-XXX-H/public/workspace/" + bundle.sha256_bytes(b"pkg/mod.py")
    )
    assert bundle.commitment_key("tasks/B4-XXX-H/gold.py") == "tasks/B4-XXX-H/gold.py"


def test_repository_carries_no_holdout_answer_files():
    """No sealed module, fixture or answer lives in the repository: only the commitments and the loader."""
    repo = Path(__file__).resolve().parents[3]
    package = repo / "bench" / "local_llm_eval"
    names = {p.name for p in package.rglob("*.py")}
    assert not names & {"gold.py", "hidden.py", "mutants.py"}
    assert not list(package.rglob("prompts.json"))
    text = COMMITMENTS.read_text(encoding="utf-8")
    assert "def " not in text and "import " not in text


def test_a_tampered_commitment_list_is_rejected(tmp_path):
    commitments = json.loads(COMMITMENTS.read_text(encoding="utf-8"))
    some = next(iter(commitments["files"]))
    commitments["files"][some] = "0" * 64
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(commitments), encoding="utf-8")
    with pytest.raises(bundle.BundleError) as caught:
        bundle.load_commitments(path)
    assert caught.value.code == codes.INFRA_SEALED_COMMITMENT_MISMATCH


@pytest.mark.skipif(
    not SEALED_ROOT.exists(), reason="the sealed bundle is not on this host"
)
def test_the_real_bundle_matches_the_committed_hashes():
    data, commitments = bundle.verified_bundle()
    assert len(data) == len(commitments["files"])
    assert oct(os.stat(SEALED_ROOT).st_mode & 0o777) == "0o700"


# ---- verification mechanism on synthetic bundles ---------------------------------------------


def test_a_matching_bundle_loads_and_exports_only_the_public_part(tmp_path):
    root, commitments_path = make_bundle(tmp_path)
    data, _ = bundle.verified_bundle(root, commitments_path)
    assert len(data) == len(SYNTHETIC)
    task = bundle.load_task(TASK_ID, root, commitments_path)
    assert task.split == "holdout" and task.id == TASK_ID
    assert (
        task.meta["sealed_bundle_sha256"]
        == json.loads(commitments_path.read_text())["bundle_sha256"]
    )
    out = tmp_path / "workspace"
    prompts = bundle.export_public(TASK_ID, out, root, commitments_path)
    assert prompts == {"S1": "fix a.py"}
    assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["a.py"]


def test_a_changed_file_is_an_infra_error_and_nothing_runs(tmp_path):
    root, commitments_path = make_bundle(tmp_path)
    gold = root / f"tasks/{TASK_ID}/gold.py"
    gold.write_text(gold.read_text() + "# one byte later\n")
    with pytest.raises(bundle.BundleError) as caught:
        bundle.load_task(TASK_ID, root, commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_COMMITMENT_MISMATCH


def test_extra_and_missing_files_are_commitment_mismatches(tmp_path):
    root, commitments_path = make_bundle(tmp_path)
    (root / "stray.txt").write_text("x")
    (root / "stray.txt").chmod(0o600)
    with pytest.raises(bundle.BundleError) as caught:
        bundle.verified_bundle(root, commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_COMMITMENT_MISMATCH
    (root / "stray.txt").unlink()
    (root / f"tasks/{TASK_ID}/mutants.py").unlink()
    with pytest.raises(bundle.BundleError) as caught:
        bundle.verified_bundle(root, commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_COMMITMENT_MISMATCH


def test_group_or_other_access_is_an_infra_permission_error(tmp_path):
    root, commitments_path = make_bundle(tmp_path)
    root.chmod(0o750)
    with pytest.raises(bundle.BundleError) as caught:
        bundle.verified_bundle(root, commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_PERMISSIONS
    root.chmod(0o700)
    leaf = root / f"tasks/{TASK_ID}/gold.py"
    leaf.chmod(0o644)
    with pytest.raises(bundle.BundleError) as caught:
        bundle.verified_bundle(root, commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_PERMISSIONS


def test_a_symlink_inside_the_bundle_is_refused(tmp_path):
    root, commitments_path = make_bundle(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    (root / "linked.py").symlink_to(outside)
    with pytest.raises(bundle.BundleError) as caught:
        bundle.verified_bundle(root, commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_COMMITMENT_MISMATCH


def test_a_missing_bundle_is_infra_sealed_missing(tmp_path):
    _, commitments_path = make_bundle(tmp_path)
    with pytest.raises(bundle.BundleError) as caught:
        bundle.verified_bundle(tmp_path / "nowhere", commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_MISSING


def test_the_executed_bytes_are_the_verified_bytes(tmp_path):
    """A file swapped after verification cannot change what is built: the loader runs the bytes it hashed."""
    root, commitments_path = make_bundle(tmp_path)
    data, commitments = bundle.verified_bundle(root, commitments_path)
    gold = root / f"tasks/{TASK_ID}/gold.py"
    gold.write_text("raise RuntimeError('swapped after verification')\n")
    task = bundle.task_from_verified(TASK_ID, data, commitments)
    assert task.gold(task.base_files)[0]["a.py"] == "x = 2\n"


def test_a_changed_public_tree_breaks_the_pinned_base_tree(tmp_path):
    files = dict(SYNTHETIC)
    files[f"tasks/{TASK_ID}/public/workspace/a.py"] = "x = 99\n"
    root, commitments_path = make_bundle(tmp_path, files)
    # commitments pin the base tree of the original public file, so a consistent re-hash still fails here
    with pytest.raises(bundle.BundleError) as caught:
        bundle.load_task(TASK_ID, root, commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_COMMITMENT_MISMATCH


# ---- evaluator entry point -------------------------------------------------------------------


def test_holdout_evaluation_reports_infra_for_a_bad_bundle_without_executing(
    tmp_path, monkeypatch
):
    from local_llm_eval import evaluator

    root, commitments_path = make_bundle(tmp_path)
    (root / f"tasks/{TASK_ID}/task.py").write_text("raise SystemExit(0)\n")
    monkeypatch.setattr(
        sandbox, "availability", lambda: sandbox.Availability(True, "mock readiness")
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("nothing may be evaluated when the commitments fail")

    monkeypatch.setattr(evaluator, "evaluate_attempt", forbidden)
    result = bundle.evaluate_holdout(
        TASK_ID, Attempt("a", TASK_ID, "p", 1), root, commitments_path
    )
    assert result.verdict == codes.INFRA and not result.evaluated
    assert result.fail_code == codes.INFRA_SEALED_COMMITMENT_MISMATCH
    assert result.stages == [] and "sealed_bundle_error" in result.flags


def test_isolation_is_checked_before_the_bundle_is_even_read(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE", "measured unavailable")

    def never(*args, **kwargs):
        raise AssertionError("the bundle must not be read without OS isolation")

    monkeypatch.setattr(bundle, "verified_bundle", never)
    result = bundle.evaluate_holdout(TASK_ID, Attempt("a", TASK_ID, "p", 1))
    assert result.verdict == codes.INFRA
    assert result.fail_code == "INFRA_ISOLATION_UNAVAILABLE" and not result.evaluated


def test_redacted_evaluation_has_no_details():
    from local_llm_eval.model import Evaluation, StageResult

    stage = StageResult(
        "S1",
        codes.FAIL,
        "FAIL_HIDDEN",
        checks=[
            {"id": "H1", "passed": False, "detail": "expected 42 got 41", "metrics": {}}
        ],
        detail="expected 42 got 41",
    )
    out = bundle.redact_evaluation(
        Evaluation(codes.FAIL, "FAIL_HIDDEN", [], [], [stage])
    )
    assert "42" not in json.dumps(out) and "detail" not in json.dumps(out)
    assert out["stages"][0]["checks"] == [{"id": "H1", "passed": False}]


def test_holdout_ids_are_the_nine_h_tasks_and_none_is_in_the_dev_registry():
    from local_llm_eval.tasks import REGISTRY

    assert len(HOLDOUT_IDS) == 9 and all(t.endswith("-H") for t in HOLDOUT_IDS)
    assert not set(HOLDOUT_IDS) & set(REGISTRY)
