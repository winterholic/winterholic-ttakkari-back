"""Quality contracts; synthetic reviewer receipts are not model execution evidence."""

import copy
import hashlib
import importlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def quality():
    assert importlib.util.find_spec("ttakkari.creative.v2.quality"), (
        "quality module missing"
    )
    return importlib.import_module("ttakkari.creative.v2.quality")


@pytest.fixture
def plan():
    return {
        "id": "p1",
        "revision": 1,
        "aspect": "16:9",
        "profile": {"fps": 24},
        "intent": {"selections": [{"entry_id": "C01.animation"}]},
        "scenes": [
            {
                "scene_id": "s1",
                "start_time": 0,
                "end_time": 2,
                "seconds": 2,
                "narrative_goal": "walk",
                "shots": [{"shot_id": "sh1"}, {"shot_id": "sh2"}],
            },
            {
                "scene_id": "s2",
                "start_time": 2,
                "end_time": 4,
                "seconds": 2,
                "shots": [{"shot_id": "sh3"}],
            },
        ],
    }


@pytest.fixture
def record():
    return {
        "segments": [
            {
                "id": "seg1",
                "scene_ids": ["s1"],
                "run_key": "good-key-1",
                "status": "succeeded",
            },
            {
                "id": "seg2",
                "scene_ids": ["s2"],
                "run_key": "good-key-2",
                "status": "succeeded",
            },
        ],
        "assets": {
            "scenes": {"s1": {"sha256": "a"}, "s2": {"sha256": "b"}},
            "voices": {"s1": {"sha256": "v1"}},
            "music": {"sha256": "m"},
        },
    }


@pytest.fixture
def media(tmp_path, monkeypatch):
    q = quality()
    path = tmp_path / "video.mp4"
    path.write_bytes(b"synthetic QA unit input; not a real video")
    monkeypatch.setattr(
        q.engines,
        "qa_media",
        lambda *a, **kw: {
            "passed": True,
            "checks": [{"id": "video_track", "passed": True}],
            "probe": {"duration": 4},
        },
    )
    return path


def receipt(q, plan, media, *, stage="semantic_qa"):
    rubric = q.build_rubric(plan)
    sha = hashlib.sha256(media.read_bytes()).hexdigest()
    return {
        "output_sha256": sha,
        "rubric_sha256": rubric["sha256"],
        "stage": stage,
        "evaluator": {
            "id": "synthetic-test-reviewer",
            "version": "1",
            "source": "unit-test",
            "independent": True,
        },
        "criteria": [
            {
                "criterion_id": c["id"],
                "scene_ids": ["s1", "s2"],
                "verdict": "pass",
                "uncertainty": 0.1,
                "evidence": [
                    {
                        "path": str(media),
                        "sha256": sha,
                        "source_sha256": sha,
                        "kind": kind,
                        "start_time": start,
                        "end_time": end,
                    }
                    for kind in c["modalities"]
                    for start, end in (
                        [(0.5, 0.5), (2.5, 2.5)] if kind == "frame" else [(0, 4)]
                    )
                ],
            }
            for c in rubric["criteria"]
            if c["stage"] == stage
        ],
        "defects": [],
    }


def test_genre_rubric_is_prerender_deterministic_and_plan_bound(plan):
    q = quality()
    original = copy.deepcopy(plan)
    animation = q.build_rubric(plan)
    assert animation == q.build_rubric(plan)
    assert plan == original
    assert "character_consistency" in {c["id"] for c in animation["criteria"]}
    plan["intent"]["selections"] = [{"entry_id": "C01.tech_explainer"}]
    explainer = q.build_rubric(plan)
    assert "factual_accuracy" in {c["id"] for c in explainer["criteria"]}
    assert explainer["sha256"] != animation["sha256"]


def test_machine_pass_never_promotes_unobserved_semantic_or_viewing(
    plan, record, media
):
    result = quality().evaluate_production(plan, record, media_path=media)
    assert result["machine_qa"]["verdict"] == "pass"
    assert result["semantic_qa"]["verdict"] == "unknown"
    assert result["viewing_qa"]["verdict"] == "unknown"
    assert result["overall_verdict"] == "unknown"
    assert result["semantic_qa"]["uncertainty"] == 1


def test_complete_bound_receipts_can_pass_and_preserve_provenance(plan, record, media):
    q = quality()
    record["quality_evidence"] = [
        receipt(q, plan, media, stage=s) for s in ("semantic_qa", "viewing_qa")
    ]
    result = q.evaluate_production(plan, record, media_path=media)
    assert result["overall_verdict"] == "pass"
    assert result["viewing_qa"]["evaluators"][0]["id"] == "synthetic-test-reviewer"


@pytest.mark.parametrize(
    "problem",
    [
        "stale_output",
        "stale_plan",
        "no_evidence",
        "uncertain",
        "missing_scene",
        "missing_criterion",
        "not_independent",
        "bad_hash",
    ],
)
def test_incomplete_or_unbound_receipts_cannot_pass(plan, record, media, problem):
    q = quality()
    evidence = receipt(q, plan, media)
    if problem == "stale_output":
        evidence["output_sha256"] = "0" * 64
    elif problem == "stale_plan":
        plan["scenes"][0]["narrative_goal"] = "jump"
    elif problem == "no_evidence":
        evidence["criteria"][0]["evidence"] = []
    elif problem == "uncertain":
        evidence["criteria"][0]["uncertainty"] = 0.9
    elif problem == "missing_scene":
        evidence["criteria"][0]["scene_ids"] = ["s1"]
    elif problem == "missing_criterion":
        evidence["criteria"].pop()
    elif problem == "not_independent":
        evidence["evaluator"]["independent"] = False
    else:
        evidence["criteria"][0]["evidence"][0]["sha256"] = "1" * 64
    record["quality_evidence"] = [evidence]
    result = q.evaluate_production(plan, record, media_path=media)
    assert result["semantic_qa"]["verdict"] != "pass"
    assert result["overall_verdict"] != "pass"


def test_still_frames_cannot_prove_motion_and_audio(plan, record, media):
    q = quality()
    evidence = receipt(q, plan, media, stage="viewing_qa")
    for c in evidence["criteria"]:
        for item in c["evidence"]:
            item["kind"] = "frame"
    record["quality_evidence"] = [evidence]
    assert (
        q.evaluate_production(plan, record, media_path=media)["viewing_qa"]["verdict"]
        == "unknown"
    )


@pytest.mark.parametrize(
    ("code", "layer"),
    [
        ("fake_text", "captions"),
        ("character_inconsistency", "visual"),
        ("motion", "motion"),
        ("voice", "voice"),
        ("transition", "transition"),
    ],
)
def test_partial_repairs_preserve_good_assets_shots_and_keys(plan, record, code, layer):
    q = quality()
    before = copy.deepcopy(record)
    defect = {
        "code": code,
        "scene_id": "s1",
        "shot_id": "sh1",
        "layer": layer,
        "evidence": [{"frame": 12, "start_time": 0.5, "end_time": 1}],
    }
    repair = q.plan_partial_repair(plan, record, [defect])
    assert repair["status"] == "planned"
    assert repair["invalidate"]["scene_ids"] == ["s1"]
    assert repair["invalidate"]["shot_ids"] == ["sh1"]
    assert repair["preserve"]["shot_ids"] == ["sh2", "sh3"]
    assert repair["preserve"]["segment_run_keys"] == {"seg2": "good-key-2"}
    assert repair["actions"][0]["layer"] == layer
    assert repair["actions"][0]["evidence"] == defect["evidence"]
    assert "music" in repair["preserve"]["asset_groups"]
    if layer != "voice":
        assert "voices.s1" in repair["preserve"]["asset_keys"]
    assert record == before


def test_unknown_target_is_blocked_instead_of_invalidating_everything(plan, record):
    repair = quality().plan_partial_repair(
        plan, record, [{"code": "motion", "scene_id": "typo"}]
    )
    assert repair["status"] == "blocked"
    assert repair["invalidate"]["scene_ids"] == []


def test_repeated_failure_switches_strategy_and_requires_reevaluation(plan, record):
    q = quality()
    defect = {"code": "character_inconsistency", "scene_id": "s1", "shot_id": "sh1"}
    first = q.plan_partial_repair(plan, record, [defect])["actions"][0]
    record["repair_history"] = [
        {**defect, "strategy": first["strategy"], "outcome": "failed"}
    ] * 2
    second = q.plan_partial_repair(plan, record, [defect])["actions"][0]
    assert second["strategy"] != first["strategy"]
    assert second["strategy_switched"] is True
    assert second["requires_reevaluation"] == [
        "machine_qa",
        "semantic_qa",
        "viewing_qa",
    ]


def test_missing_media_ignores_cached_machine_pass(plan, record):
    record["final"] = {"qa": {"passed": True}}
    assert quality().evaluate_production(plan, record)["overall_verdict"] == "unknown"


def worker():
    assert importlib.util.find_spec("ttakkari.creative.v2.quality_worker"), (
        "review transport missing"
    )
    return importlib.import_module("ttakkari.creative.v2.quality_worker")


@pytest.fixture
def real_media(tmp_path):
    from ttakkari.creative.render import ffmpeg

    path = tmp_path / "actual-synthetic.mp4"
    subprocess.run(
        [
            ffmpeg(),
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=24:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return path


def test_actual_frames_video_audio_reach_evaluator(plan, real_media, tmp_path):
    w = worker()
    package = w.prepare_review(plan, real_media, tmp_path / "review")
    on_disk = json.loads(Path(package["request_path"]).read_text(encoding="utf-8"))
    assert (
        on_disk["output_sha256"] == hashlib.sha256(real_media.read_bytes()).hexdigest()
    )
    assert {f["scene_id"] for f in package["frames"]} == {"s1", "s2"}
    assert any(1.9 < f["start_time"] < 2 for f in package["frames"])
    assert any(2 <= f["start_time"] < 2.1 for f in package["frames"])
    assert all(
        f["sha256"] == hashlib.sha256(Path(f["path"]).read_bytes()).hexdigest()
        for f in package["frames"]
    )
    seen = []

    def evaluator(request, payloads):
        assert payloads["video"] == real_media.read_bytes()
        assert payloads["audio"].startswith(b"RIFF")
        assert all(item["bytes"].startswith(b"\x89PNG") for item in payloads["frames"])
        assert request["rubric"]["sha256"] == quality().build_rubric(plan)["sha256"]
        seen.append(request["output_sha256"])
        return {
            "stage": "semantic_qa",
            "criteria": [],
            "defects": [],
            "evaluator": {
                "id": "test-transport",
                "version": "1",
                "source": "unit-test",
                "independent": True,
            },
        }

    received = w.deliver_review(package, evaluator)
    assert seen == [package["output_sha256"]]
    assert received["transport"]["media_bytes_delivered"] is True
    assert received["output_sha256"] == package["output_sha256"]
    assert (
        quality().evaluate_production(
            plan, {"quality_evidence": [received]}, media_path=real_media
        )["semantic_qa"]["verdict"]
        == "unknown"
    )


def test_review_delivery_rejects_tampered_source(plan, real_media, tmp_path):
    w = worker()
    package = w.prepare_review(plan, real_media, tmp_path / "review")
    real_media.write_bytes(b"changed output")
    with pytest.raises(ValueError, match="hash"):
        w.deliver_review(package, lambda *args: pytest.fail("must not call evaluator"))


def test_review_extraction_failure_is_observable(plan, tmp_path):
    w = worker()
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not video")
    with pytest.raises((ValueError, RuntimeError), match="video|media|decode"):
        w.prepare_review(plan, bad, tmp_path / "review")


def test_evidence_from_first_scene_cannot_cover_second_scene(plan, record, media):
    q = quality()
    evidence = receipt(q, plan, media, stage="viewing_qa")
    for c in evidence["criteria"]:
        for item in c["evidence"]:
            item["end_time"] = 1
    record["quality_evidence"] = [evidence]
    assert (
        q.evaluate_production(plan, record, media_path=media)["viewing_qa"]["verdict"]
        == "unknown"
    )


def test_shot_asset_invalidation_does_not_preserve_defective_shot_group(plan, record):
    record["assets"]["shots"] = {
        "sh1": {"sha256": "x"},
        "sh2": {"sha256": "y"},
        "sh3": {"sha256": "z"},
    }
    repair = quality().plan_partial_repair(
        plan,
        record,
        [{"code": "character_inconsistency", "scene_id": "s1", "shot_id": "sh1"}],
    )
    assert "shots" not in repair["preserve"]["asset_groups"]
    assert "shots.sh1" in repair["invalidate"]["asset_keys"]
    assert "shots.sh2" in repair["preserve"]["asset_keys"]


@pytest.mark.parametrize(
    "code", ["fake_text", "character_inconsistency", "motion", "voice", "transition"]
)
def test_defect_repair_and_new_hash_reevaluation_cycle(plan, record, media, code):
    q = quality()
    stage = (
        "semantic_qa"
        if code in {"fake_text", "character_inconsistency"}
        else "viewing_qa"
    )
    bad = receipt(q, plan, media, stage=stage)
    bad["defects"] = [
        {
            "code": code,
            "scene_id": "s1",
            "shot_id": "sh1",
            "evidence": bad["criteria"][0]["evidence"],
        }
    ]
    record["quality_evidence"] = [bad]
    failed = q.evaluate_production(plan, record, media_path=media)
    assert failed["overall_verdict"] == "fail"
    assert failed["repair_plan"]["invalidate"]["scene_ids"] == ["s1"]
    assert failed["repair_plan"]["preserve"]["segment_run_keys"] == {
        "seg2": "good-key-2"
    }
    media.write_bytes(b"changed targeted-shot composition; synthetic test output")
    assert (
        q.evaluate_production(plan, record, media_path=media)[stage]["verdict"]
        == "unknown"
    )
    record["quality_evidence"] = [
        receipt(q, plan, media, stage=s) for s in ("semantic_qa", "viewing_qa")
    ]
    assert (
        q.evaluate_production(plan, record, media_path=media)["overall_verdict"]
        == "pass"
    )


@pytest.mark.parametrize("bad_field", ["receipts", "criteria", "evidence", "evaluator"])
def test_malformed_evaluator_output_remains_unknown(plan, record, media, bad_field):
    q = quality()
    evidence = receipt(q, plan, media)
    if bad_field == "criteria":
        evidence["criteria"] = None
    elif bad_field == "evidence":
        evidence["criteria"][0]["evidence"] = [None, "not an evidence object"]
    elif bad_field == "evaluator":
        evidence["evaluator"] = "reviewer name without provenance"
    record["quality_evidence"] = (
        [None, evidence] if bad_field == "receipts" else [evidence]
    )
    assert (
        q.evaluate_production(plan, record, media_path=media)["overall_verdict"]
        == "unknown"
    )


def test_matching_rubric_snapshot_does_not_attest_its_creation_time(
    plan, record, media
):
    q = quality()
    record["rubric"] = q.build_rubric(plan)
    result = q.evaluate_production(plan, record, media_path=media)
    assert result["rubric_matches_record"] is True
    assert result["rubric_timing"] == "not_attested_by_evaluator"


def test_delivery_does_not_let_callback_mutate_package_or_receipt(
    plan, real_media, tmp_path
):
    w = worker()
    package = w.prepare_review(plan, real_media, tmp_path / "review")
    original = copy.deepcopy(package)
    response = {"stage": "semantic_qa", "criteria": []}

    def evaluator(request, payloads):
        request.clear()
        payloads["frames"][0]["metadata"].clear()
        return response

    w.deliver_review(package, evaluator)
    assert package == original
    assert response == {"stage": "semantic_qa", "criteria": []}


def test_core_dispatch_persists_rubric_and_quality_from_actual_assembled_media(
    tmp_path, monkeypatch
):
    from ttakkari.creative.service import Studio
    from ttakkari.creative.v2 import dispatch
    from ttakkari.creative.v2.plans import PlanStore
    from ttakkari.creative.v2.registry import Registry
    from unit_tests import test_creative_v2_dispatch as helpers

    q = quality()
    plans = PlanStore(tmp_path / "plans", registry=Registry())
    studio = Studio(tmp_path / "studio")
    plan = helpers.approved(plans)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    stored = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0]
    assert stored["rubric"]["sha256"] == q.build_rubric(plan)["sha256"]
    assert stored["final"] is None
    helpers.upload_all(plans, plan, tmp_path)
    seen = []
    evaluate = q.evaluate_production

    def actual_evaluation(plan, record, *, media_path):
        assert media_path.is_file()
        assert record["rubric"]["sha256"] == q.build_rubric(plan)["sha256"]
        seen.append(q.file_hash(media_path))
        return evaluate(plan, record, media_path=media_path)

    monkeypatch.setattr(q, "evaluate_production", actual_evaluation)
    dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    helpers.run_renderer(studio)
    result = dispatch.view(plans, studio, plan["id"])
    stored = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0]
    report = stored["quality_report"]
    assert seen == [stored["final"]["sha256"]]
    assert report["output_sha256"] == stored["final"]["sha256"]
    assert report["machine_qa"]["verdict"] == "pass"
    assert (
        report["semantic_qa"]["verdict"] == report["viewing_qa"]["verdict"] == "unknown"
    )
    assert report["overall_verdict"] == "unknown"
    assert result["quality_report"] == report
