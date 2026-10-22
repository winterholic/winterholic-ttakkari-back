"""기계 검수: 키프레임 표본과 전수 기계 검사, 그리고 샷 output hash에 묶이는 ReviewReceipt.

이 검수는 장면의 의미·미감을 판정하지 않는다. 디코드·프레임 수·해상도·검은/정지 구간·오디오 피크/무음·길이·싱크 같은
기계가 확정할 수 있는 항목만 pass/fail로 쓰고, 근거가 부족한 항목은 unknown으로 둔다. hard criterion이 하나라도 fail이면 fail이고
핵심 항목이 unknown이면 review_pending이다(ADR §8). 사람 시청·청취와 독립 검수는 대체하지 않으며 approval은 항상 null이다.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from ttakkari.creative.director import DirectorStore
from ttakkari.creative.engines import media

REVIEWER = "machine-check/v1"
KEYFRAME_CAP = 16
CORE_CRITERIA = ("decode", "frame_count", "duration")
BLACK_MIN_SECONDS = 0.1
FREEZE_MIN_SECONDS = 0.5
SILENCE_FAIL_SECONDS = 1.0
CLIP_LIMIT_DB = -0.1
_ALLOW = re.compile(r"^allow_(black|freeze|silence):(\d+)-(\d+):(.+)$")


def parse_allowances(
    criteria: list[str], fps_num: int, fps_den: int
) -> dict[str, list[dict[str, Any]]]:
    """acceptance_criteria의 `allow_<kind>:<시작프레임>-<끝프레임>:<이유>` 선언을 초 구간으로 바꾼다. 이유가 없으면 무시한다."""
    found: dict[str, list[dict[str, Any]]] = {"black": [], "freeze": [], "silence": []}
    for text in criteria:
        match = _ALLOW.match(text.strip())
        if not match or not match.group(4).strip():
            continue
        kind, start, end, reason = (
            match.group(1),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4).strip(),
        )
        if end <= start:
            continue
        found[kind].append(
            {
                "start_f": start,
                "end_f": end,
                "start": start * fps_den / fps_num,
                "end": end * fps_den / fps_num,
                "reason": reason,
            }
        )
    return found


def _covered(
    start: float, end: float | None, allowed: list[dict[str, Any]], eps: float = 0.05
) -> bool:
    stop = end if end is not None else start
    return any(a["start"] - eps <= start and stop <= a["end"] + eps for a in allowed)


def check_media(
    path: Path,
    *,
    expected_frames: int,
    width: int,
    height: int,
    fps_num: int,
    fps_den: int,
    frame_exact: bool,
    expect_audio: bool,
    allow: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """파일 하나에 대한 전수 기계 검사. criteria와 근거 수치를 함께 돌려준다."""
    criteria: dict[str, str] = {}
    notes: dict[str, Any] = {}
    fps = fps_num / fps_den
    expected_seconds = expected_frames / fps
    frame_tolerance = 1 / fps
    audio_tolerance = max(frame_tolerance, 0.05)
    try:
        info = media.probe_media(path)
    except media.MediaToolError as exc:
        return {"criteria": {"decode": "fail"}, "measurements": {"error": str(exc)}}
    video = info.get("video")
    scan = (
        media.decode_scan(path)
        if video
        else {"ok": False, "frames": None, "errors": ["영상 스트림이 없습니다."]}
    )
    criteria["decode"] = "pass" if scan["ok"] and (scan["frames"] or 0) > 0 else "fail"
    notes["decode"] = {"frames": scan["frames"], "errors": scan["errors"]}
    if video is None:
        return {"criteria": criteria, "measurements": notes}
    # 프레임 수·해상도·fps
    if frame_exact:
        criteria["frame_count"] = (
            "pass" if scan["frames"] == expected_frames else "fail"
        )
        criteria["resolution"] = (
            "pass" if (video["width"], video["height"]) == (width, height) else "fail"
        )
        criteria["fps"] = (
            "pass"
            if video["fps"] is not None and abs(video["fps"] - fps) < 0.01
            else "fail"
        )
    else:
        criteria["frame_count"] = "unknown"
        criteria["resolution"] = "unknown"
        criteria["fps"] = "unknown"
        notes["conform_required"] = (
            "생성형 프로필은 프레임 수·해상도를 보장하지 않아 조립 단계의 컨폼이 맞춘다."
        )
    notes["expected"] = {
        "frames": expected_frames,
        "width": width,
        "height": height,
        "fps": fps,
    }
    notes["measured"] = {
        "frames": scan["frames"],
        "width": video["width"],
        "height": video["height"],
        "fps": video["fps"],
    }
    # 길이
    duration = info["duration"]
    if frame_exact:
        criteria["duration"] = (
            "pass"
            if abs(duration - expected_seconds) <= max(frame_tolerance, 0.05)
            else "fail"
        )
    else:
        criteria["duration"] = "unknown"
    notes["duration"] = {
        "container_seconds": duration,
        "expected_seconds": round(expected_seconds, 4),
    }
    # 검은 프레임·정지
    detect = media.detect_black_freeze(path, total_duration=info["duration"])
    black = [
        b
        for b in detect["black"]
        if b["duration"] >= BLACK_MIN_SECONDS
        and not _covered(b["start"], b["end"], allow["black"])
    ]
    freeze = [
        f
        for f in detect["freeze"]
        if (f["duration"] is None or f["duration"] >= FREEZE_MIN_SECONDS)
        and not _covered(f["start"], f["end"], allow["freeze"])
    ]
    criteria["black_frames"] = "fail" if black else "pass"
    criteria["freeze"] = "fail" if freeze else "pass"
    notes["black"] = detect["black"]
    notes["black_unexcused"] = black
    notes["freeze"] = detect["freeze"]
    notes["freeze_unexcused"] = freeze
    # 오디오
    audio = media.audio_levels(path)
    if not audio["has_audio"]:
        if expect_audio:
            criteria["audio_present"] = "fail"
    else:
        criteria["audio_present"] = "pass"
        peak = audio["max_volume_db"]
        criteria["audio_peak"] = (
            "unknown" if peak is None else ("fail" if peak >= CLIP_LIMIT_DB else "pass")
        )
        long_silence = [
            s
            for s in audio["silence"]
            if (s["duration"] is None or s["duration"] >= SILENCE_FAIL_SECONDS)
            and not _covered(s["start"], s["end"], allow["silence"])
        ]
        whole_silent = peak is not None and peak == float("-inf")
        criteria["audio_silence"] = "fail" if long_silence or whole_silent else "pass"
        notes["audio"] = {k: v for k, v in audio.items() if k != "returncode"}
        notes["audio"]["silence_unexcused"] = long_silence
        durations = media.stream_durations(path)
        notes["stream_durations"] = durations
        if durations["video"] is not None and durations["audio"] is not None:
            criteria["av_sync"] = (
                "pass"
                if abs(durations["audio"] - durations["video"]) <= audio_tolerance
                else "fail"
            )
        else:
            criteria["av_sync"] = "unknown"
    return {"criteria": criteria, "measurements": notes}


def find_attempt_review(
    state: dict[str, Any], attempt_id: str
) -> dict[str, Any] | None:
    """attempt에 묶인 기계 검수 {review, report}를 돌려준다. 같은 바이트를 낸 다른 샷의 검수와 섞지 않는다."""
    reports = state.get("review_reports", {})
    for review_id, report in reports.items():
        if report.get("kind") == "shot" and report.get("attempt_id") == attempt_id:
            review = next(
                (r for r in state["reviews"] if r["review_id"] == review_id), None
            )
            if review is not None:
                return {"review": review, "report": report}
    return None


def verdict_of(criteria: dict[str, str]) -> str:
    if any(v == "fail" for v in criteria.values()):
        return "fail"
    if any(criteria.get(key, "unknown") == "unknown" for key in CORE_CRITERIA):
        return "review_pending"
    return "pass"


def sample_keyframes(
    series: dict[str, Any], *, frames: int, cues: list[int], cap: int = KEYFRAME_CAP
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """샷 안 프레임 번호(0부터)를 이유와 함께 고른다. 반환: (선택, 상한으로 버린 후보)."""
    reasons: dict[int, set[str]] = {}

    def add(frame: int, reason: str) -> None:
        if 0 <= frame < frames:
            reasons.setdefault(frame, set()).add(reason)

    for frame in (0, 1, frames - 2, frames - 1):
        add(frame, "shot_boundary")
    diff = series.get("diff") or []
    if len(diff) >= 3:
        ordered = sorted(diff[1:])
        median = ordered[len(ordered) // 2]
        threshold = max(12.0, 6 * median)
        for index in range(1, len(diff)):
            if diff[index] > threshold:
                add(index - 1, "cut_before")
                add(index, "cut_after")
        inner = list(range(2, len(diff) - 1)) or list(range(1, len(diff)))
        by_motion = sorted(inner, key=lambda i: diff[i])
        for frame in by_motion[-2:]:
            add(frame, "motion_max")
        for frame in by_motion[:2]:
            add(frame, "motion_min")
    for cue in cues:
        add(cue, "audio_event")
    if frames >= 8:
        for fraction in (0.25, 0.5, 0.75):
            add(int(frames * fraction), "uniform")
    priority = {
        "shot_boundary": 0,
        "audio_event": 1,
        "cut_before": 2,
        "cut_after": 2,
        "motion_max": 3,
        "motion_min": 4,
        "uniform": 5,
    }
    ranked = sorted(
        reasons.items(), key=lambda item: (min(priority[r] for r in item[1]), item[0])
    )
    selected = ranked[:cap]
    dropped = ranked[cap:]
    pack = lambda rows: [{"frame": f, "reasons": sorted(r)} for f, r in sorted(rows)]
    return pack(selected), pack(dropped)


class Reviewer:
    """샷 attempt 출력과 최종 합성물을 검수하고 ReviewReceipt를 output hash에 묶는다."""

    def __init__(self, store: DirectorStore, work_root: Path) -> None:
        self.store = store
        self.work_root = Path(work_root)
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def review_attempt(
        self, pid: str, attempt_id: str, *, profile_exact: bool, force: bool = False
    ) -> dict[str, Any]:
        state = self.store.get_production(pid)
        attempt = next(
            (a for a in state["attempts"] if a["attempt_id"] == attempt_id), None
        )
        if attempt is None:
            raise FileNotFoundError("시도가 없습니다.")
        if attempt["state"] != "review_pending" or not attempt["outputs"]:
            raise ValueError("검수 대기 상태의 출력이 있는 시도만 검수할 수 있습니다.")
        output_sha = attempt["outputs"][0]["sha256"]
        if not force:
            # 다른 샷이 같은 바이트를 낼 수 있으므로 재사용은 output hash가 아니라 attempt 기준으로 찾는다.
            found = find_attempt_review(state, attempt_id)
            if found:
                return {**found, "reused": True}
        spec = json.loads(self.store.read_artifact(pid, attempt["input_sha256"]))
        allow = parse_allowances(
            spec.get("acceptance_criteria", []), spec["fps"]["num"], spec["fps"]["den"]
        )
        data = self.store.read_artifact(pid, output_sha)
        fmt = spec["format"]
        with tempfile.TemporaryDirectory(dir=self.work_root, prefix="review-") as tmp:
            clip = Path(tmp) / "output.mp4"
            clip.write_bytes(data)
            checked = check_media(
                clip,
                expected_frames=spec["range"]["frames"],
                width=fmt["width"],
                height=fmt["height"],
                fps_num=spec["fps"]["num"],
                fps_den=spec["fps"]["den"],
                frame_exact=profile_exact,
                expect_audio=bool(spec.get("expect_audio")),
                allow=allow,
            )
            keyframes, dropped, series_frames = self._keyframes(
                pid, clip, spec, output_sha, Path(tmp)
            )
        verdict = verdict_of(checked["criteria"])
        evidence = [f"artifact:{k['sha256']}" for k in keyframes]
        manifest = {
            "schema": "ttakkari.keyframe-manifest.v1",
            "output_sha256": output_sha,
            "shot_id": spec["shot_id"],
            "shot_revision": spec["shot_revision"],
            "attempt_id": attempt_id,
            "decoded_frames": series_frames,
            "keyframes": keyframes,
            "dropped_candidates": dropped,
            "cap": KEYFRAME_CAP,
            "coverage_note": "표본은 장면 경계·컷 후보·모션 극값·오디오 이벤트·균등 지점이며 나머지 프레임은 기계 검사(디코드·검은/정지)로만 훑었다.",
        }
        manifest_record = self.store.put_artifact(
            pid,
            f"keyframes-{attempt_id[:12]}-{output_sha[:12]}.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(),
            media_type="application/json",
        )
        review = self.store.create_review(
            pid,
            output_sha256=output_sha,
            approval_event_id=None,
            criteria=checked["criteria"],
            evidence_uris=[f"artifact:{manifest_record['sha256']}", *evidence],
            reviewer=REVIEWER,
            attempt_id=attempt_id,
        )
        report = {
            "kind": "shot",
            "review_id": review["review_id"],
            "attempt_id": attempt_id,
            "shot_id": spec["shot_id"],
            "shot_revision": spec["shot_revision"],
            "output_sha256": output_sha,
            "verdict": verdict,
            "criteria": checked["criteria"],
            "measurements": checked["measurements"],
            "keyframe_manifest_sha256": manifest_record["sha256"],
            "approval": None,
            "independent": False,
            "scope_note": "기계 검사 결과다. 장면 의미·미감·사람 시청은 검수하지 않았다.",
        }
        self.store.record_sidecar(pid, "review_reports", review["review_id"], report)
        return {"review": review, "report": report, "reused": False}

    def _keyframes(
        self, pid: str, clip: Path, spec: dict[str, Any], output_sha: str, tmp: Path
    ):
        series = media.gray_motion_series(clip)
        cues = [
            c["frame"]
            for c in spec.get("cues", [])
            if 0 <= c["frame"] < spec["range"]["frames"]
        ]
        frames = series["frames"] or spec["range"]["frames"]
        selected, dropped = sample_keyframes(series, frames=frames, cues=cues)
        extracted = media.extract_frames(
            clip, [k["frame"] for k in selected], tmp / "kf"
        )
        keyframes = []
        for item in selected:
            path = extracted.get(item["frame"])
            if path is None:
                dropped.append(
                    {**item, "reasons": [*item["reasons"], "extract_failed"]}
                )
                continue
            png = path.read_bytes()
            record = self.store.put_artifact(
                pid,
                f"kf-{output_sha[:12]}-f{item['frame']:05d}.png",
                png,
                media_type="image/png",
            )
            keyframes.append(
                {
                    "frame": item["frame"],
                    "timeline_frame": spec["range"]["start_f"] + item["frame"],
                    "reasons": item["reasons"],
                    "sha256": record["sha256"],
                    "luma": series["luma"][item["frame"]]
                    if item["frame"] < len(series["luma"])
                    else None,
                    "motion": series["diff"][item["frame"]]
                    if item["frame"] < len(series["diff"])
                    else None,
                }
            )
        return keyframes, dropped, series["frames"]

    def check_final(
        self, pid: str, composition_key: str, *, expect_audio: bool
    ) -> dict[str, Any]:
        state = self.store.get_production(pid)
        composition = state["compositions"][composition_key]
        timeline = state["timelines"][-1]
        prior = next(
            (
                r
                for r in state["reviews"]
                if r["output_sha256"] == composition["output_sha256"]
                and r["shot_revision"] is None
                and r["reviewer"] == REVIEWER
            ),
            None,
        )
        if prior is not None:
            return {
                "review": prior,
                "report": state.get("review_reports", {}).get(prior["review_id"]),
                "reused": True,
            }
        data = self.store.read_artifact(pid, composition["output_sha256"])
        fmt = composition["format"]
        allow: dict[str, list[dict[str, Any]]] = {
            "black": [],
            "freeze": [],
            "silence": [],
        }
        num, den = timeline["fps_num"], timeline["fps_den"]
        for shot in timeline["shots"]:
            row = state["shots"].get(shot["shot_id"], [None])[-1]
            if not row:
                continue
            local = parse_allowances(row.get("acceptance_criteria", []), num, den)
            offset = shot["start_f"] * den / num
            for kind, items in local.items():
                for item in items:
                    allow[kind].append(
                        {
                            **item,
                            "start": item["start"] + offset,
                            "end": item["end"] + offset,
                        }
                    )
        for cue in timeline.get("cues", []):
            if cue.get("kind") == "allow_silence" and cue.get("reason"):
                allow["silence"].append(
                    {
                        "start": cue["frame"] * den / num,
                        "end": cue["end_frame"] * den / num,
                        "reason": cue["reason"],
                    }
                )
        with tempfile.TemporaryDirectory(dir=self.work_root, prefix="final-") as tmp:
            clip = Path(tmp) / "final.mp4"
            clip.write_bytes(data)
            checked = check_media(
                clip,
                expected_frames=timeline["duration_frames"],
                width=fmt["width"],
                height=fmt["height"],
                fps_num=num,
                fps_den=den,
                frame_exact=True,
                expect_audio=expect_audio,
                allow=allow,
            )
        verdict = verdict_of(checked["criteria"])
        review = self.store.create_review(
            pid,
            output_sha256=composition["output_sha256"],
            approval_event_id=None,
            criteria=checked["criteria"],
            evidence_uris=[],
            reviewer=REVIEWER,
            composition_key=composition_key,
        )
        report = {
            "kind": "final",
            "review_id": review["review_id"],
            "composition_key": composition_key,
            "output_sha256": composition["output_sha256"],
            "verdict": verdict,
            "criteria": checked["criteria"],
            "measurements": checked["measurements"],
            "approval": None,
            "independent": False,
            "scope_note": "전수 기계 검사(디코드·프레임 수·검은/정지·오디오 피크/무음·길이·싱크)다. 사람 시청·청취는 미실시이며 승인은 null이다.",
        }
        self.store.record_sidecar(pid, "review_reports", review["review_id"], report)
        return {"review": review, "report": report, "reused": False}


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
