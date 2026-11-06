"""DB-free checks for the server path that consumes web evidence captures."""
from __future__ import annotations

import hashlib

from PIL import Image

from ttakkari import reverse_tasks as tasks
from ttakkari.reverse_web_evidence import create_web_capture


def _web_capture(root, *, run_id="run-web", comparison_id="web-case", replica_color=(200, 10, 10), state="done"):
    viewport = {"width": 16, "height": 12, "device_scale_factor": 1}
    input_data = {"steps": [{"action": "click", "target": "button#go"}]}
    pre_state = {"ready": False}
    environment = {"browser": "test"}
    image_root = root / ".reverse/web-captures" / comparison_id
    original_path = image_root / "original.png"
    replica_path = image_root / "replica.png"
    original_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 12), (1, 2, 3)).save(original_path, format="PNG")
    Image.new("RGB", (16, 12), replica_color).save(replica_path, format="PNG")

    def screenshot(path):
        blob = path.read_bytes()
        return {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(blob).hexdigest(),
                "size_bytes": len(blob), "viewport": viewport, "format": "png"}

    original = {"run_id": run_id, "observed_at": "2026-11-07T00:00:00Z", "url": "http://127.0.0.1:8101/",
                "input": input_data, "viewport": viewport, "pre_state": pre_state, "environment": environment,
                "events": [{"type": "click", "target": "button#go"}], "dom": [{"text": "done"}],
                "state": {"status": "done"}, "network": [], "screenshot": screenshot(original_path),
                "complete": True, "capture_state": "complete"}
    replica = {**original, "url": "http://127.0.0.1:8102/", "screenshot": screenshot(replica_path),
               "state": {"status": state}}
    return create_web_capture(root, comparison_id=comparison_id, behavior="submit", input_data=input_data,
                              original=original, replica=replica, run_id=run_id,
                              expected_viewport=viewport, pre_state=pre_state, environment=environment,
                              origin_map={"http://127.0.0.1:8101": "http://127.0.0.1:8102"},
                              verified_at="2026-11-07T00:01:00Z")


def test_core_recomputes_function_and_visual_results_separately(tmp_path):
    comparison = _web_capture(tmp_path)

    checked = tasks.verify_comparisons(tmp_path, [comparison], expected_run_id="run-web")[0]

    assert checked["result"] == "match"
    assert checked["functional_result"] == "match"
    assert checked["visual_result"] == "mismatch"
    assert checked["visual_assessment"]["different_pixels"] == 16 * 12
    assert checked["evidence"][0]["integrity"]["status"] == "verified"
    assert checked["evidence"][0]["provenance"]["status"] == "unverified"
    assert tasks._comparison_summary([checked])["status"] == "match"
    assert tasks._visual_comparison_summary([checked], required=True)["status"] == "mismatch"


def test_core_does_not_trust_declared_verdict_or_promote_missing_and_wrong_run_capture(tmp_path):
    comparison = _web_capture(tmp_path)
    comparison.update(result="mismatch", difference={"declared": "mismatch"})
    checked = tasks.verify_comparisons(tmp_path, [comparison], expected_run_id="run-web")[0]
    assert checked["result"] == "match"
    assert checked["functional_result"] == "match"

    comparison["evidence"][0]["visual"] = {"different_pixels": 0, "pixel_equality": "exact"}
    wrong_run = tasks.verify_comparisons(tmp_path, [comparison], expected_run_id="other-run")[0]
    assert wrong_run["result"] == "unverified"
    assert wrong_run["evidence"][0]["integrity"]["status"] == "unverified"
    assert wrong_run["evidence"][0]["provenance"]["status"] == "unverified"
    assert "different_pixels" not in wrong_run["visual_assessment"]
    assert "different_pixels" not in wrong_run["evidence"][0]["visual"]

    missing = {**comparison, "evidence": [{"kind": "web_capture", "capture": {
        "path": ".reverse/web-captures/missing.json", "sha256": "0" * 64, "size_bytes": 1}}]}
    missing_checked = tasks.verify_comparisons(tmp_path, [missing], expected_run_id="run-web")[0]
    assert missing_checked["result"] == "unverified"
    assert missing_checked["visual_result"] == "unverified"


def test_core_rechecks_screenshot_bytes_instead_of_trusting_capture_record(tmp_path):
    comparison = _web_capture(tmp_path)
    (tmp_path / comparison["replica"]["screenshot"]["path"]).write_bytes(b"changed after capture")

    checked = tasks.verify_comparisons(tmp_path, [comparison], expected_run_id="run-web")[0]

    assert checked["result"] == "unverified"
    assert checked["functional_result"] == "unverified"
    assert checked["visual_result"] == "unverified"
    assert checked["evidence"][0]["integrity"]["status"] == "unverified"


def test_verifier_run_keeps_its_distinct_comparison_result(tmp_path):
    implementation_capture = _web_capture(tmp_path, run_id="implementation-run", comparison_id="implementation")
    implementation = tasks.verify_comparisons(tmp_path, [implementation_capture],
                                              expected_run_id="implementation-run")[0]
    verifier_capture = _web_capture(tmp_path, run_id="verifier-run", comparison_id="verifier",
                                    state="different")
    verifier = tasks.verify_comparisons(tmp_path, [verifier_capture], expected_run_id="verifier-run")[0]

    assert implementation["result"] == "match"
    assert verifier["result"] == "mismatch"
    assert verifier["functional_result"] == "mismatch"
    assert implementation["run_id"] == "implementation-run"
    assert verifier["run_id"] == "verifier-run"


def test_legacy_web_execution_evidence_stays_unverified(tmp_path):
    legacy = {"id": "legacy-web", "behavior": "web observation", "target_type": "web", "input": {},
              "pre_state": {}, "original": {"events": []}, "replica": {"events": []}, "environment": {},
              "evidence_level": "direct", "evidence": [{"kind": "execution", "path": ".reverse/comparisons/old.json",
              "sha256": "a" * 64}], "verified_at": "2026-11-07T00:00:00Z", "run_id": "old-run",
              "result": "match", "difference": None}

    checked = tasks.verify_comparisons(tmp_path, [legacy], expected_run_id="old-run")[0]

    assert checked["result"] == "unverified"
    assert checked["evidence_level"] == "observed"


def test_web_visual_gate_fails_on_mismatch_or_unverified_and_keeps_counts_separate():
    direct = {"status": "match", "count": 2, "matched": 2, "mismatched": 0, "unverified": 0}
    mismatch = {"target_type": "web", "visual_result": "mismatch"}
    unverified = {"target_type": "web", "visual_result": "unverified"}

    assert tasks._web_quality_failed("web", direct, {"status": "match"}) is False
    assert tasks._web_quality_failed("web", direct, {"status": "mismatch"}) is True
    assert tasks._web_quality_failed("web", direct, {"status": "unverified"}) is True
    assert tasks._web_quality_failed("cli", direct, {"status": "mismatch"}) is False
    visual = tasks._visual_comparison_summary([mismatch, unverified], required=True)
    assert visual == {"status": "mismatch", "count": 2, "matched": 0, "mismatched": 1, "unverified": 1}
    assert direct["count"] == 2  # visual cases never enter functional comparison counts


def test_visual_web_defect_uses_only_remaining_bounded_repair_attempts():
    direct = {"status": "match"}
    visual = {"status": "mismatch"}

    assert tasks._should_repair_comparison("implement", "web", direct, visual,
                                           attempts=0, limit=1, has_source=True, has_observations=True)
    assert not tasks._should_repair_comparison("implement", "web", direct, visual,
                                               attempts=1, limit=1, has_source=True, has_observations=True)
    assert not tasks._should_repair_comparison("verify", "web", direct, visual,
                                               attempts=0, limit=1, has_source=True, has_observations=True)
