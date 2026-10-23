"""승인된 제작 계획 → 선택 엔진 실행 → 산출 미디어·QA → 제작 기록.

수직 경로: 계획 승인(렌더 게이트) → 엔진 어댑터 사전 점검 → 계획 구간(render_plan)마다 엔진 프로젝트 실행
→ 구간 결과를 이어 붙인 최종 미디어 → 실제 미디어 QA → 계획 폴더의 `production.json`.

지금 실행되는 엔진은 v1 FFmpeg 슬라이드쇼 하나뿐이다(`engines.ADAPTERS`). 어댑터가 없는 제공자는 디스패치하지 않는다.
이미지·음악은 사용자가 올린다. 이 모듈은 생성하지 않는다.

재개 규칙: 구간 실행 키(`run_key`)는 계획 구간 키, 장면 자산 sha256, 음악 구간, 엔진 버전, 적용된 스타일로 정한다.
키가 같고 산출물이 남은 구간은 다시 실행하지 않고, 키가 바뀐 구간만 새로 실행한다.
실패한 구간은 같은 프로젝트에서 다시 시도하며 v1의 장면 캐시 덕에 끝난 장면은 다시 인코딩하지 않는다.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ttakkari.creative.render import (
    compile_caption_segments,
    dimensions,
    ffmpeg,
    inspect_media,
)
from ttakkari.creative.service import ProjectInput, Scene, Studio
from ttakkari.creative.v2 import engines
from ttakkari.creative.v2.plans import PlanStore

V1_FORMAT = "music_video"
# 렌더러는 구간 길이를 24fps 프레임으로 반올림한다. 구간이 많을수록 합계 오차가 늘어난다.
QA_BASE_TOLERANCE = 0.2
QA_PER_SEGMENT_TOLERANCE = 0.05
MUSIC_SLICE_PAD = 0.6

STYLE_SOURCES = {
    "animation": ("motion_language", "animation"),
    "transition": ("motion_language", "transition"),
    "caption_style": ("visual_identity", "caption_style"),
}
SEGMENT_ACTIVE = ("queued", "running")


class ProductionError(ValueError):
    """사용자에게 이유를 그대로 보여 줄 수 있는 제작 거부."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _providers(plan: dict[str, Any]) -> set[str]:
    return {
        need["best"]["entry_id"]
        for need in plan["match"]["needs"]
        if need["priority"] == "must" and need.get("best")
    }


# --- 사전 조건 ---------------------------------------------------------------


def dispatch_blockers(plan: dict[str, Any]) -> list[str]:
    """이 계획을 엔진으로 보낼 수 없는 이유. 비어 있으면 보낼 수 있다."""
    reasons: list[str] = []
    if not plan["render_gate"]:
        reasons.append(
            "렌더 게이트가 닫혀 있습니다. 승인, studio_runtime 범위, 필수 능력 충족이 모두 필요합니다."
        )
        return reasons
    providers = _providers(plan)
    adapter = engines.adapter_for(providers)
    if adapter is None:
        reasons.append(
            "이 계획의 필수 제공자를 실행하는 엔진 어댑터가 없습니다: "
            + ", ".join(sorted(providers))
        )
        return reasons
    pre = adapter.preflight()
    reasons += [f"필수 의존성 없음: {m}" for m in pre.missing]
    limits = adapter.limits
    if plan["aspect"] not in limits.aspects:
        reasons.append(
            f"{adapter.id}는 화면비 {', '.join(limits.aspects)}만 지원합니다(요청 {plan['aspect']})."
        )
    scenes = plan["scenes"]
    if len(scenes) < limits.min_scenes:
        reasons.append(f"{adapter.id}는 최소 {limits.min_scenes}장면이 필요합니다(현재 {len(scenes)}장면).")
    long_scenes = [s["scene_id"] for s in scenes if s["seconds"] > limits.max_scene_seconds]
    if long_scenes:
        reasons.append(
            f"장면 {', '.join(long_scenes)}이(가) 엔진 장면 한도 {limits.max_scene_seconds:g}초를 넘습니다."
        )
    for seg in plan["render_plan"]["segments"]:
        if len(seg["scene_ids"]) < limits.min_scenes and len(plan["render_plan"]["segments"]) > 1:
            reasons.append(f"구간 {seg['id']}의 장면 수가 엔진 최소 {limits.min_scenes}개에 못 미칩니다.")
        if seg["seconds"] > limits.max_segment_seconds:
            reasons.append(f"구간 {seg['id']}이(가) 엔진 구간 한도 {limits.max_segment_seconds:g}초를 넘습니다.")
    return reasons


def _style_defaults(plan: dict[str, Any], adapter: engines.EngineAdapter) -> dict[str, Any]:
    """Style DNA에서 이 엔진이 실제로 반영하는 매개변수만 골라 적용 내역을 남긴다."""
    style = plan.get("style")
    applied: dict[str, Any] = {}
    ignored: list[str] = []
    if not style:
        return {"style_dna": None, "applied": applied, "ignored": ignored}
    for name, (facet, key) in STYLE_SOURCES.items():
        value = ((style.get(facet) or {}).get("params") or {}).get(key)
        if value is None:
            continue
        if value in adapter.style_params.get(name, ()):
            applied[name] = value
        else:
            ignored.append(f"{facet}.{key}={value!r}(엔진이 받지 않는 값)")
    consumed = {facet for facet, _ in STYLE_SOURCES.values()}
    ignored += [
        f"{field}(이 엔진은 반영하지 않음)"
        for field in (
            "visual_identity",
            "materials",
            "lighting",
            "camera_language",
            "motion_language",
            "environment_rules",
            "sound_identity",
            "palette",
        )
        if style.get(field) and field not in consumed
    ]
    return {"style_dna": style.get("sha256"), "applied": applied, "ignored": ignored}


# --- 저장소 -----------------------------------------------------------------


class ProductionStore:
    """계획 폴더의 `production.json`과 업로드 자산. 계획 revision마다 실행 기록을 하나씩 둔다."""

    def __init__(self, plans: PlanStore):
        self.plans = plans

    def folder(self, plan_id: str) -> Path:
        return self.plans._folder(plan_id)

    def _path(self, plan_id: str) -> Path:
        return self.folder(plan_id) / "production.json"

    def read(self, plan_id: str) -> dict[str, Any]:
        path = self._path(plan_id)
        if path.is_file():
            return json.loads(path.read_text())
        return {
            "plan_id": str(uuid.UUID(plan_id)),
            "assets": {"scenes": {}, "voices": {}, "music": None},
            "dispatches": [],
        }

    @contextmanager
    def tx(self, plan_id: str):
        folder = self.folder(plan_id)
        if not (folder / "plan.json").is_file():
            raise FileNotFoundError("제작 계획이 없습니다.")
        with (folder / "production.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                data = self.read(plan_id)
                before = json.dumps(data, sort_keys=True)
                yield data
                if json.dumps(data, sort_keys=True) != before:
                    temp = folder / f"{uuid.uuid4()}.prod.tmp"
                    with temp.open("w") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    temp.replace(self._path(plan_id))
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def asset_path(self, plan_id: str, asset: dict[str, Any]) -> Path:
        return self.folder(plan_id) / asset["file"]


def _record(data: dict[str, Any], revision: int) -> dict[str, Any] | None:
    return next((d for d in data["dispatches"] if d["revision"] == revision), None)


def _log(record: dict[str, Any], event: str, **detail: Any) -> None:
    record["log"].append({"at": _now(), "event": event, **detail})


# --- 자산 -------------------------------------------------------------------


def put_asset(
    plans: PlanStore,
    plan_id: str,
    *,
    kind: str,
    scene_id: str | None,
    source: Path,
    name: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    """이미 검증·정규화된 파일을 계획의 제작 자산으로 등록한다. 같은 내용은 한 번만 저장한다."""
    plan = plans.get(plan_id)
    if kind in {"scene", "voice"}:
        if scene_id not in {s["scene_id"] for s in plan["scenes"]}:
            raise ProductionError(f"현재 계획 revision에 장면 {scene_id}이(가) 없습니다.")
    elif kind != "music":
        raise ProductionError("자산 종류는 scene 또는 music이어야 합니다.")
    sha = _sha256_file(source)
    store = ProductionStore(plans)
    with store.tx(plan_id) as data:
        folder = store.folder(plan_id) / "assets"
        folder.mkdir(exist_ok=True)
        relative = f"assets/{sha}{source.suffix.lower()}"
        target = store.folder(plan_id) / relative
        if not target.is_file():
            temp = folder / f"{uuid.uuid4()}.tmp"
            shutil.copyfile(source, temp)
            temp.replace(target)
        asset = {
            "id": str(uuid.uuid4()),
            "sha256": sha,
            "file": relative,
            "name": name[:160],
            "kind": "image" if kind == "scene" else "audio",
            "uploaded_at": _now(),
            "meta": meta,
        }
        if kind == "scene":
            data["assets"]["scenes"][scene_id] = asset
        elif kind == "voice":
            data.setdefault("assets", {}).setdefault("voices", {})[scene_id] = asset
        else:
            data["assets"]["music"] = asset
    return asset


def _assets_state(plan: dict[str, Any], data: dict[str, Any], store: ProductionStore) -> dict[str, Any]:
    scenes = data["assets"]["scenes"]
    ids = [s["scene_id"] for s in plan["scenes"]]
    missing = [i for i in ids if i not in scenes or not store.asset_path(plan["id"], scenes[i]).is_file()]
    music = data["assets"]["music"]
    music_problem = None
    total = plan["scenes"][-1]["end_time"] if plan["scenes"] else 0.0
    if music is None:
        music_problem = "음악 자산이 없습니다."
    else:
        duration = (music.get("meta") or {}).get("duration")
        if duration is None or duration + 0.1 < total:
            music_problem = (
                f"음악({duration or '?'}초)이 계획 길이({total:g}초)보다 짧습니다."
            )
    return {
        "scenes_total": len(ids),
        "scenes_with_asset": len(ids) - len(missing),
        "missing_scene_ids": missing,
        "music": None
        if music is None
        else {"id": music["id"], "name": music["name"], "sha256": music["sha256"], "duration": (music.get("meta") or {}).get("duration")},
        "music_problem": music_problem,
        "complete": not missing and music_problem is None,
    }


# --- 구간 실행 ----------------------------------------------------------------


def _segment_scenes(plan: dict[str, Any], segment: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {s["scene_id"]: s for s in plan["scenes"]}
    return [by_id[i] for i in segment["scene_ids"]]


def _run_key(
    plan: dict[str, Any],
    segment: dict[str, Any],
    data: dict[str, Any],
    engine: dict[str, Any],
    style: dict[str, Any],
    caption_mode: str = "narration",
) -> str:
    music = data["assets"]["music"]
    start = segment["start_frame"] / plan["profile"]["fps"]
    return _digest(
        {
            "plan_key": segment["input_sha256"],
            "assets": [data["assets"]["scenes"][i]["sha256"] for i in segment["scene_ids"]],
            "music": {"sha256": music["sha256"], "start": round(start, 3), "seconds": segment["seconds"]},
            "engine": {"id": engine["adapter"], "version": engine["version"]},
            "style": style["applied"],
            "caption_mode": caption_mode,
            "voice_assets": {
                scene_id: {
                    "sha256": (data.get("assets", {}).get("voices", {}).get(scene_id) or {}).get("sha256"),
                    "duration": ((data.get("assets", {}).get("voices", {}).get(scene_id) or {}).get("meta") or {}).get("duration"),
                }
                for scene_id in segment["scene_ids"]
            },
        }
    )


def _slice_music(source: Path, start: float, seconds: float, target: Path) -> None:
    result = subprocess.run(
        [
            ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{start:.3f}", "-t", f"{seconds + MUSIC_SLICE_PAD:.3f}",
            "-i", str(source), "-vn", "-c:a", "pcm_s16le", str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise ProductionError("음악 구간을 자르지 못했습니다: " + result.stderr[-300:])


def _create_project(
    studio: Studio,
    store: ProductionStore,
    plan: dict[str, Any],
    segment: dict[str, Any],
    data: dict[str, Any],
    style: dict[str, Any],
    caption_mode: str = "narration",
) -> str:
    project = studio.create(
        ProjectInput(
            name=f"{plan['name'][:90]} · {segment['id']}",
            brief=plan["brief"][:6000],
            content_type=V1_FORMAT,
            duration=min(max(segment["seconds"], 3), 240),
            aspect=plan["aspect"],
        )
    )
    pid = project["id"]
    fps = plan["profile"]["fps"]
    width, _height = dimensions(plan["aspect"])
    scenes: list[Scene] = []
    for scene in _segment_scenes(plan, segment):
        asset = data["assets"]["scenes"][scene["scene_id"]]
        added = studio.add_asset(
            pid,
            store.asset_path(plan["id"], asset),
            name=asset["name"],
            kind="image",
            provenance=f"v2-plan-asset:{asset['sha256']}",
            metadata=asset.get("meta") or {},
        )
        narration = str(scene.get("narration") or "").strip()
        voice = data.get("assets", {}).get("voices", {}).get(scene["scene_id"])
        voice_seconds = ((voice or {}).get("meta") or {}).get("duration")
        if caption_mode == "narration" and narration:
            caption_seconds = min(float(voice_seconds), scene["seconds"]) if voice_seconds else scene["seconds"]
            caption_style = style.get("applied", {}).get("caption_style", "bottom_card")
            caption_segments = compile_caption_segments(
                narration, caption_seconds, width, style=caption_style
            )
            caption_text = caption_segments[0]["text"] if caption_segments else ""
            timing_basis = "estimated_voice_duration" if voice_seconds else "scene_duration"
            for cue in caption_segments:
                cue["timing_basis"] = timing_basis
        else:
            caption_text = scene["title"][:160]
            caption_segments = []
        scenes.append(
            Scene(
                id=scene["scene_id"],
                duration=scene["seconds"],
                prompt=(scene["narration"] or scene["title"])[:3000],
                caption=caption_text,
                caption_segments=caption_segments,
                asset_id=added["id"],
                **style["applied"],
            )
        )
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "music.wav"
        music = data["assets"]["music"]
        _slice_music(
            store.asset_path(plan["id"], music),
            segment["start_frame"] / fps,
            segment["seconds"],
            wav,
        )
        audio = studio.add_asset(
            pid,
            wav,
            name=f"{music['name']} · {segment['id']}",
            kind="audio",
            provenance=f"v2-plan-music:{music['sha256']}",
            metadata=inspect_media(wav),
        )
    current = studio.get(pid)
    studio.edit(pid, current["revision"], scenes=scenes, music_asset_id=audio["id"])
    return pid


def _sync_segment(studio: Studio, record: dict[str, Any], segment: dict[str, Any]) -> None:
    """엔진 프로젝트의 실제 작업 상태를 구간 기록에 옮긴다."""
    if not segment.get("project_id") or not segment.get("job_id"):
        return
    try:
        project = studio.get(segment["project_id"])
    except FileNotFoundError:
        if segment["status"] != "stale":
            segment["status"] = "stale"
            segment["error"] = "엔진 프로젝트를 찾을 수 없습니다."
            _log(record, "segment_stale", segment=segment["id"])
        return
    job = next((j for j in project["render_jobs"] if j["id"] == segment["job_id"]), None)
    if job is None:
        return
    previous = segment["status"]
    if job["status"] == "succeeded":
        output = studio.path(segment["project_id"]) / job["output"]
        if output.is_file():
            segment.update(
                status="succeeded",
                error=None,
                output={
                    "file": str(output),
                    "sha256": _sha256_file(output),
                    "size": output.stat().st_size,
                    "media": job.get("media"),
                },
                metrics={
                    k: job.get(k)
                    for k in ("elapsed_seconds", "peak_rss_bytes", "cached_scenes")
                },
            )
        else:
            segment.update(status="stale", error="산출물이 사라졌습니다.")
    elif job["status"] in SEGMENT_ACTIVE:
        segment["status"] = job["status"]
    elif job["status"] == "failed":
        segment.update(status="failed", error=job.get("error"))
    if segment["status"] != previous:
        _log(record, f"segment_{segment['status']}", segment=segment["id"], job_id=job["id"], error=segment.get("error"))


def _assemble(
    plans: PlanStore, plan: dict[str, Any], record: dict[str, Any], data: dict[str, Any]
) -> None:
    """끝난 구간을 이어 붙이고 결과 미디어를 실제로 측정한다."""
    revision_dir = plans._folder(plan["id"]) / "media" / f"r{plan['revision']}"
    revision_dir.mkdir(parents=True, exist_ok=True)
    outputs = [Path(s["output"]["file"]) for s in record["segments"]]
    with tempfile.TemporaryDirectory() as tmp:
        concat = Path(tmp) / "list.txt"
        concat.write_text("\n".join(f"file '{p}'" for p in outputs))
        partial = revision_dir / f"{uuid.uuid4()}.partial.mp4"
        result = subprocess.run(
            [
                ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat),
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-movflags", "+faststart", str(partial),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    if result.returncode:
        partial.unlink(missing_ok=True)
        record["final"] = {"status": "assemble_failed", "error": result.stderr[-600:], "at": _now()}
        _log(record, "assemble_failed", error=result.stderr[-300:])
        return
    planned = sum(s["seconds"] for s in record["segments"])
    tolerance = QA_BASE_TOLERANCE + QA_PER_SEGMENT_TOLERANCE * len(record["segments"])
    qa = engines.qa_media(partial, aspect=plan["aspect"], planned_seconds=planned, tolerance=tolerance)
    sha = _sha256_file(partial)
    final_path = revision_dir / f"final-{sha[:16]}.mp4"
    partial.replace(final_path)
    record["final"] = {
        "status": "qa_passed" if qa["passed"] else "qa_failed",
        "id": str(uuid.UUID(sha[:32])),
        "file": str(final_path.relative_to(plans._folder(plan["id"]))),
        "sha256": sha,
        "size": final_path.stat().st_size,
        "name": f"{plan['name']}.mp4",
        "qa": qa,
        "engine": record["engine"],
        "inputs": {
            "segments": [
                {
                    "id": s["id"],
                    "project_id": s["project_id"],
                    "job_id": s["job_id"],
                    "run_key": s["run_key"],
                    "output_sha256": s["output"]["sha256"],
                }
                for s in record["segments"]
            ],
            "asset_sha256": sorted(
                {data["assets"]["scenes"][i]["sha256"] for s in record["segments"] for i in s["scene_ids"]}
                | {data["assets"]["music"]["sha256"]}
            ),
        },
        "planned_seconds": planned,
        # 사용자가 원한 길이와 승인된 계획의 차이를 결과와 함께 남긴다(편차는 계획 단계에서 설명·승인됨).
        "duration": {
            "requested": plan["duration"]["request"],
            "plan_status": plan["duration"]["status"],
            "planned_seconds": planned,
            "measured_seconds": qa["probe"]["duration"],
        },
        "assembled_at": _now(),
    }
    _log(record, "assembled", sha256=sha, qa_passed=qa["passed"])


def _reconcile(
    plans: PlanStore,
    studio: Studio,
    plan: dict[str, Any],
    record: dict[str, Any],
    data: dict[str, Any],
) -> None:
    for segment in record["segments"]:
        _sync_segment(studio, record, segment)
    done = all(s["status"] == "succeeded" for s in record["segments"])
    keys = [s["run_key"] for s in record["segments"]]
    final = record.get("final")
    if done and (final is None or final.get("keys") != keys):
        _assemble(plans, plan, record, data)
        if record.get("final"):
            record["final"]["keys"] = keys


# --- 공개 API -----------------------------------------------------------------


def _new_record(
    plan: dict[str, Any], adapter: engines.EngineAdapter, pre: engines.Preflight, caption_mode: str
) -> dict[str, Any]:
    style = _style_defaults(plan, adapter)
    record = {
        "revision": plan["revision"],
        "plan_sha256": plan["approval"]["snapshot"]["plan_sha256"],
        "engine": {"adapter": adapter.id, "version": pre.version, "preflight_at": _now()},
        "style": style,
        "caption_mode": caption_mode,
        "caption_timing": (
            "scene duration; title-only captions"
            if caption_mode == "title_only"
            else "estimated from synthesized voice asset duration; no word-level alignment"
        ),
        "caption_sources": {
            scene["scene_id"]: (
                "title_only"
                if caption_mode == "title_only"
                else "narration" if str(scene.get("narration") or "").strip() else "title_fallback_no_narration"
            )
            for scene in plan["scenes"]
        },
        "dispatched_at": _now(),
        "segments": [
            {
                "id": seg["id"],
                "scene_ids": seg["scene_ids"],
                "start_frame": seg["start_frame"],
                "end_frame": seg["end_frame"],
                "seconds": seg["seconds"],
                "plan_key": seg["input_sha256"],
                "input_sha256": seg["input_sha256"],
                "run_key": None,
                "project_id": None,
                "job_id": None,
                "status": "pending",
                "attempts": 0,
                "error": None,
                "output": None,
            }
            for seg in plan["render_plan"]["segments"]
        ],
        "final": None,
        "log": [],
    }
    _log(record, "dispatched", engine=adapter.id, version=pre.version, segments=len(record["segments"]))
    return record


def dispatch(
    plans: PlanStore,
    studio: Studio,
    plan_id: str,
    revision: int,
    caption_mode: Literal["narration", "title_only"] = "narration",
) -> dict[str, Any]:
    """승인된 계획 revision의 실행 기록을 만든다. 게이트·엔진·의존성이 맞을 때만, revision당 한 번."""
    plan = plans.assert_renderable(plan_id, revision)
    if caption_mode not in {"narration", "title_only"}:
        raise ProductionError("caption_mode는 narration 또는 title_only여야 합니다.")
    blockers = dispatch_blockers(plan)
    if blockers:
        raise ProductionError("디스패치할 수 없습니다: " + " / ".join(blockers))
    adapter = engines.adapter_for(_providers(plan))
    assert adapter is not None
    store = ProductionStore(plans)
    with store.tx(plan_id) as data:
        if _record(data, revision) is None:
            data["dispatches"].append(_new_record(plan, adapter, adapter.preflight(), caption_mode))
        return _compose(plans, studio, plan, data, store)


def request_render(plans: PlanStore, studio: Studio, plan_id: str, revision: int) -> dict[str, Any]:
    """게이트와 의존성을 다시 확인하고, 실행 키가 달라졌거나 실패한 구간만 엔진에 보낸다."""
    plan = plans.assert_renderable(plan_id, revision)
    blockers = dispatch_blockers(plan)
    if blockers:
        raise ProductionError("렌더할 수 없습니다: " + " / ".join(blockers))
    store = ProductionStore(plans)
    with store.tx(plan_id) as data:
        record = _record(data, revision)
        if record is None:
            raise ProductionError("먼저 이 계획 revision을 디스패치해 주세요.")
        state = _assets_state(plan, data, store)
        if not state["complete"]:
            problems = [f"장면 {i}에 이미지가 없습니다." for i in state["missing_scene_ids"]]
            if state["music_problem"]:
                problems.append(state["music_problem"])
            raise ProductionError("렌더할 수 없습니다: " + " / ".join(problems))
        adapter = engines.adapter_for(_providers(plan))
        assert adapter is not None
        pre = adapter.preflight()
        record["engine"] = {"adapter": adapter.id, "version": pre.version, "preflight_at": _now()}
        style = record["style"]
        for segment in record["segments"]:
            _sync_segment(studio, record, segment)
            caption_mode = record.get("caption_mode", "narration")
            key = _run_key(plan, segment, data, record["engine"], style, caption_mode)
            reusable = segment["run_key"] == key and segment["status"] in ("succeeded", *SEGMENT_ACTIVE)
            if reusable:
                continue
            retry_same = (
                segment["run_key"] == key
                and segment["status"] == "failed"
                and segment.get("project_id")
            )
            if retry_same:
                job = studio.enqueue(segment["project_id"], retry=True)
            else:
                segment["project_id"] = _create_project(
                    studio, store, plan, segment, data, style, caption_mode
                )
                job = studio.enqueue(segment["project_id"])
            segment.update(
                run_key=key, job_id=job["id"], status="queued", error=None, output=None,
                attempts=segment["attempts"] + 1,
            )
            record["final"] = None
            _log(
                record, "segment_queued", segment=segment["id"], project_id=segment["project_id"],
                job_id=job["id"], run_key=key, retry=bool(retry_same),
            )
        return _compose(plans, studio, plan, data, store)


def view(plans: PlanStore, studio: Studio, plan_id: str) -> dict[str, Any]:
    plan = plans.get(plan_id)
    store = ProductionStore(plans)
    with store.tx(plan_id) as data:
        return _compose(plans, studio, plan, data, store)


def _compose(
    plans: PlanStore,
    studio: Studio,
    plan: dict[str, Any],
    data: dict[str, Any],
    store: ProductionStore,
) -> dict[str, Any]:
    record = _record(data, plan["revision"])
    assets = _assets_state(plan, data, store)
    base = {
        "plan_id": plan["id"],
        "plan_revision": plan["revision"],
        "caption_mode": record.get("caption_mode") if record else None,
        "caption_timing": record.get("caption_timing") if record else None,
        "caption_sources": record.get("caption_sources") if record else None,
        "assets": assets,
        "asset_generation_blockers": (
            []
            if plan.get("match", {}).get("primary_recipe") == "R21"
            else [
                f"레시피 {plan.get('match', {}).get('primary_recipe') or '미지정'}는 자동 자산 생성 어댑터가 지원하지 않습니다."
            ]
        ),
        "asset_generation": next(
            (
                item
                for item in reversed(data.get("asset_generations", []))
                if item.get("plan_revision") == plan["revision"]
            ),
            None,
        ),
        "history": [
            {"revision": d["revision"], "dispatched_at": d["dispatched_at"], "final": (d.get("final") or {}).get("status")}
            for d in data["dispatches"]
        ],
    }
    if record is None:
        reasons = dispatch_blockers(plan)
        return base | {
            "state": "stale_revision" if data["dispatches"] else "not_dispatched",
            "dispatchable": not reasons,
            "reasons": reasons,
            "engine": None,
            "outdated_segments": [],
            "segments": [],
            "final": None,
            "log": [],
        }
    _reconcile(plans, studio, plan, record, data)
    segments = record["segments"]
    final = record.get("final")
    statuses = {s["status"] for s in segments}
    reasons: list[str] = []
    # 자산이 바뀌어 지난 실행 키와 달라진 구간. 이 구간만 다시 실행된다.
    outdated = (
        [
            s["id"]
            for s in segments
            if s["run_key"] != _run_key(plan, s, data, record["engine"], record["style"])
        ]
        if assets["complete"]
        else []
    )
    if "failed" in statuses:
        state = "failed"
        reasons = [f"구간 {s['id']}: {s['error']}" for s in segments if s["status"] == "failed"]
    elif statuses & set(SEGMENT_ACTIVE):
        state = "rendering"
    elif outdated and statuses != {"pending"}:
        state = "ready_to_render"
        reasons = ["자산이 바뀌어 다시 렌더할 구간: " + ", ".join(outdated)]
    elif final and final["status"] == "qa_passed":
        state = "succeeded"
    elif final and final["status"] == "qa_failed":
        state = "qa_failed"
        reasons = [c["detail"] for c in final["qa"]["checks"] if not c["passed"]]
    elif final and final["status"] == "assemble_failed":
        state = "failed"
        reasons = [final["error"]]
    elif not assets["complete"]:
        state = "awaiting_assets"
        reasons = [f"장면 {i}에 이미지가 없습니다." for i in assets["missing_scene_ids"]]
        if assets["music_problem"]:
            reasons.append(assets["music_problem"])
    else:
        state = "ready_to_render"
    return base | {
        "state": state,
        "dispatchable": False,
        "reasons": reasons,
        "engine": record["engine"],
        "style": record["style"],
        "outdated_segments": outdated,
        "segments": [
            {
                "id": s["id"], "scene_ids": s["scene_ids"], "seconds": s["seconds"],
                "status": s["status"], "attempts": s["attempts"], "error": s["error"],
                "project_id": s["project_id"], "job_id": s["job_id"], "run_key": s["run_key"],
                "metrics": s.get("metrics"),
            }
            for s in segments
        ],
        "final": None
        if final is None
        else {k: final.get(k) for k in ("status", "id", "sha256", "size", "name", "qa", "planned_seconds", "duration", "assembled_at", "error")},
        "log": record["log"][-60:],
    }


def final_media(plans: PlanStore, plan_id: str, media_id: str) -> tuple[Path, str]:
    """QA를 통과한 현재 revision의 최종 미디어 파일과 이름."""
    plan = plans.get(plan_id)
    data = ProductionStore(plans).read(plan_id)
    record = _record(data, plan["revision"])
    final = (record or {}).get("final") or {}
    if final.get("id") != media_id or final.get("status") != "qa_passed":
        raise FileNotFoundError("미디어가 없습니다.")
    path = plans._folder(plan_id) / final["file"]
    if not path.is_file():
        raise FileNotFoundError("미디어가 없습니다.")
    return path, final["name"]
