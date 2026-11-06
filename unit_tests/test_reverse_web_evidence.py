from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from ttakkari.reverse_web_evidence import (
    CaptureError,
    create_web_capture,
    verify_web_capture,
)


def _png(path: Path, color: tuple[int, int, int], *, size: tuple[int, int] = (32, 24)) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    blob = path.read_bytes()
    return {"path": path.relative_to(path.parents[2]).as_posix(), "sha256": hashlib.sha256(blob).hexdigest(),
            "size_bytes": len(blob), "viewport": {"width": size[0], "height": size[1],
            "device_scale_factor": 1}, "format": "png"}


def _capture(root: Path, *, replica_color=(20, 40, 60), state="done", run_id="run-1", comparison_id="web-1"):
    original = _png(root / ".reverse/web-captures/original.png", (20, 40, 60))
    replica = _png(root / ".reverse/web-captures/replica.png", replica_color)
    observation = {
        "run_id": run_id,
        "observed_at": "2026-11-07T01:00:00Z",
        "url": "http://127.0.0.1:8101/",
        "input": {"action": "click", "target": "#run", "value": ""},
        "viewport": {"width": 32, "height": 24, "device_scale_factor": 1},
        "pre_state": {"count": 0},
        "environment": {"browser": "configured ttakkari_browser", "color_scheme": "light"},
        "events": [{"type": "click", "target": "#run", "value": ""}],
        "dom": [{"role": "heading", "name": "요청 처리"}, {"role": "button", "name": "실행"}],
        "state": {"text": state, "count": 1},
        "network": [{"method": "GET", "url": "http://127.0.0.1:8101/", "status": 200,
                     "request_body": None, "response_body_sha256": "a" * 64,
                     "response_headers": {"content-type": "text/html"}}],
        "screenshot": original,
        "complete": True,
        "capture_state": "complete",
    }
    replica_obs = {**observation, "url": "http://127.0.0.1:8102/",
                   "network": [{**observation["network"][0], "url": "http://127.0.0.1:8102/"}],
                   "screenshot": replica, "state": {"text": state, "count": 1}}
    return create_web_capture(root, comparison_id=comparison_id, behavior="버튼 실행",
                              input_data=observation["input"], original=observation, replica=replica_obs,
                              run_id=run_id, expected_viewport=observation["viewport"],
                              pre_state=observation["pre_state"], environment=observation["environment"],
                              origin_map={"http://127.0.0.1:8101": "http://127.0.0.1:8102"})


def test_same_function_and_pixels_recompute_as_matches(tmp_path):
    capture = _capture(tmp_path)

    result = verify_web_capture(tmp_path, capture, expected_run_id="run-1")

    assert result["result"] == "match"
    assert result["evidence"][0]["functional_result"] == "match"
    assert result["evidence"][0]["visual_result"] == "match"
    assert result["evidence"][0]["provenance"]["status"] == "unverified"


def test_same_function_different_pixels_keeps_visual_mismatch(tmp_path):
    capture = _capture(tmp_path, replica_color=(170, 0, 0))

    result = verify_web_capture(tmp_path, capture, expected_run_id="run-1")

    evidence = result["evidence"][0]
    assert evidence["functional_result"] == "match"
    assert evidence["visual_result"] == "mismatch"
    assert evidence["visual"]["different_pixels"] == 32 * 24
    assert result["result"] == "match"


def test_different_state_with_similar_screen_is_functional_mismatch(tmp_path):
    capture = _capture(tmp_path, state="ready")
    capture["replica"]["state"] = {"text": "different", "count": 1}
    _rewrite_capture_file(tmp_path, capture)

    result = verify_web_capture(tmp_path, capture, expected_run_id="run-1")

    assert result["result"] == "mismatch"
    assert result["evidence"][0]["functional_result"] == "mismatch"
    assert result["evidence"][0]["visual_result"] == "match"


def test_missing_screenshot_and_tampered_sha_are_unverified(tmp_path):
    capture = _capture(tmp_path, comparison_id="web-2")
    shot_path = tmp_path / capture["original"]["screenshot"]["path"]
    shot_path.unlink()

    missing = verify_web_capture(tmp_path, capture, expected_run_id="run-1")
    assert missing["evidence"][0]["visual_result"] == "unverified"

    capture = _capture(tmp_path, comparison_id="web-3")
    capture["replica"]["screenshot"]["sha256"] = "0" * 64
    _rewrite_capture_file(tmp_path, capture)
    tampered = verify_web_capture(tmp_path, capture, expected_run_id="run-1")
    assert tampered["evidence"][0]["visual_result"] == "unverified"


def test_viewport_mismatch_and_truncated_capture_do_not_promote(tmp_path):
    capture = _capture(tmp_path)
    capture["replica"]["viewport"] = {"width": 33, "height": 24, "device_scale_factor": 1}
    _rewrite_capture_file(tmp_path, capture)
    mismatch = verify_web_capture(tmp_path, capture, expected_run_id="run-1")
    assert mismatch["evidence"][0]["visual_result"] == "unverified"

    capture = _capture(tmp_path, comparison_id="web-truncated")
    capture["replica"]["capture_state"] = "truncated"
    _rewrite_capture_file(tmp_path, capture)
    truncated = verify_web_capture(tmp_path, capture, expected_run_id="run-1")
    assert truncated["evidence"][0]["functional_result"] == "unverified"
    assert truncated["result"] == "unverified"


def test_empty_observations_and_wrong_run_id_stay_unverified(tmp_path):
    capture = _capture(tmp_path)
    capture["original"] = None
    _rewrite_capture_file(tmp_path, capture)
    missing = verify_web_capture(tmp_path, capture, expected_run_id="run-1")
    assert missing["result"] == "unverified"

    capture = _capture(tmp_path, comparison_id="web-empty")
    for observation in (capture["original"], capture["replica"]):
        observation["events"] = []
        observation["dom"] = []
        observation["state"] = {}
        observation["network"] = []
    _rewrite_capture_file(tmp_path, capture)
    empty = verify_web_capture(tmp_path, capture, expected_run_id="run-1")
    assert empty["evidence"][0]["functional_result"] == "unverified"

    capture = _capture(tmp_path, comparison_id="web-wrong-run")
    wrong_run = verify_web_capture(tmp_path, capture, expected_run_id="other-run")
    assert wrong_run["result"] == "unverified"


def test_network_path_is_not_hidden_by_origin_mapping(tmp_path):
    capture = _capture(tmp_path)
    capture["replica"]["network"][0]["url"] = "http://127.0.0.1:8102/other-path"
    _rewrite_capture_file(tmp_path, capture)

    result = verify_web_capture(tmp_path, capture, expected_run_id="run-1")

    assert result["evidence"][0]["functional_result"] == "mismatch"
    assert result["evidence"][0]["functional_differences"] == [
        "network[0] meaningful request/response fields differ"
    ]


def test_symlink_and_unbounded_payload_are_rejected(tmp_path):
    outside = tmp_path.parent / "outside.png"
    Image.new("RGB", (1, 1)).save(outside)
    link = tmp_path / ".reverse/web-captures/escape.png"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    capture = _capture(tmp_path)
    capture["original"]["screenshot"] = {
        "path": link.relative_to(tmp_path).as_posix(),
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
        "size_bytes": outside.stat().st_size,
        "viewport": {"width": 32, "height": 24, "device_scale_factor": 1},
        "format": "png",
    }
    with pytest.raises(CaptureError):
        create_web_capture(tmp_path, comparison_id="symlink-blocked", behavior="blocked", input_data=capture["input"],
                           original=capture["original"], replica=capture["replica"], run_id="run-1",
                           expected_viewport=capture["replica"]["viewport"],
                           pre_state=capture["pre_state"], environment=capture["environment"],
                           origin_map={"http://127.0.0.1:8101": "http://127.0.0.1:8102"})
    _rewrite_capture_file(tmp_path, capture)
    assert verify_web_capture(tmp_path, capture, expected_run_id="run-1")["result"] == "unverified"

    huge = {"x": "x" * (1024 * 1024 + 1)}
    with pytest.raises(CaptureError):
        create_web_capture(tmp_path, comparison_id="huge", behavior="oversized", input_data={},
                           original=huge, replica=huge, run_id="run-1", expected_viewport={"width": 1, "height": 1})


def _rewrite_capture_file(root: Path, capture: dict) -> None:
    evidence = capture["evidence"][0]
    path = root / evidence["capture"]["path"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["original"] = capture["original"]
    raw["replica"] = capture["replica"]
    raw["complete"] = capture["original"] is not None and capture["replica"] is not None
    path.write_text(json.dumps(raw), encoding="utf-8")
    blob = path.read_bytes()
    evidence["capture"]["sha256"] = hashlib.sha256(blob).hexdigest()
    evidence["capture"]["size_bytes"] = len(blob)
