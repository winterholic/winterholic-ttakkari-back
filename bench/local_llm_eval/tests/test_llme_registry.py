"""Registry, manifest consistency, negative-control coverage, holdout absence."""

import json
from pathlib import Path

import pytest
from local_llm_eval import codes
from local_llm_eval.tasks import REGISTRY

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "docs/design/local-llm-worker-refactor/benchmark-manifest-draft.json"
TASK_IDS = sorted(REGISTRY)


def built():
    return {task_id: REGISTRY[task_id]() for task_id in TASK_IDS}


def test_only_the_nine_dev_tasks_exist_and_no_holdout_is_in_the_repository():
    assert TASK_IDS == sorted(
        f"B4-{t}-D"
        for t in ("EXP", "SGL", "MUL", "API", "DBG", "REF", "TST", "REC", "LNG")
    )
    package = Path(__file__).resolve().parents[1]
    names = [p.name.lower() for p in package.rglob("*")]
    assert not any("holdout" in n or "sealed" in n for n in names)
    assert not any(task_id.endswith("-H") for task_id in REGISTRY)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_task_definition_is_well_formed(task_id):
    task = REGISTRY[task_id]()
    ids = [c.id for s in task.stages for c in s.checks]
    assert len(ids) == len(set(ids)) and task.prompt.strip()
    assert all(
        s.checks and any(c.kind == codes.KIND_DELTA for c in s.checks)
        for s in task.stages
    )
    assert all(
        s.change_class in (codes.CHANGE_DIFF, codes.CHANGE_READ_ONLY)
        for s in task.stages
    )
    assert task.gold is not None and task.alt_gold is not None
    assert task.gold(task.base_files) != task.alt_gold(task.base_files)
    assert all(
        "__pycache__" not in p and not p.endswith(".pyc") for p in task.base_files
    )
    assert (
        task.gold(task.base_files)[0] != task.base_files
        or task.stages[0].change_class == codes.CHANGE_READ_ONLY
    )


def test_change_classes_match_the_spec():
    expected = {"B4-EXP-D": [codes.CHANGE_READ_ONLY]}
    for task_id, task in built().items():
        classes = [s.change_class for s in task.stages]
        if task_id in expected:
            assert classes == expected[task_id]
        elif task_id == "B4-LNG-D":
            assert classes == [codes.CHANGE_DIFF] * 4 + [codes.CHANGE_READ_ONLY]
        else:
            assert classes == [codes.CHANGE_DIFF]


def test_every_negative_control_is_implemented_at_least_once():
    seen = {n.id for task in built().values() for n in task.negatives}
    assert seen >= set(codes.NEGATIVE_CONTROLS), set(codes.NEGATIVE_CONTROLS) - seen


def test_manifest_required_controls_and_requirement_counts_match_the_implementation():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in manifest["tasks"]}
    for task_id, task in built().items():
        entry = by_id[task_id]
        implemented = {n.id for n in task.negatives}
        assert set(entry["required_negative_controls"]) <= implemented, task_id
        requirements = {
            c.requirement for s in task.stages for c in s.checks if c.requirement
        }
        extra = 2 if task_id == "B4-LNG-D" else 0
        assert len(requirements) + extra == entry["n_requirements"], (
            task_id,
            sorted(requirements),
        )


def test_base_trees_are_text_with_unix_newlines():
    for task in built().values():
        for path, content in task.base_files.items():
            assert isinstance(content, str) and "\r" not in content, path


def test_sealed_baseline_hash_in_the_manifest_matches_the_task_definition():
    """The start-state baseline the evaluator compares against is pinned in the committed manifest."""
    from local_llm_eval.manifest import manifest_of_files, tree_hash

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    pinned = {
        t["id"]: t["base_tree_sha256"]
        for t in manifest["tasks"]
        if "base_tree_sha256" in t
    }
    assert set(pinned) == set(TASK_IDS)
    for task_id, task in built().items():
        assert tree_hash(manifest_of_files(task.base_files)) == pinned[task_id], task_id
