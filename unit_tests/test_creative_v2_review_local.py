"""Local measurement evaluator and the audio-layer remix, run against real ffmpeg output.

The OCR step is the only stand-in: a stub executable replays configured lines, because Apple Vision is
not available on every CI host. Everything else (decode, loudness, frame analysis, hashing, remux)
is executed for real, and the verdicts under test come from those measurements.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_creative_v2_dispatch import stores as _stores
from test_creative_v2_stage2 import make_real_production

from ttakkari.creative.render import ffmpeg
from ttakkari.creative.v2 import audio_measure, dispatch, repair_audio
from ttakkari.creative.v2 import production_review as reviews
from ttakkari.creative.v2.review_local_evaluator import local_measure_binding

SR = audio_measure.SAMPLE_RATE


@pytest.fixture
def stores(tmp_path):
    return _stores.__wrapped__(tmp_path)


def tone(seconds: float, db: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    wave = (10 ** (db / 20)) * np.sin(2 * np.pi * 220 * t) * np.sqrt(2)
    return np.stack([wave, wave], axis=1).astype(np.float32)


def make_media(path: Path, samples: np.ndarray) -> Path:
    seconds = len(samples) / SR
    subprocess.run(
        [ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=24:d={seconds}",
         "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0", "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(path)],
        input=np.ascontiguousarray(samples, dtype="<f4").tobytes(), check=True, capture_output=True,
    )
    return path


def lufs(path: Path, start: float, end: float) -> float:
    pcm = audio_measure.decode_pcm(path)
    return audio_measure.loudness(pcm[int(start * SR) : int(end * SR)])["lufs"]


def decodes_cleanly(path: Path) -> bool:
    result = subprocess.run(
        [ffmpeg(), "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stderr.strip()


# --- measurements -----------------------------------------------------------


def test_dips_ignore_intended_fades_and_report_seam():
    body = tone(20, -20)
    fade = np.linspace(0, 1, int(1.2 * SR), dtype=np.float32)[:, None]
    body[: len(fade)] *= fade
    body[-len(fade) :] *= fade[::-1]
    assert audio_measure.find_dips(body) == []
    seam = slice(int(9.4 * SR), int(10.4 * SR))
    body[seam] *= 10 ** (-12 / 20)
    (dip,) = audio_measure.find_dips(body)
    assert 9.0 < dip["at"] < 10.6 and dip["depth_db"] >= 9


def test_level_verdict_fails_quiet_and_hot_and_never_passes_unmeasured():
    assert audio_measure.level_verdict({"lufs": -34.0, "true_peak_db": -27.0})[0] == "fail"
    assert audio_measure.level_verdict({"lufs": -18.0, "true_peak_db": -11.0})[0] == "ok"
    assert audio_measure.level_verdict({"lufs": -12.0, "true_peak_db": -0.2})[0] == "fail"
    assert audio_measure.level_verdict(None)[0] == "unknown"


# --- remix repair: only the audio layer and only the named window -----------


def test_remix_fixes_named_window_keeps_picture_and_neighbour_level(tmp_path):
    quiet, normal = tone(10, -40), tone(10, -20)
    track = np.concatenate([quiet, normal])
    track[int(4.4 * SR) : int(5.4 * SR)] *= 10 ** (-12 / 20)  # seam dip inside the quiet scene
    source = make_media(tmp_path / "source.mp4", track)
    scenes = [{"scene_id": "S1", "start_time": 0.0, "end_time": 10.0}, {"scene_id": "S2", "start_time": 10.0, "end_time": 20.0}]
    dips = audio_measure.find_dips(audio_measure.decode_pcm(source))
    assert dips, "fixture must contain a measurable seam"
    out = tmp_path / "fixed.mp4"
    report = repair_audio.remix(source, out, level_windows=[{"start": 0.0, "end": 10.0}], dips=dips, scenes=scenes)

    assert report["video_stream_md5"]["before"] == report["video_stream_md5"]["after"] == repair_audio.video_md5(source)
    assert abs(lufs(out, 0.5, 9.5) - audio_measure.TARGET_LUFS) < 1.5
    # The untouched scene keeps its level (AAC re-encode only), proving the repair stayed inside its window.
    assert abs(lufs(out, 10.5, 19.5) - lufs(source, 10.5, 19.5)) < 0.5
    assert report["dips_before"] and report["dips_after"] == []
    assert decodes_cleanly(out)


def test_remix_refuses_to_keep_output_when_picture_would_change(tmp_path, monkeypatch):
    source = make_media(tmp_path / "source.mp4", tone(4, -40))
    hashes = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(repair_audio, "video_md5", lambda path: next(hashes))
    out = tmp_path / "fixed.mp4"
    with pytest.raises(RuntimeError, match="video stream changed"):
        repair_audio.remix(source, out, level_windows=[{"start": 0.0, "end": 4.0}], dips=[],
                           scenes=[{"scene_id": "S1", "start_time": 0.0, "end_time": 4.0}])
    assert not out.exists()


# --- run_review wiring --------------------------------------------------------

STUB = """#!{python}
import json, sys
from pathlib import Path
from PIL import Image
config = json.loads(Path({config!r}).read_text())
for path in sys.argv[1:]:
    request = json.loads((Path(path).parent / "review-request.json").read_text())
    scene = next(f["scene_id"] for f in request["frames"] if f["path"] == path)
    width, height = Image.open(path).size
    print(json.dumps({{"path": path, "width": width, "height": height, "lines": config.get(scene, [])}}))
"""


def ocr_stub(tmp_path, monkeypatch, config):
    cfg = tmp_path / "ocr.json"
    cfg.write_text(json.dumps(config))
    stub = tmp_path / "ocr_stub.py"
    stub.write_text(STUB.format(python=sys.executable, config=str(cfg)))
    stub.chmod(0o755)
    monkeypatch.setenv("TTAKKARI_OCR_BINARY", str(stub))


def line(text, h, conf=1.0):
    return {"text": text, "confidence": conf, "x": 0.1, "y": 0.05, "w": 0.5, "h": h}


@pytest.fixture
def prod(tmp_path):
    plans, studio = _stores.__wrapped__(tmp_path)
    plan, _ = make_real_production(plans, studio, tmp_path)
    return plans, studio, plan


def titles(plans, plan):
    return {s["scene_id"]: s["title"] for s in plans.get(plan["id"])["scenes"]}


def stored(plans, plan):
    return dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][-1]


def rows(rec, stage, criterion):
    receipt = next(r for r in rec["quality_evidence"][-2:] if r["stage"] == stage)
    return {r["scene_ids"][0]: r for r in receipt["criteria"] if r["criterion_id"] == criterion}


def test_review_records_both_stages_and_text_defect_is_evidence_bound(prod, tmp_path, monkeypatch):
    plans, studio, plan = prod
    names = titles(plans, plan)
    sizes = {sid: (0.012 if i == 1 else 0.035) for i, sid in enumerate(names)}  # scene 2 caption is tiny
    ocr_stub(tmp_path, monkeypatch, {sid: [line(text, sizes[sid])] for sid, text in names.items()})
    out = reviews.run_review(plans, studio, plan["id"], 2, binding=local_measure_binding())
    rec = stored(plans, plan)

    assert out["status"] == "recorded" and len(out["receipt_sha256s"]) == 2
    assert {r["stage"] for r in rec["quality_evidence"]} == {"semantic_qa", "viewing_qa"}
    legible = rows(rec, "semantic_qa", "text_legibility")
    ids = list(names)
    assert [legible[i]["verdict"] for i in ids] == ["pass", "fail", "pass"]
    assert "높이" in legible[ids[1]]["rationale"] and legible[ids[1]]["evidence"]
    defects = rec["quality_report"]["defects"]
    assert [(d["code"], d["scene_id"]) for d in defects if d["layer"] == "captions"] == [("text_legibility", ids[1])]
    # Criteria that need perception stay unknown and the overall verdict cannot be a pass.
    for criterion in ("intent_alignment", "style_continuity"):
        assert {r["verdict"] for r in rows(rec, "semantic_qa", criterion).values()} == {"unknown"}
    assert {r["verdict"] for r in rows(rec, "viewing_qa", "viewing_value").values()} == {"unknown"}
    assert rec["quality_report"]["overall_verdict"] == "fail"


def test_misspelled_caption_is_a_fake_text_defect(prod, tmp_path, monkeypatch):
    plans, studio, plan = prod
    names = titles(plans, plan)
    garbled = {sid: text[:-2] + "ㅋㅋ" if i == 0 else text for i, (sid, text) in enumerate(names.items())}
    ocr_stub(tmp_path, monkeypatch, {sid: [line(text, 0.035)] for sid, text in garbled.items()})
    reviews.run_review(plans, studio, plan["id"], 2, binding=local_measure_binding())
    rec = stored(plans, plan)
    first = next(iter(names))
    assert rows(rec, "semantic_qa", "text_legibility")[first]["verdict"] == "fail"
    assert ("fake_text", first) in [(d["code"], d["scene_id"]) for d in rec["quality_report"]["defects"]]


def test_missing_ocr_leaves_text_unknown_not_pass(prod, tmp_path, monkeypatch):
    plans, studio, plan = prod
    monkeypatch.setenv("TTAKKARI_OCR_BINARY", str(tmp_path / "does-not-exist"))
    reviews.run_review(plans, studio, plan["id"], 2, binding=local_measure_binding())
    rec = stored(plans, plan)
    verdicts = {r["verdict"] for r in rows(rec, "semantic_qa", "text_legibility").values()}
    assert verdicts == {"unknown"}
    assert rec["quality_report"]["semantic_qa"]["verdict"] == "unknown"
    first = next(iter(titles(plans, plan)))
    assert "OCR" in rows(rec, "semantic_qa", "text_legibility")[first]["rationale"]


def test_ocr_image_decode_error_is_reported_as_unavailable(monkeypatch, tmp_path):
    image = tmp_path / "broken.png"
    image.write_bytes(b"not a png")
    monkeypatch.setattr(
        "ttakkari.creative.v2.review_local_evaluator.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"path": str(image), "error": "nilError"}) + "\n",
            stderr="",
        ),
    )

    with pytest.raises(TypeError, match="OCR image decode failed.*nilError"):
        from ttakkari.creative.v2.review_local_evaluator import run_ocr

        run_ocr([str(image)], Path("unused-ocr-binary"))


def quieten_final(plans, plan):
    """Replace the recorded final with a copy whose audio is 25 dB lower (picture stream copied)."""
    store = dispatch.ProductionStore(plans)
    with store.tx(plan["id"]) as data:
        record = dispatch._record(data, 2)
        final = record["final"]
        path = store.folder(plan["id"]) / final["file"]
        quiet = path.with_name("quiet-final.mp4")
        subprocess.run(
            [ffmpeg(), "-v", "error", "-y", "-i", str(path), "-c:v", "copy", "-af", "volume=-25dB", "-c:a", "aac", str(quiet)],
            check=True, capture_output=True,
        )
        digest = dispatch._sha256_file(quiet)
        final.update(file=str(quiet.relative_to(store.folder(plan["id"]))), sha256=digest, size=quiet.stat().st_size,
                     id=str(__import__("uuid").UUID(digest[:32])))
    return quiet


def test_audio_defect_is_repaired_in_place_then_rereviewed(prod, tmp_path, monkeypatch):
    plans, studio, plan = prod
    monkeypatch.setenv("TTAKKARI_OCR_BINARY", str(tmp_path / "does-not-exist"))
    quiet = quieten_final(plans, plan)
    reviews.run_review(plans, studio, plan["id"], 2, binding=local_measure_binding())
    rec = stored(plans, plan)
    before = {sid: r["verdict"] for sid, r in rows(rec, "viewing_qa", "audio_quality").items()}
    assert set(before.values()) == {"fail"}
    assert rec["quality_report"]["viewing_qa"]["verdict"] == "fail"
    audio = [{"scene_id": d["scene_id"], "code": d["code"], "layer": d["layer"]} for d in rec["quality_report"]["defects"] if d["layer"] == "audio"]
    assert {d["code"] for d in audio} == {"audio_level"}

    proposal = dispatch.plan_repair(plans, plan["id"], 2, audio)
    assert proposal["status"] == "planned"
    assert proposal["invalidate"]["segment_ids"] == [] and proposal["invalidate"]["layers"] == ["audio"]
    with pytest.raises(dispatch.ProductionError, match="no caption updates"):
        dispatch.apply_repair(plans, plan["id"], 2, proposal["proposal_sha256"], [{"scene_id": "x"}])
    applied = dispatch.apply_repair(plans, plan["id"], 2, proposal["proposal_sha256"], [])

    assert applied["status"] == "applied_rendered" and applied["output_sha256"] != applied["source_output_sha256"]
    assert applied["report"]["video_stream_md5"]["preserved"]
    rec = stored(plans, plan)
    assert rec["final"]["sha256"] == applied["output_sha256"] and rec["final_history"][-1]["sha256"] == applied["source_output_sha256"]
    assert quiet.is_file(), "the superseded final is kept on disk"
    assert rec["review_execution"]["status"] == "needs_review"
    assert rec["quality_report"]["viewing_qa"]["verdict"] == "unknown"  # old receipts no longer attest the new file
    assert rec["final"]["qa"]["passed"] and decodes_cleanly(dispatch.result_media(plans, plan["id"], 2, "final")[0])

    reviews.run_review(plans, studio, plan["id"], 2, binding=local_measure_binding())
    rec = stored(plans, plan)
    after = {sid: r["verdict"] for sid, r in rows(rec, "viewing_qa", "audio_quality").items()}
    assert set(after.values()) == {"unknown"}  # defect gone; a listening judgement is still not made
    assert not [d for d in rec["quality_report"]["defects"] if d["layer"] == "audio"]
    assert rec["quality_report"]["overall_verdict"] != "pass"


def test_mixed_audio_and_caption_proposal_still_blocks_caption_part(prod, tmp_path, monkeypatch):
    plans, studio, plan = prod
    monkeypatch.setenv("TTAKKARI_OCR_BINARY", str(tmp_path / "does-not-exist"))
    quieten_final(plans, plan)
    reviews.run_review(plans, studio, plan["id"], 2, binding=local_measure_binding())
    sid = next(iter(titles(plans, plan)))
    proposal = dispatch.plan_repair(plans, plan["id"], 2, [
        {"scene_id": sid, "code": "audio_level", "layer": "audio"},
        {"scene_id": sid, "code": "text_legibility", "layer": "captions"},
    ])
    out = dispatch.apply_repair(plans, plan["id"], 2, proposal["proposal_sha256"], [])
    assert out["status"] == "blocked" and any(b["action"]["layer"] == "audio" for b in out["blocked"])
