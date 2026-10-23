"""승인된 제작 계획 → 스튜디오 렌더러 디스패치와 제작 기록.

지금 제작 런타임(creative_runtime)에 연결된 엔진은 v1 FFmpeg 슬라이드쇼 렌더러 하나뿐이다.
이 모듈은 렌더 게이트(`PlanStore.assert_renderable`)를 통과한 계획만 그 렌더러의 프로젝트로 넘기고,
계획 revision과 렌더 작업을 `production.json`에 묶어 둔다. 이미지·음악은 생성하지 않는다.
사용자가 프로젝트에 올린 자산이 있어야 렌더가 시작된다. 다른 엔진은 이 표에 등록돼야 디스패치된다.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ttakkari.creative.service import ProjectInput, Scene, Studio
from ttakkari.creative.v2.plans import (
    RENDERER_PROJECT_MAX_SECONDS,
    RENDERER_SCENE_MAX_SECONDS,
    PlanStore,
)

ENGINE_ID = "skill.v1_ffmpeg_slideshow"
# 이 엔진이 만드는 계획의 필수 능력 제공자. 이 밖의 제공자가 필수에 끼면 디스패치하지 않는다.
ENGINE_PROVIDERS = {ENGINE_ID, "tool.ffmpeg"}
# v1 포맷 규격: 16:9와 1:1, 3~40장면, 3~240초(creative/catalog.py FORMATS['music_video']).
V1_FORMAT = "music_video"
V1_ASPECTS = ("16:9", "1:1")
V1_SCENES = (3, 40)
DURATION_TOLERANCE = 0.001


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _providers(plan: dict[str, Any]) -> set[str]:
    return {
        need["best"]["entry_id"]
        for need in plan["match"]["needs"]
        if need["priority"] == "must" and need.get("best")
    }


def dispatch_blockers(plan: dict[str, Any]) -> list[str]:
    """이 계획을 v1 렌더러로 보낼 수 없는 이유. 비어 있으면 보낼 수 있다."""
    reasons: list[str] = []
    if not plan["render_gate"]:
        reasons.append(
            "렌더 게이트가 닫혀 있습니다. 승인, studio_runtime 범위, 필수 능력 충족이 모두 필요합니다."
        )
    extra = _providers(plan) - ENGINE_PROVIDERS
    if extra:
        reasons.append("이 렌더러가 다루지 못하는 필수 제공자가 있습니다: " + ", ".join(sorted(extra)))
    if plan["aspect"] not in V1_ASPECTS:
        reasons.append(f"슬라이드쇼 렌더러는 화면비 {', '.join(V1_ASPECTS)}만 지원합니다(요청 {plan['aspect']}).")
    scenes = plan["scenes"]
    if not V1_SCENES[0] <= len(scenes) <= V1_SCENES[1]:
        reasons.append(f"슬라이드쇼 렌더러는 {V1_SCENES[0]}~{V1_SCENES[1]}장면만 지원합니다(현재 {len(scenes)}장면).")
    long_scenes = [s["scene_id"] for s in scenes if s["seconds"] > RENDERER_SCENE_MAX_SECONDS]
    if long_scenes:
        reasons.append(f"장면 {', '.join(long_scenes)}이(가) 렌더러 장면 한도 {RENDERER_SCENE_MAX_SECONDS:g}초를 넘습니다.")
    total = sum(s["seconds"] for s in scenes)
    if total > RENDERER_PROJECT_MAX_SECONDS:
        segments = len(plan.get("render_plan", {}).get("segments", []))
        reasons.append(
            f"총 {total:g}초가 렌더러 한도 {RENDERER_PROJECT_MAX_SECONDS:g}초를 넘습니다. "
            f"계획은 {segments}개 구간으로 나뉘지만 구간 디스패치와 이어 붙이기는 아직 구현되지 않았습니다."
        )
    return reasons


class ProductionStore:
    """계획 폴더의 `production.json`. 계획 revision마다 디스패치 기록을 하나씩 남긴다."""

    def __init__(self, plans: PlanStore):
        self.plans = plans

    def _path(self, plan_id: str) -> Path:
        return self.plans._folder(plan_id) / "production.json"

    @contextmanager
    def _tx(self, plan_id: str):
        folder = self.plans._folder(plan_id)
        if not (folder / "plan.json").is_file():
            raise FileNotFoundError("제작 계획이 없습니다.")
        with (folder / "production.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                data = self.read(plan_id)
                yield data
                temp = folder / f"{uuid.uuid4()}.prod.tmp"
                with temp.open("w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                temp.replace(self._path(plan_id))
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self, plan_id: str) -> dict[str, Any]:
        path = self._path(plan_id)
        if path.is_file():
            return json.loads(path.read_text())
        return {"plan_id": str(uuid.UUID(plan_id)), "dispatches": []}


def _scenes_for_v1(plan: dict[str, Any]) -> list[Scene]:
    return [
        Scene(
            id=s["scene_id"],
            duration=s["seconds"],
            prompt=(s["narration"] or s["title"])[:3000],
            caption=s["title"][:160],
        )
        for s in plan["scenes"]
    ]


def _approval_sha(plan: dict[str, Any]) -> str:
    return plan["approval"]["snapshot"]["plan_sha256"]


def dispatch(
    plans: PlanStore, studio: Studio, plan_id: str, revision: int
) -> dict[str, Any]:
    """승인된 계획 revision을 v1 렌더러 프로젝트로 만든다. 같은 revision은 한 번만 만든다."""
    plan = plans.assert_renderable(plan_id, revision)
    blockers = dispatch_blockers(plan)
    if blockers:
        raise ValueError("디스패치할 수 없습니다: " + " / ".join(blockers))
    store = ProductionStore(plans)
    with store._tx(plan_id) as data:
        if any(d["revision"] == revision for d in data["dispatches"]):
            return view(plans, studio, plan_id)
        project = studio.create(
            ProjectInput(
                name=plan["name"][:120],
                brief=plan["brief"][:6000],
                content_type=V1_FORMAT,
                duration=min(max(sum(s["seconds"] for s in plan["scenes"]), 3), 240),
                aspect=plan["aspect"],
            )
        )
        studio.edit(project["id"], project["revision"], scenes=_scenes_for_v1(plan))
        data["dispatches"].append(
            {
                "revision": revision,
                "plan_sha256": _approval_sha(plan),
                "engine": ENGINE_ID,
                "v1_format": V1_FORMAT,
                "project_id": project["id"],
                "dispatched_at": _now(),
                "scenes": [
                    {"scene_id": s["scene_id"], "seconds": s["seconds"]}
                    for s in plan["scenes"]
                ],
                "render_requests": [],
            }
        )
    return view(plans, studio, plan_id)


def _find(data: dict[str, Any], revision: int) -> dict[str, Any] | None:
    return next((d for d in data["dispatches"] if d["revision"] == revision), None)


def _project_state(
    record: dict[str, Any], project: dict[str, Any]
) -> tuple[str, list[str], dict[str, Any] | None]:
    planned = [(s["scene_id"], s["seconds"]) for s in record["scenes"]]
    current = [(s["id"], s["duration"]) for s in project["scenes"]]
    if len(planned) != len(current) or any(
        a[0] != b[0] or abs(a[1] - b[1]) > DURATION_TOLERANCE
        for a, b in zip(planned, current, strict=True)
    ):
        return "drifted", ["프로젝트의 장면 구성이 승인된 계획과 달라졌습니다."], None
    jobs = {j["id"]: j for j in project["render_jobs"]}
    requested = [jobs[r["job_id"]] for r in record["render_requests"] if r["job_id"] in jobs]
    job = requested[-1] if requested else None
    if job and job["status"] in ("queued", "running", "succeeded"):
        return job["status"], [], job
    missing = [
        f"장면 {s['id']}에 이미지·영상 자산이 없습니다." for s in project["scenes"] if not s["asset_id"]
    ]
    if not project["music_asset_id"]:
        missing.append("음악 자산이 없습니다.")
    if missing:
        return "awaiting_assets", missing, job
    return ("failed" if job else "ready_to_render"), [], job


def view(plans: PlanStore, studio: Studio, plan_id: str) -> dict[str, Any]:
    """계획의 현재 revision 기준 제작 상태. 렌더러 프로젝트의 실제 상태를 읽어 만든다."""
    plan = plans.get(plan_id)
    data = ProductionStore(plans).read(plan_id)
    record = _find(data, plan["revision"])
    base = {
        "plan_id": plan["id"],
        "plan_revision": plan["revision"],
        "engine": ENGINE_ID,
        "join": "단일 프로젝트 렌더라 이어 붙이기 없음",
        "history": [
            {k: d[k] for k in ("revision", "project_id", "dispatched_at")}
            for d in data["dispatches"]
        ],
    }
    if record is None:
        reasons = dispatch_blockers(plan)
        stale = bool(data["dispatches"])
        return base | {
            "state": "stale_revision" if stale else "not_dispatched",
            "dispatchable": not reasons,
            "reasons": reasons,
            "project_id": None,
            "job": None,
        }
    try:
        project = studio.get(record["project_id"])
    except FileNotFoundError:
        return base | {
            "state": "project_missing",
            "dispatchable": False,
            "reasons": ["렌더러 프로젝트를 찾을 수 없습니다."],
            "project_id": record["project_id"],
            "job": None,
        }
    state, reasons, job = _project_state(record, project)
    return base | {
        "state": state,
        "dispatchable": False,
        "reasons": reasons,
        "project_id": record["project_id"],
        "project_url": f"/creative/{record['project_id']}",
        "dispatched_at": record["dispatched_at"],
        "job": None
        if job is None
        else {
            k: job.get(k)
            for k in (
                "id",
                "status",
                "progress",
                "elapsed_seconds",
                "peak_rss_bytes",
                "error",
                "started_at",
                "ended_at",
            )
        },
    }


def request_render(
    plans: PlanStore, studio: Studio, plan_id: str, revision: int
) -> dict[str, Any]:
    """렌더 직전에 게이트를 다시 확인하고, 프로젝트가 승인 계획과 같을 때만 렌더를 큐에 넣는다."""
    plans.assert_renderable(plan_id, revision)
    store = ProductionStore(plans)
    with store._tx(plan_id) as data:
        record = _find(data, revision)
        if record is None:
            raise ValueError("먼저 이 계획 revision을 디스패치해 주세요.")
        project = studio.get(record["project_id"])
        state, reasons, _ = _project_state(record, project)
        if state in ("drifted", "awaiting_assets"):
            raise ValueError("렌더할 수 없습니다: " + " / ".join(reasons))
        job = studio.enqueue(record["project_id"], retry=state == "failed")
        if not any(r["job_id"] == job["id"] for r in record["render_requests"]):
            record["render_requests"].append(
                {
                    "job_id": job["id"],
                    "requested_at": _now(),
                    "project_revision": project["revision"],
                }
            )
    return view(plans, studio, plan_id)
