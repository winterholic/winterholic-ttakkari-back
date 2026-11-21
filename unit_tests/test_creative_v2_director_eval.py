"""Sequence layer of the storyboard contract and the CLI semantic reviewer's receipt mapping.

The fake runner below only exercises receipt construction. It is not evidence that any model viewed a frame;
real execution evidence lives in the run report produced by scripts/creative_director_eval.py.
"""

import json
import uuid
from pathlib import Path

import pytest
from test_creative_v2_stage3 import output, request
from test_creative_v2_stage3 import stores as _stores

from ttakkari.creative.v2 import storyboard as sb


@pytest.fixture
def stores(tmp_path):
    return _stores.__wrapped__(tmp_path)


def with_sequences(*groups):
    data = output()
    data["sequences"] = [
        {"sequence_id": f"q{i}", "title": f"시퀀스 {i}", "purpose": "서사 단위", "scene_ids": ids}
        for i, ids in enumerate(groups)
    ]
    return data


def test_sequences_are_saved_in_plan_director(stores):
    plans, _ = stores
    raw = json.dumps(with_sequences(["i0"], ["i1", "i2"]), ensure_ascii=False)
    plan = sb.materialize(
        plans, request(), raw, run_id=str(uuid.uuid4()), model="fixture",
        reviewed_output_sha256=sb.output_hash(raw),
    )
    assert [(q["sequence_id"], q["scene_ids"]) for q in plan["director"]["sequences"]] == [
        ("q0", ["i0"]), ("q1", ["i1", "i2"])
    ]
    assert plans.get(plan["id"])["director"]["sequences"][1]["title"] == "시퀀스 1"


@pytest.mark.parametrize(
    "groups",
    [
        (["i0"], ["i1"]),  # i2 missing
        (["i0", "i1"], ["i1", "i2"]),  # i1 twice
        (["i0", "i1", "i2", "ghost"],),  # unknown scene
    ],
)
def test_sequences_must_cover_every_scene_exactly_once(groups):
    with pytest.raises(ValueError, match="sequences"):
        sb.parse_storyboard(request(), json.dumps(with_sequences(*groups)))


def test_duplicate_sequence_id_is_rejected():
    data = with_sequences(["i0"], ["i1", "i2"])
    data["sequences"][1]["sequence_id"] = "q0"
    with pytest.raises(ValueError, match="duplicate sequence"):
        sb.parse_storyboard(request(), json.dumps(data))


def test_prompt_demands_sequences_and_shot_contract():
    prompt = sb.build_prompt(request())
    assert "sequences" in prompt and "output_contract" in prompt and "quality_criteria" in prompt


# --- reviewer -------------------------------------------------------------


def package(tmp_path):
    frames = []
    for i, (scene, t) in enumerate([("s1", 0.5), ("s1", 1.5), ("s2", 3.5)]):
        path = tmp_path / f"frame-{i:04d}.png"
        path.write_bytes(b"png%d" % i)
        frames.append({
            "scene_id": scene, "frame": i, "path": str(path), "sha256": f"{i:064x}",
            "source_sha256": "a" * 64, "kind": "frame", "start_time": t, "end_time": t,
        })
    return {
        "request_path": str(tmp_path / "review-request.json"),
        "rubric": {
            "criteria": [
                {"id": "intent_alignment", "stage": "semantic_qa"},
                {"id": "motion", "stage": "viewing_qa"},
            ],
            "targets": [
                {"scene_id": "s1", "start_time": 0, "end_time": 2, "narrative_goal": "g", "shots": []},
                {"scene_id": "s2", "start_time": 2, "end_time": 4, "narrative_goal": "g", "shots": []},
            ],
        },
        "frames": frames,
    }


def stream(pkg, answer, read_indices):
    blocks = [
        {"type": "tool_use", "name": "Read", "input": {"file_path": pkg["frames"][i]["path"]}}
        for i in read_indices
    ]
    return [
        {"type": "assistant", "message": {"content": blocks}},
        {"type": "result", "is_error": False, "result": json.dumps(answer),
         "modelUsage": {"claude-fixture": {}}, "session_id": "sess"},
    ]


def row(scene, indices, verdict="pass", uncertainty=0.1, criterion="intent_alignment"):
    return {"criterion_id": criterion, "scene_id": scene, "verdict": verdict,
            "uncertainty": uncertainty, "frame_indices": indices, "rationale": "관찰"}


def execute(tmp_path, answer, read_indices):
    from ttakkari.creative.v2.review_cli_evaluator import make_evaluator

    pkg = package(tmp_path)
    return pkg, make_evaluator(lambda prompt, directory: stream(pkg, answer, read_indices))(pkg, {})


def test_pass_row_carries_only_frames_that_were_read_in_its_scene(tmp_path):
    pkg = package(tmp_path)
    answer = {"criteria": [row("s1", [0, 1, 2]), row("s2", [2])], "defects": []}
    _, receipt = execute(tmp_path, answer, [0, 2])
    assert receipt["stage"] == "semantic_qa"
    s1, s2 = receipt["criteria"]
    assert [e["path"] for e in s1["evidence"]] == [pkg["frames"][0]["path"]]  # frame 1 unread, frame 2 other scene
    assert s1["verdict"] == "pass" and s2["verdict"] == "pass"
    assert receipt["execution"]["frames_read"] == [0, 2]
    assert "viewing_qa" in receipt["execution"]


def test_claim_without_read_frames_is_forced_unknown(tmp_path):
    _, receipt = execute(tmp_path, {"criteria": [row("s1", [0])], "defects": []}, [])
    assert receipt["criteria"][0]["verdict"] == "unknown"
    assert receipt["criteria"][0]["uncertainty"] == 1.0
    assert receipt["criteria"][0]["evidence"] == []


def test_rows_for_unknown_criterion_scene_or_viewing_stage_are_dropped(tmp_path):
    answer = {"criteria": [row("s1", [0], criterion="motion"), row("zz", [0]),
                           row("s1", [0], criterion="made_up"), row("s1", [0])], "defects": []}
    _, receipt = execute(tmp_path, answer, [0])
    assert [c["criterion_id"] for c in receipt["criteria"]] == ["intent_alignment"]


def test_invalid_uncertainty_is_unknown(tmp_path):
    _, receipt = execute(tmp_path, {"criteria": [row("s1", [0], uncertainty=7)], "defects": []}, [0])
    assert receipt["criteria"][0]["verdict"] == "unknown"


def test_defect_needs_read_frame_from_same_scene(tmp_path):
    defect = {"code": "fake_text", "scene_id": "s1", "layer": "captions", "frame_index": 0, "description": "깨진 글자"}
    far = {**defect, "scene_id": "s2"}
    _, receipt = execute(tmp_path, {"criteria": [], "defects": [defect, far]}, [0])
    assert [d["scene_id"] for d in receipt["defects"]] == ["s1"]
    assert receipt["defects"][0]["evidence"][0]["kind"] == "frame"


def test_runner_error_result_raises(tmp_path):
    from ttakkari.creative.v2.review_cli_evaluator import make_evaluator

    pkg = package(tmp_path)
    bad = make_evaluator(lambda p, d: [{"type": "result", "is_error": True, "result": "x"}])
    with pytest.raises(RuntimeError):
        bad(pkg, {})


def test_binding_independence_comes_from_engine_sets_not_reviewer_claims():
    from ttakkari.creative.v2.review_cli_evaluator import GENERATOR_ENGINES, claude_cli_binding

    binding = claude_cli_binding("sonnet", runner=lambda p, d: [])
    assert GENERATOR_ENGINES == {"qwen-local", "codex"}
    assert binding.independent and binding.id == "claude-cli:sonnet"
    assert "codex" in binding.source


def test_receipt_is_scored_by_quality_stage_with_unmocked_evidence_rules(tmp_path):
    """A read pass row for scene s1 reaches a pass verdict in quality._review_stage; unread scene stays unknown."""
    from ttakkari.creative.v2 import quality

    pkg = package(tmp_path)
    for frame in pkg["frames"]:
        frame["sha256"] = quality.file_hash(Path(frame["path"]))
        frame["source_sha256"] = "a" * 64
    rubric = {**pkg["rubric"], "sha256": "r" * 64, "max_pass_uncertainty": 0.25,
              "criteria": [{"id": "intent_alignment", "stage": "semantic_qa", "modalities": ["frame"]}]}
    from ttakkari.creative.v2.review_cli_evaluator import make_evaluator

    answer = {"criteria": [row("s1", [0]), row("s2", [2])], "defects": []}
    receipt = make_evaluator(lambda p, d: stream(pkg, answer, [0]))(pkg, {})
    receipt.update(output_sha256="a" * 64, rubric_sha256="r" * 64,
                   evaluator={"id": "x", "version": "1", "source": "s", "independent": True})
    result, _ = quality._review_stage("semantic_qa", rubric, [receipt], "a" * 64)
    verdicts = {c["scene_id"]: c["verdict"] for c in result["checks"]}
    assert verdicts == {"s1": "pass", "s2": "unknown"}
    assert result["verdict"] == "unknown"
