"""Local measurement evaluator bound to run_review: Apple Vision OCR, ffmpeg frame/audio analysis.

It is a rule-based instrument, not a model: no generative model of this host and no conversation
model produced these verdicts. Thresholds live in code and every row cites the measured numbers.
Only a subset of the rubric is measurable. Criteria that need perception (does the scene match the
request, does it look good, does it sound good, do audio and picture line up) stay `unknown`;
measurement may fail a criterion, and passes a row only where the criterion itself is objective
(caption text present, spelled as planned, big enough, readable). A pass elsewhere is never
manufactured from a proxy, so the overall verdict cannot become a pass from this evaluator alone.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ttakkari.creative.render import ffmpeg
from ttakkari.creative.v2 import audio_measure
from ttakkari.creative.v2.production_review import EvaluatorBinding

EVALUATOR_ID = "local-measure"
OCR_SOURCE = Path(__file__).parent / "tools" / "ocr_lines.swift"
OCR_ENV = "TTAKKARI_OCR_BINARY"
_EVIDENCE_KEYS = ("path", "sha256", "source_sha256", "kind", "start_time", "end_time")

# Body caption text shorter than this fraction of frame height is hard to read (24 px at 1080p).
CAPTION_MIN_HEIGHT = 0.022
CAPTION_MIN_CONTRAST = 4.5
SAFE_MARGIN = 0.02
MATCH_RATIO = 0.95  # similarity at which an OCR line counts as the planned text (punctuation and spacing ignored)
TYPO_RATIO = 0.7  # between this and MATCH_RATIO a confidently read line is a misspelling of the planned text
MIN_TYPO_CONFIDENCE = 0.9  # a weakly read line may be OCR noise, so it never proves a misspelling
MAX_CHARS_PER_SECOND = 14.0
FREEZE_SECONDS = 2.5
BLACK_LUMA = 8.0
MOTION_STRATEGIES = {"code_motion", "frame_animation_2d", "scene_3d", "generative_video"}
_PROXY = (160, 90)


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.casefold())


def ocr_binary() -> Path | None:
    """Compiled OCR helper. Built once per source hash with the host swift toolchain; None if unavailable."""
    override = os.environ.get(OCR_ENV)
    if override:
        return Path(override) if Path(override).is_file() else None
    swiftc = shutil.which("swiftc")
    if swiftc is None or not OCR_SOURCE.is_file():
        return None
    digest = hashlib.sha256(OCR_SOURCE.read_bytes()).hexdigest()[:16]
    target = Path(tempfile.gettempdir()) / "ttakkari-ocr" / digest / "ocr_lines"
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(f".{os.getpid()}.tmp")
    result = subprocess.run(
        [swiftc, "-O", str(OCR_SOURCE), "-o", str(partial)], capture_output=True, text=True, timeout=600, check=False,
    )
    if result.returncode or not partial.is_file():
        partial.unlink(missing_ok=True)
        return None
    partial.replace(target)
    return target


def run_ocr(paths: list[str], binary: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(paths), 16):
        batch = paths[start : start + 16]
        result = subprocess.run([str(binary), *batch], capture_output=True, text=True, timeout=300, check=False)
        if result.returncode:
            raise RuntimeError(f"ocr failed ({result.returncode}): {result.stderr[-300:]}")
        for line in result.stdout.splitlines():
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise TypeError("OCR returned a malformed image record")
            if not isinstance(row.get("lines"), list):
                reason = row.get("error") or "missing lines array"
                raise TypeError(f"OCR image decode failed for {row['path']}: {reason}")
            out[row["path"]] = row
        missing = set(batch) - out.keys()
        if missing:
            raise RuntimeError(f"OCR returned no image record for {min(missing)}")
    return out


def _contrast(image: Image.Image, line: dict[str, Any], width: int, height: int) -> float:
    """WCAG contrast between the brightest and darkest 5% of pixels inside the text box."""
    x0, x1 = int(line["x"] * width) - 2, int((line["x"] + line["w"]) * width) + 2
    y0, y1 = int((1 - line["y"] - line["h"]) * height) - 2, int((1 - line["y"]) * height) + 2
    crop = np.asarray(image.crop((max(0, x0), max(0, y0), min(width, x1), min(height, y1))).convert("RGB"), dtype=np.float64) / 255
    if crop.size == 0:
        return 0.0
    linear = np.where(crop <= 0.03928, crop / 12.92, ((crop + 0.055) / 1.055) ** 2.4)
    luminance = linear @ np.array([0.2126, 0.7152, 0.0722])
    high, low = np.percentile(luminance, 95), np.percentile(luminance, 5)
    return float((high + 0.05) / (low + 0.05))


def _best_line(expected: str, lines: list[dict[str, Any]]) -> tuple[float, dict[str, Any] | None]:
    target = _norm(expected)
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    if not target:
        return best
    for line in lines:
        text = _norm(line["text"])
        ratio = difflib.SequenceMatcher(None, target, text).ratio()
        if ratio > best[0]:
            best = (ratio, line)
    return best


def frame_motion(media: Path, fps: float) -> dict[str, Any]:
    """Per-frame changed-pixel counts and luma on a small gray proxy, streamed so long media stay bounded."""
    width, height = _PROXY
    size = width * height
    proc = subprocess.Popen(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(media), "-an",
         "-vf", f"scale={width}:{height},format=gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    previous, changed, luma = None, [], []
    while chunk := proc.stdout.read(size):
        if len(chunk) < size:
            break
        frame = np.frombuffer(chunk, dtype=np.uint8).astype(np.int16)
        luma.append(float(frame.mean()))
        changed.append(0 if previous is None else int((np.abs(frame - previous) > 2).sum()))
        previous = frame
    proc.stdout.close()
    error = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() or not luma:
        raise RuntimeError(f"frame decode failed: {error[-300:]}")
    return {"fps": fps, "changed": changed, "luma": luma}


def _longest_freeze(changed: list[int], start: int, end: int) -> int:
    best = run = 0
    for value in changed[start:end]:
        run = run + 1 if value < 5 else 0
        best = max(best, run)
    return best


def _evidence(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item[k] for k in _EVIDENCE_KEYS}


def _row(criterion: str, scene: str, verdict: str, uncertainty: float, rationale: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "criterion_id": criterion, "scene_ids": [scene], "verdict": verdict, "uncertainty": uncertainty,
        "rationale": rationale[:1200], "evidence": [_evidence(e) for e in evidence],
    }


def _unknown(criterion: str, scene: str, reason: str) -> dict[str, Any]:
    return _row(criterion, scene, "unknown", 1.0, reason, [])


class _Measurements:
    """Everything measured once per review so criteria rows only combine numbers."""

    def __init__(self, package: dict[str, Any], binary: Path | None):
        self.package = package
        self.context = package.get("context") or {}
        self.scenes = {s["scene_id"]: s for s in self.context.get("scenes", [])}
        self.targets = {t["scene_id"]: t for t in package["rubric"]["targets"]}
        self.frames = package["frames"]
        self.problems: list[str] = []
        self.ocr: dict[str, dict[str, Any]] = {}
        if binary is None:
            self.problems.append("OCR 도구(swiftc/Vision)를 쓸 수 없어 글자 판독을 하지 못했다")
        elif self.frames:
            try:
                self.ocr = run_ocr([f["path"] for f in self.frames], binary)
            except (RuntimeError, OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
                self.problems.append(f"OCR 실행 실패: {type(exc).__name__}: {exc}"[:300])
        media = Path(package["media"]["path"])
        self.duration = package["media"]["end_time"]
        self.motion: dict[str, Any] | None = None
        try:
            fps = max(f["fps"] for f in self.frames)
            self.motion = frame_motion(media, fps)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.problems.append(f"프레임 움직임 측정 실패: {type(exc).__name__}: {exc}"[:300])
        self.levels: dict[str, dict[str, float] | None] = {}
        self.dips: list[dict[str, float]] = []
        self.clipped = 0.0
        self.audio_ok = False
        if package.get("audio"):
            try:
                samples = audio_measure.decode_pcm(Path(package["audio"]["path"]))
                self.levels = audio_measure.scene_levels(samples, [self.targets[i] for i in self.targets])
                self.dips = audio_measure.find_dips(samples)
                self.clipped = audio_measure.clipped_fraction(samples)
                self.audio_ok = True
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                self.problems.append(f"오디오 측정 실패: {type(exc).__name__}: {exc}"[:300])

    def interior(self, scene_id: str) -> list[dict[str, Any]]:
        target = self.targets[scene_id]
        margin = min(0.5, 0.1 * (target["end_time"] - target["start_time"]))
        return [f for f in self.frames if f["scene_id"] == scene_id and target["start_time"] + margin <= f["start_time"] <= target["end_time"] - margin]

    def expected_text(self, scene_id: str) -> dict[str, str]:
        scene = self.scenes.get(scene_id) or {}
        title, narration = str(scene.get("title") or "").strip(), str(scene.get("narration") or "").strip()
        source = scene.get("caption_source")
        if source == "narration" and narration:
            return {"body": narration, "title": title} if title else {"body": narration}
        return {"body": title} if title else {}


def _text_row(m: _Measurements, scene_id: str, defects: list[dict[str, Any]]) -> dict[str, Any]:
    cid = "text_legibility"
    expected = m.expected_text(scene_id)
    if m.problems and not m.ocr:
        return _unknown(cid, scene_id, "; ".join(m.problems))
    if not expected:
        return _unknown(cid, scene_id, "계획에 이 장면의 자막 문구가 없어 비교할 기준이 없다")
    frames = m.interior(scene_id)
    if not frames:
        return _unknown(cid, scene_id, "장면 안쪽 프레임 표본이 없다")
    body_heights, found, typos, contrasts, offsafe, confidences = [], [], [], [], [], []
    weak = 0
    for frame in frames:
        scan = m.ocr.get(frame["path"])
        if not scan or "lines" not in scan:
            continue
        width, height = scan["width"], scan["height"]
        ratio, line = _best_line(expected["body"], scan["lines"])
        if line is None:
            continue
        if ratio >= MATCH_RATIO:
            found.append(frame)
            confidences.append(line["confidence"])
            body_heights.append(line["h"])
            with Image.open(frame["path"]) as image:
                contrasts.append(_contrast(image, line, width, height))
            if not (SAFE_MARGIN <= line["x"] and line["x"] + line["w"] <= 1 - SAFE_MARGIN
                    and SAFE_MARGIN <= line["y"] and line["y"] + line["h"] <= 1 - SAFE_MARGIN):
                offsafe.append(frame)
        elif ratio >= TYPO_RATIO and line["confidence"] >= MIN_TYPO_CONFIDENCE:
            typos.append((frame, line["text"], ratio))
        elif ratio >= TYPO_RATIO:
            weak += 1
    shown = f"기대 문구 '{expected['body'][:40]}'"
    if typos and not found:
        frame, text, ratio = typos[0]
        reason = f"{shown}와 다르게 읽힘: OCR '{text[:60]}' (유사도 {ratio:.2f}). 오탈자 또는 깨진 글자 의심"
        defects.append(_defect("fake_text", scene_id, "captions", [frame], reason))
        return _row(cid, scene_id, "fail", 0.2, reason, [frame])
    if not found and weak:
        return _unknown(cid, scene_id, f"{shown}와 비슷한 줄이 {weak}장에서 읽혔지만 OCR 신뢰도가 낮아 오탈자인지 판독 오류인지 가리지 못했다")
    if not found:
        reason = f"{shown}가 장면 안쪽 프레임 {len(frames)}장 어디에서도 읽히지 않았다"
        defects.append(_defect("text_legibility", scene_id, "captions", frames[:1], reason))
        return _row(cid, scene_id, "fail", 0.3, reason, frames)
    frame_h = m.ocr[found[0]["path"]]["height"]
    median_h = statistics.median(body_heights)
    low_contrast = min(contrasts)
    px = median_h * frame_h
    problems = []
    if median_h < CAPTION_MIN_HEIGHT:
        problems.append(f"본문 자막 높이 {px:.0f}px은 프레임 높이 {frame_h}px의 {median_h * 100:.1f}%로 기준 {CAPTION_MIN_HEIGHT * 100:.1f}%"
                        f"({CAPTION_MIN_HEIGHT * frame_h:.0f}px)보다 작다")
    if low_contrast < CAPTION_MIN_CONTRAST:
        problems.append(f"자막 대비 {low_contrast:.1f}:1이 기준 {CAPTION_MIN_CONTRAST}:1 미만")
    if offsafe:
        problems.append(f"자막이 안전영역({SAFE_MARGIN * 100:.0f}%)을 벗어난 프레임 {len(offsafe)}장")
    seen = f"장면 안쪽 {len(frames)}장 중 {len(found)}장에서 '{expected['body'][:30]}' 판독, 높이 {px:.0f}px, 대비 {low_contrast:.1f}:1"
    if problems:
        reason = "; ".join(problems) + f" ({seen})"
        defects.append(_defect("text_legibility", scene_id, "captions", found, reason))
        return _row(cid, scene_id, "fail", 0.15, reason, found)
    uncertainty = 0.1 if min(confidences) >= 0.9 else 0.2
    note = " 단 OCR 신뢰도가 낮은 프레임이 있다" if uncertainty > 0.1 else ""
    return _row(cid, scene_id, "pass", uncertainty, f"{seen}. 오탈자·크기·대비·안전영역 기준 통과.{note}", found)


def _defect(code: str, scene_id: str, layer: str, evidence: list[dict[str, Any]], description: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "scene_id": scene_id, "layer": layer, "description": description[:600],
            "evidence": [_evidence(e) for e in evidence], **extra}


def _intent_row(m: _Measurements, scene_id: str, titles: dict[str, str], defects: list[dict[str, Any]]) -> dict[str, Any]:
    cid = "intent_alignment"
    frames = m.interior(scene_id)
    if not m.ocr or not frames:
        return _unknown(cid, scene_id, "비전 모델이 없어 장면 내용과 서사 목표의 일치는 판정하지 못했다 (글자 판독 자료도 없음)")
    wrong: list[tuple[dict[str, Any], str]] = []
    for frame in frames:
        scan = m.ocr.get(frame["path"])
        if not scan:
            continue
        own, _ = _best_line(titles.get(scene_id, ""), scan["lines"])
        for other_id, other_title in titles.items():
            if other_id == scene_id:
                continue
            ratio, _ = _best_line(other_title, scan["lines"])
            if ratio >= MATCH_RATIO and ratio > own:
                wrong.append((frame, other_id))
                break
    if wrong and len(wrong) * 2 >= len(frames):
        reason = f"장면 {scene_id} 구간 프레임 {len(frames)}장 중 {len(wrong)}장이 장면 {wrong[0][1]}의 제목을 보여 준다"
        defects.append(_defect("intent_alignment", scene_id, "visual", [f for f, _ in wrong], reason))
        return _row(cid, scene_id, "fail", 0.2, reason, [f for f, _ in wrong])
    return _unknown(cid, scene_id, "비전 모델이 없어 장면 내용과 서사 목표의 일치는 판정하지 못했다. 다른 장면의 제목이 노출되는 불일치는 발견되지 않았다")


def _motion_row(m: _Measurements, scene_id: str, defects: list[dict[str, Any]]) -> dict[str, Any]:
    cid = "motion"
    strategy = (m.scenes.get(scene_id) or {}).get("strategy")
    if m.motion is None:
        return _unknown(cid, scene_id, "; ".join(m.problems) or "프레임 움직임을 측정하지 못했다")
    target = m.targets[scene_id]
    fps = m.motion["fps"]
    start, end = int(target["start_time"] * fps), int(target["end_time"] * fps)
    frames = max(1, end - start - 1)
    freeze = _longest_freeze(m.motion["changed"], start + 1, end) / fps
    active = sum(c >= 5 for c in m.motion["changed"][start + 1 : end]) / frames
    note = f"가장 긴 정지 구간 {freeze:.2f}초, 움직임 프레임 비율 {active * 100:.0f}% (자연스러움은 판정하지 못함)"
    if strategy in MOTION_STRATEGIES and freeze >= FREEZE_SECONDS:
        reason = f"움직임 장면인데 {freeze:.2f}초 동안 화면이 멈춰 있다 ({note})"
        defects.append(_defect("motion", scene_id, "motion", [m.package["media"]], reason))
        return _row(cid, scene_id, "fail", 0.15, reason, [m.package["media"]])
    return _row(cid, scene_id, "unknown", 1.0, f"비전 모델이 없어 동작의 자연스러움은 판정하지 못했다. 측정: {note}", [m.package["media"]])


def _transition_row(m: _Measurements, scene_id: str, defects: list[dict[str, Any]]) -> dict[str, Any]:
    cid = "transition"
    if m.motion is None:
        return _unknown(cid, scene_id, "; ".join(m.problems) or "프레임을 측정하지 못했다")
    target = m.targets[scene_id]
    fps, luma = m.motion["fps"], m.motion["luma"]
    lo, hi = max(0, int((target["start_time"] - 0.1) * fps)), min(len(luma), int((target["start_time"] + 0.25) * fps) + 1)
    dark = [i for i in range(lo, hi) if luma[i] < BLACK_LUMA] if target["start_time"] > 0 else []
    if dark:
        reason = f"장면 {scene_id} 경계 {target['start_time']:.2f}초 부근 프레임 {len(dark)}장이 검다 (평균 밝기 < {BLACK_LUMA:g})"
        defects.append(_defect("transition", scene_id, "transition", [m.package["media"]], reason))
        return _row(cid, scene_id, "fail", 0.15, reason, [m.package["media"]])
    return _row(cid, scene_id, "unknown", 1.0, "경계 부근에 검은 프레임은 없다. 전환의 자연스러움은 영상을 보지 못해 판정하지 못했다", [m.package["media"]])


def _audio_row(m: _Measurements, scene_id: str, defects: list[dict[str, Any]]) -> dict[str, Any]:
    cid = "audio_quality"
    audio = m.package.get("audio")
    if not audio or not m.audio_ok:
        return _unknown(cid, scene_id, "; ".join(m.problems) or "오디오 트랙이 없어 측정하지 못했다")
    target = m.targets[scene_id]
    window = {"start": target["start_time"], "end": target["end_time"]}
    state, reason = audio_measure.level_verdict(m.levels.get(scene_id))
    problems = []
    if state == "fail":
        problems.append(reason)
        measured = m.levels[scene_id] or {}
        defects.append(_defect("audio_level", scene_id, "audio", [audio], f"장면 {scene_id} {reason}", window=window,
                               measured=measured, target_lufs=audio_measure.TARGET_LUFS))
    for dip in m.dips:
        if target["start_time"] <= dip["at"] < target["end_time"]:
            text = (f"{dip['at']:.2f}초 부근 {dip['start']:.2f}~{dip['end']:.2f}초에 음량이 평소(중앙값 {dip['reference_db']} dB)보다 "
                    f"{dip['depth_db']} dB 푹 꺼진다. 루프 이음매 의심")
            problems.append(text)
            defects.append(_defect("audio_seam", scene_id, "audio", [audio], f"장면 {scene_id}: {text}", window=dict(dip)))
    if m.clipped > 0.001:
        problems.append(f"샘플 클리핑 {m.clipped * 100:.2f}%")
    if problems:
        return _row(cid, scene_id, "fail", 0.1, "; ".join(problems), [audio])
    return _row(cid, scene_id, "unknown", 1.0, f"측정 이상 없음 ({reason}, 음량 꺼짐 없음). 음색·균형은 듣지 못해 판정하지 못했다", [audio])


def _pacing_row(m: _Measurements, scene_id: str, defects: list[dict[str, Any]]) -> dict[str, Any]:
    cid = "pacing"
    target = m.targets[scene_id]
    seconds = target["end_time"] - target["start_time"]
    scene = m.scenes.get(scene_id) or {}
    text = _norm(str(scene.get("narration") or scene.get("title") or ""))
    cps = len(text) / seconds if seconds > 0 else math.inf
    planned = m.context.get("planned_seconds")
    note = f"자막 {len(text)}자/{seconds:.1f}초={cps:.1f}자/초"
    if planned and abs(m.duration - planned) > 0.5 + 0.05 * len(m.scenes):
        reason = f"전체 길이 {m.duration:.2f}초가 계획 {planned:.2f}초와 어긋난다"
        return _row(cid, scene_id, "fail", 0.1, reason, [m.package["media"]])
    if cps > MAX_CHARS_PER_SECOND:
        reason = f"{note}로 읽는 속도 상한 {MAX_CHARS_PER_SECOND:g}자/초를 넘는다"
        return _row(cid, scene_id, "fail", 0.2, reason, [m.package["media"]])
    return _row(cid, scene_id, "unknown", 1.0, f"길이와 읽기 속도 기준은 통과({note}, 전체 {m.duration:.2f}초/계획 {planned}). 서사 리듬은 판정하지 못했다", [m.package["media"]])


def execute(package: dict[str, Any], payloads: dict[str, Any]) -> dict[str, Any]:
    del payloads  # measurements read the hash-validated package files directly
    binary = ocr_binary()
    m = _Measurements(package, binary)
    titles = {i: str(s.get("title") or "") for i, s in m.scenes.items()}
    defects: list[dict[str, Any]] = []
    stages: dict[str, dict[str, Any]] = {"semantic_qa": {"criteria": [], "defects": []}, "viewing_qa": {"criteria": [], "defects": []}}
    handlers = {
        "text_legibility": lambda sid: _text_row(m, sid, defects),
        "intent_alignment": lambda sid: _intent_row(m, sid, titles, defects),
        "motion": lambda sid: _motion_row(m, sid, defects),
        "transition": lambda sid: _transition_row(m, sid, defects),
        "audio_quality": lambda sid: _audio_row(m, sid, defects),
        "pacing": lambda sid: _pacing_row(m, sid, defects),
    }
    manual = {
        "style_continuity": "비전 모델이 없어 장면 사이 스타일 일관성은 판정하지 못했다",
        "audio_sync": "소리와 화면의 타이밍은 청취·시청 없이 판정하지 못했다",
        "viewing_value": "완성 영상을 사람이 보는 방식으로 재생해 판단하지 못했다",
        "character_consistency": "비전 모델이 없어 캐릭터 일관성은 판정하지 못했다",
        "factual_accuracy": "사실 정확성은 출처 대조 없이 판정하지 못했다",
        "period_accuracy": "시대 고증은 출처 대조 없이 판정하지 못했다",
        "source_fidelity": "원본 대조 자료가 없어 판정하지 못했다",
        "beat_sync": "비트와 컷의 일치는 청취 없이 판정하지 못했다",
        "hook": "초반 훅은 시청 없이 판정하지 못했다",
    }
    for criterion in package["rubric"]["criteria"]:
        stage = stages[criterion["stage"]]
        for scene_id in m.targets:
            handler = handlers.get(criterion["id"])
            row = handler(scene_id) if handler else _unknown(criterion["id"], scene_id, manual.get(criterion["id"], "이 기준을 측정하는 도구가 없다"))
            stage["criteria"].append(row)
    seen = set()
    for defect in defects:
        key = (defect["code"], defect["scene_id"], json.dumps(defect.get("window"), sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        stage = "viewing_qa" if defect["code"] in {"motion", "transition", "audio_level", "audio_seam"} else "semantic_qa"
        stages[stage]["defects"].append(defect)
    return {
        "stages": stages,
        "execution": {
            "engine": EVALUATOR_ID,
            "tools": {"ocr": "Apple Vision VNRecognizeTextRequest (ko-KR, en-US)" if binary else "unavailable",
                      "media": "ffmpeg decode, ebur128, numpy frame/level analysis"},
            "model_used": False,
            "frame_count": len(package["frames"]),
            "limits": "no vision or listening model: intent, style, motion naturalness, audio timbre/sync and viewing value stay unknown",
            "problems": m.problems,
        },
    }


def local_measure_binding(version: str = "1") -> EvaluatorBinding:
    """Server-owned binding. Independent of the generators: it is a measurement program, not a generation engine."""
    return EvaluatorBinding(
        id=EVALUATOR_ID,
        version=version,
        source="Apple Vision OCR + ffmpeg/numpy measurements over delivered frames and audio; no generative or conversation model",
        independent=True,
        execute=execute,
    )
