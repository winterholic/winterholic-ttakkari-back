"""'제작 기록' 화면용 읽기 모델. 저장된 G0 상태와 sidecar에서 샷별 이력을 조립할 뿐 새 사실을 만들지 않는다."""

from __future__ import annotations

import json
from typing import Any

from ttakkari.creative.pipeline import ProductionPipeline
from ttakkari.creative.review import find_attempt_review


def _output(
    attempt: dict[str, Any], state: dict[str, Any], receipt_media_type: str | None
) -> dict[str, Any] | None:
    if not attempt.get("outputs"):
        return None
    first = attempt["outputs"][0]
    return {
        "sha256": first["sha256"],
        "bytes": first["bytes"],
        # G0 CAS 목록의 첫 출력은 octet-stream이다. engine receipt가 말하는 실제 종류를 우선한다.
        "media_type": receipt_media_type or _media_type(first, state),
    }


def _media_type(first: dict[str, Any], state: dict[str, Any]) -> str | None:
    known = {a["sha256"]: a for a in state["artifacts"].values()}
    return (known.get(first["sha256"]) or {}).get("media_type")


def production_record(pipeline: ProductionPipeline, pid: str) -> dict[str, Any]:
    store = pipeline.store
    state = store.get_production(pid)
    reports = state.get("review_reports", {})
    feedback = state.get("feedback", {})
    timeline = state["timelines"][-1]
    bible = state["bibles"][-1]
    shots_view = []
    for shot_id, rows in state["shots"].items():
        shot = rows[-1]
        params_doc = pipeline.params_doc(pid, shot)
        attempts = [a for a in state["attempts"] if a["shot_id"] == shot_id]
        current = pipeline.current_attempt(state, shot_id)
        review = None
        keyframes: list[dict[str, Any]] = []
        if current and current["outputs"]:
            match = find_attempt_review(state, current["attempt_id"])
            if match:
                latest, report = match["review"], match["report"]
                review = {
                    "review_id": latest["review_id"],
                    "verdict": report.get("verdict"),
                    "criteria": latest["criteria"],
                    "reviewer": latest["reviewer"],
                    "independent": latest["independent"],
                    "approval_event_id": latest["approval_event_id"],
                    "scope_note": report.get("scope_note"),
                }
                manifest_sha = report.get("keyframe_manifest_sha256")
                if manifest_sha:
                    manifest = json.loads(store.read_artifact(pid, manifest_sha))
                    keyframes = manifest["keyframes"]
                    review["dropped_candidates"] = len(manifest["dropped_candidates"])
        receipt = None
        output_media_type = None
        if current and current["state"] == "review_pending":
            doc = pipeline.attempt_receipt(
                current, lambda s: store.read_artifact(pid, s)
            )
            if doc:
                receipt = {
                    "engine": doc["engine"],
                    "profile": doc["profile"],
                    "execution_mode": doc["execution_mode"],
                    "real_execution": doc["real_execution"],
                    "selection": doc["selection"],
                    "wall_seconds": doc["wall_seconds"],
                    "metrics": {
                        k: v for k, v in doc["metrics"].items() if k != "probe"
                    },
                    "media": (doc["outputs"][0] or {}).get("media"),
                }
                output_media_type = doc["outputs"][0].get("media_type")
        shots_view.append(
            {
                "shot_id": shot_id,
                "start_f": shot["start_f"],
                "end_f": shot["end_f"],
                "revision": shot["revision"],
                "intent": shot["intent"],
                "engine": shot["engine"],
                "profile": shot["profile"],
                "params": params_doc.get("params", {}),
                "selection": params_doc.get("selection"),
                "selection_reason": params_doc.get("reason"),
                "seed": shot["seed"],
                "acceptance_criteria": shot["acceptance_criteria"],
                "stale": shot_id in state["stale"]["shots"],
                "transition_neighbor_stale": shot_id
                in state["stale"]["transition_neighbors"],
                "current_attempt": None
                if current is None
                else {
                    "attempt_id": current["attempt_id"],
                    "state": current["state"],
                    "error_kind": current.get("error_kind"),
                    "error_details": current.get("error_details", []),
                    "output": _output(current, state, output_media_type),
                    "receipt": receipt,
                },
                "review": review,
                "keyframes": keyframes,
                "revisions": [
                    {
                        "revision": r["revision"],
                        "content_hash": r["content_hash"],
                        "engine": r["engine"],
                        "profile": r["profile"],
                        "created_at": r["created_at"],
                    }
                    for r in rows
                ],
                "attempts": [
                    {
                        "attempt_id": a["attempt_id"],
                        "shot_revision": a["shot_revision"],
                        "state": a["state"],
                        "error_kind": a.get("error_kind"),
                        "created_at": a["created_at"],
                    }
                    for a in attempts
                ],
                "feedback": [
                    {
                        k: f[k]
                        for k in (
                            "feedback_id",
                            "from_revision",
                            "text",
                            "decided_by",
                            "reason",
                            "changes",
                            "at",
                        )
                    }
                    for f in feedback.values()
                    if f["shot_id"] == shot_id
                ],
            }
        )
    compositions = state.get("compositions", {})
    final = None
    if compositions:
        key, comp = max(compositions.items(), key=lambda kv: kv[1]["at"])
        reviews = [
            r
            for r in state["reviews"]
            if r["output_sha256"] == comp["output_sha256"]
            and r["shot_revision"] is None
        ]
        report = reports.get(reviews[-1]["review_id"]) if reviews else None
        current_outputs = pipeline.current_outputs(state)
        final = {
            "composition_key": key,
            "output_sha256": comp["output_sha256"],
            "execution_mode": comp["execution_mode"],
            "real_execution": comp["real_execution"],
            "matches_current_shots": comp.get("shot_outputs") == current_outputs,
            "review": None
            if report is None
            else {
                "verdict": report["verdict"],
                "criteria": report["criteria"],
                "scope_note": report["scope_note"],
                "approval": None,
            },
        }
    return {
        "production_id": pid,
        "brief": state["manifest"]["brief_ref"],
        "manifest": {
            "revision": state["manifest"]["manifest_revision"],
            "content_hash": state["manifest"]["content_hash"],
            "policy_ref": state["manifest"]["policy_ref"],
        },
        "cancelled": state["cancelled"],
        "fps": {"num": timeline["fps_num"], "den": timeline["fps_den"]},
        "duration_frames": timeline["duration_frames"],
        "format": bible["composition"].get("size"),
        "bible": {
            k: bible[k]
            for k in (
                "revision",
                "palette",
                "typography",
                "composition",
                "camera",
                "negative_rules",
                "author",
                "approval_event_id",
            )
        },
        "timeline": {
            "revision": timeline["revision"],
            "sections": timeline["sections"],
            "beats": len(timeline["beats"]),
            "cues": len(timeline["cues"]),
        },
        "shots": shots_view,
        "final": final,
        "stale": state["stale"],
        "direction": [
            {
                "step": d["step"],
                "status": d.get("status"),
                "repairs": d.get("repairs"),
                "at": d["at"],
            }
            for d in state.get("direction", {}).values()
            if "step" in d
        ],
        "events_tail": state["events"][-20:],
        "engines": [
            {
                "engine_id": e["engine_id"],
                "execution_mode": e["execution_mode"],
                "auto_eligible": e["auto_eligible"],
                "installation": e["installation"].get("state"),
                "verification": e["verification"]["level"],
            }
            for e in pipeline.registry.snapshot()
        ],
    }
