"""기계 검수·키프레임 표본·결함 샷 재제작 시험. 실제 FFmpeg(imageio-ffmpeg 번들, CPU)로 만든 미디어를 쓴다.

Chrome·GPU·모델은 쓰지 않는다. 정상/결함 대조로 검수기가 결함을 잡는지, 선언된 예외는 통과시키는지 확인한다.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from creative_support import (
    click_track,
    ffmpeg_plans,
    new_production,
    real_ffmpeg_pipeline,
)
from PIL import Image

from ttakkari.creative import review as review_module
from ttakkari.creative.engines import media
from ttakkari.creative.pipeline import PipelineError


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        [media.ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y", *args],
        check=True,
        timeout=60,
    )


def _clip(
    path: Path,
    *,
    video: str,
    audio: str | None = None,
    frames: int = 24,
    fps: int = 24,
    extra=(),
):
    joiner = ":" if "=" in video else "="
    argv = ["-f", "lavfi", "-i", f"{video}{joiner}s=160x90:r={fps}"]
    if audio:
        argv += ["-f", "lavfi", "-i", audio]
    argv += ["-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        argv += ["-c:a", "aac", "-b:a", "128k", *extra]
    _ffmpeg(*argv, str(path))
    return path


def _check(path: Path, *, frames=24, expect_audio=False, allow=None, exact=True):
    return review_module.check_media(
        path,
        expected_frames=frames,
        width=160,
        height=90,
        fps_num=24,
        fps_den=1,
        frame_exact=exact,
        expect_audio=expect_audio,
        allow=allow or {"black": [], "freeze": [], "silence": []},
    )


def test_good_clip_passes_every_machine_criterion(tmp_path):
    clip = _clip(
        tmp_path / "good.mp4",
        video="testsrc2",
        audio="sine=frequency=440:sample_rate=48000",
        extra=("-t", "1"),
    )
    result = _check(clip, expect_audio=True)
    assert set(result["criteria"].values()) == {"pass"}, result["criteria"]
    assert review_module.verdict_of(result["criteria"]) == "pass"
    assert {
        "decode",
        "frame_count",
        "resolution",
        "fps",
        "duration",
        "black_frames",
        "freeze",
        "audio_peak",
        "audio_silence",
        "av_sync",
    } <= set(result["criteria"])


@pytest.mark.parametrize(
    "name,video,audio,expected_fail",
    [
        ("black", "color=c=black", None, {"black_frames"}),
        ("frozen", "color=c=0x336699", None, {"freeze"}),
        ("silent", "testsrc2", "anullsrc=r=48000:cl=mono", {"audio_silence"}),
        (
            "clipping",
            "testsrc2",
            "sine=frequency=440:sample_rate=48000,volume=40dB",
            {"audio_peak"},
        ),
    ],
)
def test_defective_clips_are_flagged_with_the_specific_criterion(
    tmp_path, name, video, audio, expected_fail
):
    clip = _clip(
        tmp_path / f"{name}.mp4",
        video=video,
        audio=audio,
        extra=("-t", "1") if audio else (),
    )
    result = _check(clip, expect_audio=bool(audio))
    failed = {k for k, v in result["criteria"].items() if v == "fail"}
    assert expected_fail <= failed, result["criteria"]
    assert review_module.verdict_of(result["criteria"]) == "fail"


def test_wrong_length_missing_audio_and_av_sync_mismatch_are_failures(tmp_path):
    short = _clip(tmp_path / "short.mp4", video="testsrc2", frames=12)
    result = _check(short, frames=24)
    assert (
        result["criteria"]["frame_count"] == "fail"
        and result["criteria"]["duration"] == "fail"
    )
    no_audio = _clip(tmp_path / "noaudio.mp4", video="testsrc2")
    assert _check(no_audio, expect_audio=True)["criteria"]["audio_present"] == "fail"
    # 영상 1초에 오디오 2초를 각각 만든 뒤 스트림 복사로 합쳐 길이 차이를 만든다.
    video_only = _clip(tmp_path / "v1.mp4", video="testsrc2")
    audio_only = tmp_path / "a2.m4a"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=330:sample_rate=48000",
        "-t",
        "2",
        "-c:a",
        "aac",
        str(audio_only),
    )
    mismatch = tmp_path / "mismatch.mp4"
    _ffmpeg(
        "-i",
        str(video_only),
        "-i",
        str(audio_only),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-c",
        "copy",
        str(mismatch),
    )
    durations = media.stream_durations(mismatch)
    assert durations["audio"] - durations["video"] > 0.5
    assert _check(mismatch, expect_audio=True)["criteria"]["av_sync"] == "fail"


def test_corrupt_bytes_fail_decode_instead_of_raising(tmp_path):
    good = _clip(tmp_path / "g.mp4", video="testsrc2")
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 3])
    result = _check(broken)
    assert result["criteria"]["decode"] == "fail"
    assert review_module.verdict_of(result["criteria"]) == "fail"
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"not a video" * 100)
    assert _check(garbage)["criteria"] == {"decode": "fail"}


def test_declared_freeze_and_black_exceptions_are_honored_only_with_a_reason(tmp_path):
    frozen = _clip(tmp_path / "hold.mp4", video="color=c=0x336699")
    declared = review_module.parse_allowances(
        ["allow_freeze:0-24:타이틀 정지 홀드"], 24, 1
    )
    assert _check(frozen, allow=declared)["criteria"]["freeze"] == "pass"
    no_reason = review_module.parse_allowances(
        ["allow_freeze:0-24:   ", "allow_freeze:5-5:x", "allow_freeze:bad"], 24, 1
    )
    assert no_reason == {"black": [], "freeze": [], "silence": []}
    assert _check(frozen, allow=no_reason)["criteria"]["freeze"] == "fail"
    partial = review_module.parse_allowances(["allow_freeze:0-6:앞부분만"], 24, 1)
    assert (
        _check(frozen, allow=partial)["criteria"]["freeze"] == "fail"
    )  # 선언 범위를 넘는 정지는 예외가 아니다
    black = _clip(tmp_path / "dark.mp4", video="color=c=black")
    allowed_black = review_module.parse_allowances(
        ["allow_black:0-24:의도된 암전"], 24, 1
    )
    assert (
        _check(
            black,
            allow={
                **allowed_black,
                "freeze": review_module.parse_allowances(
                    ["allow_freeze:0-24:암전 중 정지"], 24, 1
                )["freeze"],
            },
        )["criteria"]["black_frames"]
        == "pass"
    )


def test_non_exact_generative_profile_leaves_size_and_length_unknown_and_review_pending(
    tmp_path,
):
    clip = _clip(tmp_path / "gen.mp4", video="testsrc2", frames=30)
    result = _check(clip, frames=24, exact=False)
    assert (
        result["criteria"]["frame_count"]
        == result["criteria"]["duration"]
        == result["criteria"]["resolution"]
        == "unknown"
    )
    assert review_module.verdict_of(result["criteria"]) == "review_pending"


def test_sample_keyframes_covers_boundaries_cuts_motion_extremes_audio_events_and_records_dropped():
    diff = [0.0] + [1.0] * 59
    diff[30] = 80.0  # 하드 컷 후보
    diff[45] = 9.0
    series = {"frames": 60, "diff": diff, "luma": [10.0] * 60}
    selected, dropped = review_module.sample_keyframes(
        series, frames=60, cues=[12, 50], cap=100
    )
    by_frame = {k["frame"]: set(k["reasons"]) for k in selected}
    assert {0, 1, 58, 59} <= set(by_frame) and "shot_boundary" in by_frame[0]
    assert {29, 30} <= set(by_frame) and "cut_after" in by_frame[30]
    assert "motion_max" in by_frame[45] or "motion_max" in by_frame[30]
    assert {"audio_event"} <= by_frame[12] and {"audio_event"} <= by_frame[50]
    assert dropped == []
    capped, dropped = review_module.sample_keyframes(
        series, frames=60, cues=[12, 50], cap=6
    )
    assert len(capped) == 6 and dropped  # 상한으로 버린 후보가 기록된다
    assert all(
        k["frame"] in {0, 1, 58, 59, 12, 50}
        or "shot_boundary" in k["reasons"]
        or "audio_event" in k["reasons"]
        for k in capped[:4]
    )


# ---------- 파이프라인 전체: 렌더 → 검수 → 결함 교정 → 조립


def _three_shot_production(tmp_path, *, shot2_color="#336699"):
    pipeline, store, _registry = real_ffmpeg_pipeline(tmp_path)
    pid, timeline = new_production(
        store,
        shots=3,
        frames=24,
        cues=[
            {"frame": 30, "kind": "beat", "label": "beat"},
            {"frame": 60, "kind": "beat", "label": "beat"},
        ],
    )
    plans = ffmpeg_plans(3)
    plans[1].update(
        profile="solid-color", params={"color": shot2_color}, acceptance_criteria=[]
    )
    pipeline.apply_shot_plan(pid, timeline["revision"], plans)
    return pipeline, store, pid


def test_review_binds_keyframes_to_output_hash_and_records_a_machine_only_receipt(
    tmp_path,
):
    pipeline, store, pid = _three_shot_production(tmp_path)
    attempt = pipeline.render_shot(pid, "shot-1", review=True)
    state = store.get_production(pid)
    output_sha = attempt["outputs"][0]["sha256"]
    review = next(r for r in state["reviews"] if r["output_sha256"] == output_sha)
    assert (
        review["reviewer"] == "machine-check/v1"
        and review["independent"] is False
        and review["approval_event_id"] is None
    )
    assert review["shot_revision"] == attempt["shot_revision"]
    report = state["review_reports"][review["review_id"]]
    assert (
        report["verdict"] == "pass"
        and report["approval"] is None
        and "의미·미감" in report["scope_note"]
    )
    manifest = json.loads(store.read_artifact(pid, report["keyframe_manifest_sha256"]))
    assert (
        manifest["output_sha256"] == output_sha
        and manifest["attempt_id"] == attempt["attempt_id"]
    )
    reasons = {r for k in manifest["keyframes"] for r in k["reasons"]}
    # shot-1은 타임라인 프레임 0~24이므로 beat(30)은 이 샷의 오디오 이벤트가 아니다. 샷 안 cue만 표본에 든다.
    assert {"shot_boundary", "motion_max"} <= reasons and "audio_event" not in reasons
    assert all(0 <= k["frame"] < 24 for k in manifest["keyframes"])
    uris = set(review["evidence_uris"])
    assert f"artifact:{report['keyframe_manifest_sha256']}" in uris
    for item in manifest["keyframes"]:
        assert f"artifact:{item['sha256']}" in uris
        image = Image.open(io.BytesIO(store.read_artifact(pid, item["sha256"])))
        assert image.size == (320, 180)
    # 같은 출력을 다시 검수해도 새 receipt를 만들지 않는다(멱등).
    again = pipeline.review(pid, attempt["attempt_id"])
    assert again["reused"] is True and len(store.get_production(pid)["reviews"]) == 1


def test_audio_event_cues_inside_a_shot_become_keyframes(tmp_path):
    pipeline, store, pid = _three_shot_production(tmp_path)
    attempt = pipeline.render_shot(
        pid, "shot-2", review=True
    )  # 타임라인 24~48 안에 beat 30
    state = store.get_production(pid)
    review = next(
        r
        for r in state["reviews"]
        if r["output_sha256"] == attempt["outputs"][0]["sha256"]
    )
    manifest = json.loads(
        store.read_artifact(
            pid,
            state["review_reports"][review["review_id"]]["keyframe_manifest_sha256"],
        )
    )
    audio = [k for k in manifest["keyframes"] if "audio_event" in k["reasons"]]
    assert [(k["frame"], k["timeline_frame"]) for k in audio] == [(6, 30)]


def test_defective_shot_is_detected_then_only_that_shot_is_rerendered_and_neighbors_keep_their_hashes(
    tmp_path,
):
    pipeline, store, pid = _three_shot_production(tmp_path, shot2_color="#000000")
    for shot_id in ("shot-1", "shot-2", "shot-3"):
        pipeline.render_shot(pid, shot_id, review=True)
    state = store.get_production(pid)
    defects = pipeline.defective_shots(pid)
    assert [d["shot_id"] for d in defects] == ["shot-2"]
    assert (
        "black_frames" in defects[0]["detail"] and defects[0]["kind"] == "review_failed"
    )
    with pytest.raises(PipelineError, match="결함 샷"):
        pipeline.compose_final(pid)
    before = pipeline.current_outputs(state)
    revisions_before = dict(state["manifest"]["active_shot_revisions"])
    attempts_before = {a["attempt_id"] for a in state["attempts"]}
    # 교정: 그 샷만 색을 바꿔 새 revision으로 재제작
    pipeline.revise_shot(
        pid,
        "shot-2",
        expected_revision=revisions_before["shot-2"],
        profile="test-pattern",
        reason="검은 프레임 교정",
        source="test",
        selection="explicit-lab",
    )
    attempt = pipeline.render_shot(pid, "shot-2", review=True)
    state = store.get_production(pid)
    after = pipeline.current_outputs(state)
    assert (
        after["shot-1"] == before["shot-1"] and after["shot-3"] == before["shot-3"]
    ), "주변 샷 output hash가 바뀌었다"
    assert (
        after["shot-2"] != before["shot-2"]
        and after["shot-2"] == attempt["outputs"][0]["sha256"]
    )
    assert state["manifest"]["active_shot_revisions"] == {
        **revisions_before,
        "shot-2": revisions_before["shot-2"] + 1,
    }
    new_attempts = [
        a for a in state["attempts"] if a["attempt_id"] not in attempts_before
    ]
    assert [a["shot_id"] for a in new_attempts] == ["shot-2"]
    # 이전 결함 시도와 그 검수는 이력에 그대로 남는다.
    assert any(
        a["state"] == "review_pending"
        and a["shot_id"] == "shot-2"
        and a["shot_revision"] == revisions_before["shot-2"]
        for a in state["attempts"]
    )
    assert pipeline.defective_shots(pid) == []


def test_final_assembly_with_audio_machine_check_and_stale_resolution(tmp_path):
    pipeline, store, pid = _three_shot_production(tmp_path)
    state = store.get_production(pid)
    # shot-2는 정지 화면(단색)이므로 정지 구간을 선언한다.
    pipeline.revise_shot(
        pid,
        "shot-2",
        expected_revision=state["shots"]["shot-2"][-1]["revision"],
        acceptance_criteria=["allow_freeze:0-24:단색 배경 홀드"],
        reason="선언 추가",
        source="test",
        selection="explicit-lab",
    )
    for shot_id in ("shot-1", "shot-2", "shot-3"):
        pipeline.render_shot(pid, shot_id, review=True)
    audio = store.put_artifact(
        pid, "click.wav", click_track(seconds=4), media_type="audio/wav"
    )
    composition = pipeline.compose_final(pid, audio_sha256=audio["sha256"])
    assert composition["real_execution"] is True and composition[
        "shot_outputs"
    ] == pipeline.current_outputs(store.get_production(pid))
    result = pipeline.review_final(pid, composition["composition_key"])
    report = result["report"]
    assert report["kind"] == "final" and report["approval"] is None
    assert report["verdict"] == "pass", report["criteria"]
    assert (
        report["criteria"]["frame_count"] == "pass"
        and report["criteria"]["av_sync"] == "pass"
    )
    final_review = result["review"]
    assert (
        final_review["shot_revision"] is None
        and final_review["output_sha256"] == composition["output_sha256"]
    )
    state = store.get_production(pid)
    assert (
        state["stale"]["transition_neighbors"] == []
        and "production" not in state["stale"]["reviews"]
    )
    final_path = tmp_path / "final.mp4"
    final_path.write_bytes(store.read_artifact(pid, composition["output_sha256"]))
    info = media.probe_media(final_path)
    assert info["video"]["width"] == 320 and info["audio"]["sample_rate"] == 48000
    # 같은 입력으로 다시 조립해도 새 합성을 만들지 않는다(멱등).
    again = pipeline.compose_final(pid, audio_sha256=audio["sha256"])
    assert (
        again["output_sha256"] == composition["output_sha256"]
        and len(store.get_production(pid)["compositions"]) == 1
    )
    assert pipeline.review_final(pid, composition["composition_key"])["reused"] is True


def test_final_check_flags_a_broken_final_without_changing_other_state(tmp_path):
    pipeline, store, pid = _three_shot_production(tmp_path)
    state = store.get_production(pid)
    pipeline.revise_shot(
        pid,
        "shot-2",
        expected_revision=state["shots"]["shot-2"][-1]["revision"],
        acceptance_criteria=["allow_freeze:0-24:홀드"],
        reason="선언",
        source="test",
        selection="explicit-lab",
    )
    for shot_id in ("shot-1", "shot-2", "shot-3"):
        pipeline.render_shot(pid, shot_id, review=True)
    composition = pipeline.compose_final(pid)
    # 조립 기록의 출력 대신 길이가 짧은 영상을 같은 키로 가장해 넣을 수는 없다(sidecar 불변). 대신 다른 길이의 타임라인으로 검사하면 실패해야 한다.
    store_state = store.get_production(pid)
    timeline = store_state["timelines"][-1]
    assert timeline["duration_frames"] == 72
    forged_timeline = store.update_timeline(
        pid,
        expected_revision=timeline["revision"],
        values={
            "duration_frames": 80,
            "shots": [*timeline["shots"][:-1], {**timeline["shots"][-1], "end_f": 80}],
        },
    )
    assert forged_timeline["duration_frames"] == 80
    result = pipeline.reviewer.check_final(
        pid, composition["composition_key"], expect_audio=False
    )
    assert (
        result["report"]["criteria"]["frame_count"] == "fail"
        and result["report"]["verdict"] == "fail"
    )
