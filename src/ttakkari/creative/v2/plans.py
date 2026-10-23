"""제작 계획: 의도 → 매칭 → 필요 길이 → 장면 → 사용자 확인 → 편집 후 재계산 → 승인.

렌더는 이 계획의 승인된 revision에만 묶인다. 장면이나 길이 요청이 바뀌면 revision이 오르고 승인은 무효가 된다.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ttakkari.creative.v2.catalog_data import ENTRIES
from ttakkari.creative.v2.duration import (
    Beat,
    ContentProfile,
    DurationPlan,
    DurationRequest,
    SceneEdit,
    plan_duration,
    recheck_duration,
)
from ttakkari.creative.v2.intent import IntentSpec, hint_intent, validate_intent
from ttakkari.creative.v2.matching import MatchResult, match_intent
from ttakkari.creative.v2.registry import Registry
from ttakkari.creative.v2.style_dna import Resolution, StyleError, StyleRef, StyleStore

SCAFFOLDS: dict[str, list[tuple[str, float]]] = {
    "C08.three_act": [
        ("오프닝", 0.8),
        ("설정", 1.0),
        ("대립", 1.5),
        ("전환점", 1.2),
        ("해결", 1.0),
        ("엔딩", 0.8),
    ],
    "C08.explainer": [
        ("훅", 0.8),
        ("문제 제시", 1.0),
        ("핵심 개념", 1.5),
        ("단계별 설명", 1.5),
        ("정리", 1.0),
    ],
    "C08.problem_solution": [
        ("훅", 0.8),
        ("문제 제시", 1.0),
        ("원인", 1.2),
        ("해결", 1.5),
        ("결과", 1.0),
    ],
    "C08.tutorial": [
        ("훅", 0.8),
        ("준비", 1.0),
        ("단계 1", 1.3),
        ("단계 2", 1.3),
        ("확인", 1.0),
    ],
    "C08.documentary_form": [
        ("도입", 1.0),
        ("배경", 1.2),
        ("핵심 근거", 1.5),
        ("전환", 1.0),
        ("결론", 1.0),
    ],
    "C08.music_video_form": [
        ("인트로", 1.0),
        ("절 1", 1.3),
        ("후렴", 1.5),
        ("절 2", 1.3),
        ("후렴 반복", 1.5),
        ("아웃트로", 1.0),
    ],
}
DEFAULT_SCAFFOLD = [("오프닝", 1.0), ("전개 1", 1.3), ("전개 2", 1.3), ("마무리", 1.0)]


# v1 렌더러의 실제 한도(creative/service.py Scene.duration<=60, 프로젝트<=240초)에서 가져온 값이다.
# 이를 넘는 영상은 분할 렌더 없이는 만들 수 없으므로 계획 단계에서 구간과 재개 키를 정해 둔다.
SEGMENT_TARGET_SECONDS = 60.0
RENDERER_SCENE_MAX_SECONDS = 60.0
RENDERER_PROJECT_MAX_SECONDS = 240.0
RENDERER_SEGMENT_MIN_SCENES = 3
RENDERER_SEGMENT_MAX_SCENES = 40
_POSITION_KEYS = {
    "start_time",
    "end_time",
    "start_frame",
    "end_frame",
    "dependencies",
}


def render_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """승인된 장면에서 계산하는 분할 렌더·체크포인트 계획. 저장하지 않고 조회 때마다 장면에서 다시 만든다.

    구간 키(input_sha256)는 장면 내용과 구간 안 길이로만 정하고 절대 위치는 넣지 않는다.
    앞 장면 길이가 바뀌어도 내용이 같은 뒤 구간은 같은 키를 가져 이어 하기에서 재사용할 수 있다.
    이 계획은 실행 증거가 아니다. 렌더러가 연결되기 전에는 구간 산출물이 없다.
    """
    fps = plan["profile"]["fps"]
    shared = {
        "aspect": plan["aspect"],
        "fps": fps,
        "style_dna": (plan.get("style") or {}).get("sha256"),
    }
    groups: list[list[dict[str, Any]]] = []
    acc = 0.0
    for scene in plan["scenes"]:
        length = (scene["end_frame"] - scene["start_frame"]) / fps
        if groups and (
            acc + length > SEGMENT_TARGET_SECONDS
            or len(groups[-1]) >= RENDERER_SEGMENT_MAX_SCENES
        ):
            groups.append([])
            acc = 0.0
        if not groups:
            groups.append([])
        groups[-1].append(scene)
        acc += length
    # 렌더러는 구간마다 최소 장면 수가 있다. 모자란 꼬리 구간은 앞 구간에 합친다.
    while (
        len(groups) > 1
        and len(groups[-1]) < RENDERER_SEGMENT_MIN_SCENES
        and len(groups[-2]) + len(groups[-1]) <= RENDERER_SEGMENT_MAX_SCENES
    ):
        groups[-2].extend(groups.pop())
    segments = []
    for index, group in enumerate(groups, 1):
        start, end = group[0]["start_frame"], group[-1]["end_frame"]
        content = [
            {
                **{k: v for k, v in sc.items() if k not in _POSITION_KEYS},
                "frames": sc["end_frame"] - sc["start_frame"],
            }
            for sc in group
        ]
        segments.append(
            {
                "id": f"seg-{index:02d}",
                "scene_ids": [sc["scene_id"] for sc in group],
                "start_frame": start,
                "end_frame": end,
                "seconds": round((end - start) / fps, 3),
                "input_sha256": _digest({**shared, "scenes": content}),
            }
        )
    total = plan["scenes"][-1]["end_frame"] / fps if plan["scenes"] else 0.0
    warnings = [
        f"장면 {sc['scene_id']}이(가) {sc['seconds']:g}초로 렌더러 장면 한도 {RENDERER_SCENE_MAX_SECONDS:g}초를 넘습니다. 장면을 나눠야 합니다."
        for sc in plan["scenes"]
        if sc["seconds"] > RENDERER_SCENE_MAX_SECONDS
    ]
    return {
        "segment_target_seconds": SEGMENT_TARGET_SECONDS,
        "requires_split": total > RENDERER_PROJECT_MAX_SECONDS,
        "segments": segments,
        "join": {
            "method": "concat_in_order",
            "inputs": [seg["id"] for seg in segments],
        },
        "resume": "구간의 input_sha256이 같고 산출물이 남아 있으면 그 구간은 다시 렌더하지 않는다.",
        "executed": False,
        "warnings": warnings,
    }


def now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _plan_revision_hash(plan: dict[str, Any]) -> str:
    """Hash the saved plan revision, excluding approval and history metadata."""
    metadata = {
        "approval",
        "history",
        "updated_at",
        "render_blockers",
        "render_gate",
        "render_gate_reasons",
        "render_plan",
    }
    return _digest({key: value for key, value in plan.items() if key not in metadata})


def _capability_snapshot(match: dict[str, Any]) -> dict[str, Any]:
    needs = [
        {
            "options": need["options"],
            "priority": need["priority"],
            "status": need["status"],
            "level": need["level"],
            "best": (
                {
                    "entry_id": need["best"]["entry_id"],
                    "capability": need["best"]["capability"],
                    "level": need["best"]["level"],
                    "integration": need["best"]["integration"],
                }
                if need.get("best")
                else None
            ),
        }
        for need in match["needs"]
    ]
    return {
        "execution_scope": match["execution_scope"],
        "status": match["status"],
        "fingerprint": match["fingerprint"],
        "needs": needs,
    }


def _render_authorized(
    plan: dict[str, Any], current_capabilities: dict[str, Any] | None = None
) -> bool:
    approval = plan.get("approval")
    if not approval or approval.get("revision") != plan["revision"]:
        return False
    snapshot = approval.get("snapshot")
    if not snapshot or snapshot.get("revision") != plan["revision"]:
        return False
    if snapshot.get("plan_sha256") != _plan_revision_hash(plan):
        return False
    saved_capabilities = _capability_snapshot(plan["match"])
    if snapshot.get("capabilities") != saved_capabilities:
        return False
    if (
        current_capabilities is not None
        and snapshot.get("capabilities") != current_capabilities
    ):
        return False

    match = current_capabilities or saved_capabilities
    if match["execution_scope"] != "studio_runtime" or match["status"] != "ready":
        return False
    return all(
        need["status"] == "executed"
        and need.get("best") is not None
        and need["best"]["integration"] == "creative_runtime"
        for need in match["needs"]
        if need["priority"] == "must"
    )


def _gate_reasons(
    plan: dict[str, Any], current_capabilities: dict[str, Any] | None
) -> list[str]:
    """렌더 게이트가 닫힌 이유. 서버 판정(_render_authorized)과 같은 조건을 사람이 읽는 문장으로 푼다."""
    reasons = list(plan["render_blockers"])
    approval = plan.get("approval")
    if not approval or approval.get("revision") != plan["revision"]:
        reasons.append("이 revision은 아직 승인되지 않았습니다.")
        return reasons
    snapshot = approval.get("snapshot") or {}
    if (
        snapshot.get("revision") != plan["revision"]
        or snapshot.get("plan_sha256") != _plan_revision_hash(plan)
    ):
        reasons.append("승인 이후 계획 내용이 바뀌었습니다. 다시 승인해 주세요.")
    saved = _capability_snapshot(plan["match"])
    if snapshot.get("capabilities") != saved or (
        current_capabilities is not None
        and snapshot.get("capabilities") != current_capabilities
    ):
        reasons.append("승인 이후 능력 판정이 바뀌었습니다(레지스트리·설치 상태). 다시 승인해 주세요.")
    match = current_capabilities or saved
    if match["execution_scope"] != "studio_runtime" or match["status"] != "ready":
        reasons.append(
            f"실행 범위가 {match['execution_scope']}({match['status']})라 스튜디오 렌더러로 실행할 수 없습니다. "
            "승인은 계획 확정까지만 의미합니다."
        )
    unwired = [
        "/".join(need["options"])
        for need in match["needs"]
        if need["priority"] == "must"
        and not (
            need["status"] == "executed"
            and need.get("best") is not None
            and need["best"]["integration"] == "creative_runtime"
        )
    ]
    if unwired:
        reasons.append("제작 런타임에 연결되지 않았거나 실행 기록이 없는 필수 능력: " + ", ".join(unwired))
    return reasons


def _beats(intent: IntentSpec) -> tuple[list[Beat], str, list[str]]:
    """정보 항목이 있으면 그것이 장면이 되고, 없으면 서사 구조 템플릿으로 추정한다. 길이를 채우려고 장면을 만들지 않는다."""
    warnings: list[str] = []
    if intent.info_items:
        return (
            [
                Beat(
                    id=i.id,
                    title=i.title,
                    narration=i.narration,
                    info_weight=i.weight,
                    essential=i.essential,
                )
                for i in intent.info_items
            ],
            "info_items",
            warnings,
        )
    structure = next(
        (s.entry_id for s in intent.selections if s.entry_id in SCAFFOLDS), None
    )
    scaffold = SCAFFOLDS.get(structure or "", DEFAULT_SCAFFOLD)
    warnings.append(
        "전달할 정보 항목이 없어 서사 구조 템플릿으로 장면을 추정했습니다. 정보 항목을 주면 길이 산정이 정확해집니다."
    )
    return (
        [
            Beat(id=f"s{i + 1}", title=title, info_weight=w)
            for i, (title, w) in enumerate(scaffold)
        ],
        "template_scaffold",
        warnings,
    )


def _scene_records(
    duration: DurationPlan,
    profile: ContentProfile,
    match: MatchResult,
    registry: Registry,
    style_hash: str | None,
) -> list[dict[str, Any]]:
    by_catalog: dict[str, list[str]] = {}
    for sel in match.selections:
        by_catalog.setdefault(ENTRIES[sel.entry_id].catalog, []).append(sel.entry_id)
    skills: list[str] = []
    tools: list[str] = []
    models: list[str] = []
    for need in match.needs:
        if need.priority == "prefer" or not need.best:
            continue
        provider = need.best.entry_id
        bucket = {"skill": skills, "tool": tools, "model": models}[
            provider.split(".")[0]
        ]
        if provider not in bucket:
            bucket.append(provider)
    checks = ["qa.machine_video"] + [ENTRIES[e].id for e in by_catalog.get("C12", [])]
    fps = duration.fps
    beats = {b.id: b for b in profile.beats}
    return [
        {
            "scene_id": s.beat_id,
            "title": beats[s.beat_id].title,
            "narration": beats[s.beat_id].narration,
            "start_time": round(s.start_frame / fps, 3),
            "end_time": round(s.end_frame / fps, 3),
            "seconds": s.seconds,
            "start_frame": s.start_frame,
            "end_frame": s.end_frame,
            "duration_state": s.state,
            "floor": s.floor,
            "ideal": s.ideal,
            "ceiling": s.ceiling,
            "speech_seconds": s.speech_seconds,
            "info_weight": beats[s.beat_id].info_weight,
            "essential": beats[s.beat_id].essential,
            "narrative_goal": beats[s.beat_id].title or s.beat_id,
            "style_dna": style_hash,
            "characters": by_catalog.get("C04", []),
            "environment": by_catalog.get("C05", []) + by_catalog.get("C02", []),
            "camera": by_catalog.get("C06", []),
            "motion": by_catalog.get("C07", []),
            "audio": by_catalog.get("C09", []),
            "required_skills": skills,
            "selected_tools": tools,
            "selected_models": models,
            "dependencies": [] if i == 0 else [duration.scenes[i - 1].beat_id],
            "output_assets": [],
            "quality_checks": checks,
        }
        for i, s in enumerate(duration.scenes)
    ]


def _layout(profile: ContentProfile, planned: DurationPlan) -> DurationPlan:
    """strict 충돌로 계획안이 없어도 사용자가 고칠 수 있게 콘텐츠 기준(자동) 배치를 보여 준다."""
    return planned if planned.scenes else plan_duration(profile, DurationRequest())


def build_plan(
    brief: str,
    intent: IntentSpec | None,
    *,
    name: str = "",
    duration: DurationRequest | None = None,
    aspect: str | None = None,
    registry: Registry | None = None,
    styles: StyleStore | None = None,
    style_refs: list[StyleRef] | None = None,
    resolutions: list[Resolution] | None = None,
) -> dict[str, Any]:
    registry = registry or Registry()
    notes: list[str] = []
    if intent is None:
        intent, hint = hint_intent(brief)
        notes.append(
            "구조화된 의도가 없어 단어 힌트로 초안을 만들었습니다. 선택은 모두 soft이고 확인이 필요합니다."
        )
        if hint:
            notes.append(
                f"브리프에 길이 언급({hint['quote']})이 있지만 확인 전에는 적용하지 않았습니다."
            )
    elif intent.extraction.method == "llm":
        raise ValueError(
            "검토 전 의도 초안은 계획 계산에 사용할 수 없습니다. 항목을 직접 검토하고 확인해 주세요."
        )
    # UI에서 사용자가 고른 길이·화면비는 추출기의 해석보다 우선한다.
    update: dict[str, Any] = {}
    if duration is not None:
        update["duration"] = duration
    if aspect is not None:
        update["aspect"] = aspect
    if update:
        intent = intent.model_copy(update=update)
    validation = validate_intent(brief, intent)
    if not validation.valid:
        raise ValueError(
            "의도 검증 실패: "
            + "; ".join(f"{e.path} {e.message}" for e in validation.errors)
        )
    match = match_intent(brief, intent, registry)
    beats, basis, beat_warnings = _beats(intent)
    profile = ContentProfile(beats=beats, music_seconds=intent.music_seconds)
    duration = plan_duration(profile, intent.duration)
    style = None
    style_hash = None
    if style_refs:
        if styles is None:
            raise StyleError("스타일 저장소가 없습니다.")
        composed = styles.compose(style_refs, resolutions)
        style = composed.model_dump(mode="json")
        style_hash = composed.sha256
    elif match.style_resolution_needed:
        notes.append(
            "혼합 스타일 충돌이 있습니다. Style DNA를 고르고 충돌 해결을 남겨 주세요: "
            + ", ".join(match.style_resolution_needed)
        )
    plan_id = str(uuid.uuid4())
    scenes = _scene_records(
        _layout(profile, duration), profile, match, registry, style_hash
    )
    return {
        "id": plan_id,
        "revision": 1,
        "created_at": now(),
        "updated_at": now(),
        "name": name.strip() or brief.strip()[:60] or "새 제작 계획",
        "brief": brief,
        "brief_sha256": hashlib.sha256(brief.encode()).hexdigest(),
        "aspect": intent.aspect or "16:9",
        "intent": intent.model_dump(mode="json"),
        "intent_status": "unconfirmed_hint"
        if validation.needs_confirmation
        else "confirmed",
        "validation": validation.model_dump(mode="json"),
        "match": match.model_dump(mode="json"),
        "style": style,
        "profile": profile.model_dump(mode="json"),
        "basis": basis,
        "duration": duration.model_dump(mode="json"),
        "duration_check": None,
        "scenes": scenes,
        "notes": [*notes, *beat_warnings],
        "approval": None,
        "history": [
            {
                "revision": 1,
                "at": now(),
                "kind": "created",
                "total_seconds": duration.planned_seconds,
                "status": duration.status,
            }
        ],
    }


def _blockers(plan: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    duration = plan["duration"]
    check = plan.get("duration_check")
    if duration["status"] == "conflict" and check is None:
        reasons.append(
            "길이 요청이 콘텐츠와 충돌합니다. 대안을 고르거나 장면을 직접 맞춰 주세요."
        )
    if check and check["status"] == "violation":
        reasons.extend(check["errors"])
    if not plan["scenes"]:
        reasons.append("장면이 없습니다.")
    if plan["match"]["status"] == "needs_user_decision":
        reasons.append("선택 충돌 또는 레시피 미결정으로 사용자 결정이 필요합니다.")
    if plan["match"]["status"] == "blocked":
        reasons.append("필수 능력이 없어 지금은 제작할 수 없습니다.")
    if plan["intent_status"] == "unconfirmed_hint":
        reasons.append("의도가 확인되지 않았습니다(단어 힌트 초안).")
    if plan.get("style") and plan["style"]["status"] == "unresolved":
        reasons.append("Style DNA 충돌이 해결되지 않았습니다.")
    return reasons


class PlanStore:
    def __init__(self, root: Path, registry: Registry | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = registry or Registry()

    def _folder(self, plan_id: str) -> Path:
        try:
            canonical = str(uuid.UUID(plan_id))
        except ValueError as e:
            raise ValueError("계획 ID가 잘못됐습니다.") from e
        return self.root / canonical

    def get(self, plan_id: str) -> dict[str, Any]:
        path = self._folder(plan_id) / "plan.json"
        if not path.is_file():
            raise FileNotFoundError("제작 계획이 없습니다.")
        plan = json.loads(path.read_text())
        plan["render_blockers"] = _blockers(plan)
        try:
            current_match = match_intent(
                plan["brief"], IntentSpec.model_validate(plan["intent"]), self.registry
            )
            current_capabilities = _capability_snapshot(
                current_match.model_dump(mode="json")
            )
        except (KeyError, ValueError, TypeError):
            current_capabilities = None
        plan["render_gate"] = (
            current_capabilities is not None
            and _render_authorized(plan, current_capabilities)
            and not plan["render_blockers"]
        )
        plan["render_gate_reasons"] = (
            []
            if plan["render_gate"]
            else _gate_reasons(plan, current_capabilities)
            or ["능력 판정을 다시 계산하지 못했습니다."]
        )
        plan["render_plan"] = render_plan(plan)
        return plan

    def assert_renderable(self, plan_id: str, revision: int) -> dict[str, Any]:
        """Server-side authorization check for a renderer before it starts work."""
        plan = self.get(plan_id)
        if plan["revision"] != revision or not plan["render_gate"]:
            raise ValueError(
                "렌더할 수 없습니다: 승인 스냅샷 또는 studio_runtime 조건이 맞지 않습니다."
            )
        return plan

    def list(self) -> list[dict[str, Any]]:
        plans = [
            self.get(p.name)
            for p in self.root.iterdir()
            if p.is_dir() and (p / "plan.json").is_file()
        ]
        return sorted(plans, key=lambda p: p["updated_at"], reverse=True)

    def _save(self, plan: dict[str, Any]) -> None:
        plan = {
            k: v
            for k, v in plan.items()
            if k
            not in (
                "render_blockers",
                "render_gate",
                "render_gate_reasons",
                "render_plan",
            )
        }
        folder = self._folder(plan["id"])
        temp = folder / f"{uuid.uuid4()}.tmp"
        with temp.open("w") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(folder / "plan.json")

    def create(self, plan: dict[str, Any]) -> dict[str, Any]:
        folder = self._folder(plan["id"])
        folder.mkdir()
        self._save(plan)
        return self.get(plan["id"])

    @contextmanager
    def _tx(self, plan_id: str):
        folder = self._folder(plan_id)
        if not (folder / "plan.json").is_file():
            raise FileNotFoundError("제작 계획이 없습니다.")
        with (folder / "plan.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                plan = json.loads((folder / "plan.json").read_text())
                yield plan
                plan["updated_at"] = now()
                self._save(plan)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def edit(
        self,
        plan_id: str,
        revision: int,
        *,
        scenes: list[SceneEdit] | None = None,
        duration: DurationRequest | None = None,
        registry: Registry | None = None,
    ) -> dict[str, Any]:
        """장면 편집은 사용자가 정한 길이를 보존한 채 재계산(recheck), 길이 요청 변경은 장면 구성부터 다시 계획한다."""
        if scenes is None and duration is None:
            raise ValueError("바꿀 내용이 없습니다.")
        registry = registry or Registry()
        with self._tx(plan_id) as plan:
            if plan["revision"] != revision:
                raise ValueError(
                    "다른 변경이 먼저 저장됐습니다. 새로고침한 뒤 다시 편집해 주세요."
                )
            request = duration or DurationRequest.model_validate(
                plan["duration"]["request"]
            )
            profile = ContentProfile.model_validate(plan["profile"])
            match = MatchResult.model_validate(plan["match"])
            style_hash = (plan.get("style") or {}).get("sha256")
            if scenes is not None:
                if len({s.id for s in scenes}) != len(scenes):
                    raise ValueError("장면 ID가 중복됐습니다.")
                check = recheck_duration(
                    scenes,
                    request,
                    fps=profile.fps,
                    music_seconds=profile.music_seconds,
                    min_scene_seconds=profile.min_scene_seconds,
                )
                profile = ContentProfile(
                    beats=[
                        Beat(
                            id=s.id,
                            title=s.title,
                            narration=s.narration,
                            info_weight=s.info_weight,
                            essential=s.essential,
                            min_seconds=s.min_seconds,
                            max_seconds=s.max_seconds,
                            measured_speech_seconds=s.measured_speech_seconds,
                        )
                        for s in scenes
                    ],
                    fps=profile.fps,
                    music_seconds=profile.music_seconds,
                    min_scene_seconds=profile.min_scene_seconds,
                )
                planned = plan_duration(profile, request)
                # 사용자가 정한 길이를 장면에 그대로 둔다. 계획안은 비교용으로만 남긴다.
                plan["duration_check"] = check.model_dump(mode="json")
                plan["duration"] = planned.model_dump(mode="json")
                by_id = {s.id: s for s in scenes}
                cursor = 0
                records = _scene_records(
                    _layout(profile, planned), profile, match, registry, style_hash
                )
                for rec, check_scene in zip(records, check.scenes, strict=True):
                    edit_scene = by_id[rec["scene_id"]]
                    frames = check_scene.end_frame - check_scene.start_frame
                    rec.update(
                        seconds=edit_scene.seconds,
                        start_frame=cursor,
                        end_frame=cursor + frames,
                        start_time=round(cursor / profile.fps, 3),
                        end_time=round((cursor + frames) / profile.fps, 3),
                        duration_state=check_scene.state,
                        floor=check_scene.floor,
                        ideal=check_scene.ideal,
                        ceiling=check_scene.ceiling,
                        speech_seconds=check_scene.speech_seconds,
                    )
                    cursor += frames
                plan["scenes"] = records
                plan["basis"] = "user_edited"
                kind = "scenes_edited"
                total = check.total_seconds
                status = check.status
            else:
                planned = plan_duration(profile, request)
                plan["duration"] = planned.model_dump(mode="json")
                plan["duration_check"] = None
                plan["scenes"] = _scene_records(
                    _layout(profile, planned), profile, match, registry, style_hash
                )
                kind, total, status = (
                    "duration_replanned",
                    planned.planned_seconds,
                    planned.status,
                )
            plan["profile"] = profile.model_dump(mode="json")
            plan["intent"]["duration"] = request.model_dump(mode="json")
            plan["revision"] += 1
            plan["approval"] = None
            plan["history"].append(
                {
                    "revision": plan["revision"],
                    "at": now(),
                    "kind": kind,
                    "total_seconds": total,
                    "status": status,
                }
            )
        return self.get(plan_id)

    def approve(self, plan_id: str, revision: int) -> dict[str, Any]:
        with self._tx(plan_id) as plan:
            if plan["revision"] != revision:
                raise ValueError(
                    "다른 변경이 먼저 저장됐습니다. 새로고침한 뒤 다시 승인해 주세요."
                )
            blockers = _blockers(plan)
            if blockers:
                raise ValueError("승인할 수 없습니다: " + " / ".join(blockers))
            frames = plan["scenes"][-1]["end_frame"]
            plan["approval"] = {
                "revision": plan["revision"],
                "planned_frames": frames,
                "planned_seconds": round(frames / plan["profile"]["fps"], 3),
                "fps": plan["profile"]["fps"],
                "approved_at": now(),
                "by": "user",
                "scenes_sha256": _digest(
                    [
                        (
                            s["scene_id"],
                            s["start_frame"],
                            s["end_frame"],
                            s["narration"],
                        )
                        for s in plan["scenes"]
                    ]
                ),
                "snapshot": {
                    "revision": plan["revision"],
                    "plan_sha256": _plan_revision_hash(plan),
                    "capabilities": _capability_snapshot(plan["match"]),
                },
            }
            plan["history"].append(
                {
                    "revision": plan["revision"],
                    "at": now(),
                    "kind": "approved",
                    "total_seconds": plan["approval"]["planned_seconds"],
                    "status": "approved",
                }
            )
        return self.get(plan_id)


def default_plan_store() -> PlanStore:
    from ttakkari.config import get_settings

    return PlanStore(get_settings().home / "creative-studio" / "plans")
