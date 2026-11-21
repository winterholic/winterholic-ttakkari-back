"""Evidence-bound quality evaluation and non-mutating partial repair contracts.

Machine measurements never stand in for semantic or viewing judgements. External
review receipts are assertions by the named reviewer, not user approval. This
module validates their scope and evidence; it does not invent a model execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from ttakkari.creative.v2 import engines


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# These are acceptance policies, not measured quality scores.
_CRITERIA = {
    "intent_alignment": (
        "semantic_qa",
        ["frame"],
        "장면의 서사 목표와 시각 내용이 일치한다",
    ),
    "text_legibility": (
        "semantic_qa",
        ["frame"],
        "문자·자막이 정확하고 안전영역에서 읽힌다; 문자 없음도 관측해 기록한다",
    ),
    "style_continuity": (
        "semantic_qa",
        ["frame"],
        "장면 사이 Style DNA·공간·색·재질이 일관된다",
    ),
    "character_consistency": (
        "semantic_qa",
        ["frame"],
        "캐릭터 외형·비율·의상·정체성이 설정과 일치한다",
    ),
    "factual_accuracy": (
        "semantic_qa",
        ["frame", "reference"],
        "제품·과학·역사·수치가 명시된 출처와 일치한다",
    ),
    "period_accuracy": (
        "semantic_qa",
        ["frame", "reference"],
        "의상·건축·소품이 시대 근거와 일치한다",
    ),
    "motion": (
        "viewing_qa",
        ["video"],
        "동작·카메라 움직임·연기가 자연스럽고 의도에 맞는다",
    ),
    "transition": (
        "viewing_qa",
        ["video"],
        "경계 전후에서 겹침·점프·의도치 않은 끊김이 없다",
    ),
    "audio_sync": (
        "viewing_qa",
        ["video", "audio"],
        "발화·자막·효과음·음악과 화면의 타이밍이 맞는다",
    ),
    "audio_quality": (
        "viewing_qa",
        ["audio"],
        "발음·음악·효과음의 청감과 균형이 좋다; 무음 의도도 관측한다",
    ),
    "pacing": (
        "viewing_qa",
        ["video", "audio"],
        "정보를 이해할 시간·서사 리듬·전체 길이가 적절하다",
    ),
    "viewing_value": (
        "viewing_qa",
        ["video", "audio"],
        "완성 영상을 실제 재생해 목적 달성과 시청 가치를 판단한다",
    ),
    "beat_sync": (
        "viewing_qa",
        ["video", "audio"],
        "비트·가사·음악 구간에 컷과 동작이 맞는다",
    ),
    "hook": ("viewing_qa", ["video"], "초반 훅과 마무리가 장르·목적에 맞는다"),
    "source_fidelity": (
        "semantic_qa",
        ["frame", "reference"],
        "실제 촬영·게임 상태·제품 원본과 일치한다",
    ),
}
_GENRES = {
    "animation": ["character_consistency"],
    "story_film": ["character_consistency"],
    "motion_comic": ["character_consistency"],
    "music_video": ["beat_sync"],
    "tech_explainer": ["factual_accuracy"],
    "science_edu": ["factual_accuracy"],
    "documentary": ["factual_accuracy", "period_accuracy"],
    "history_reenactment": ["period_accuracy"],
    "product_intro": ["factual_accuracy", "source_fidelity"],
    "shorts": ["hook"],
    "advertisement": ["hook"],
    "game_trailer": ["hook"],
    "gameplay": ["source_fidelity"],
    "vlog": ["source_fidelity"],
    "presentation": [],
}
_C12 = {
    "subtitle_legibility": "text_legibility",
    "motion_naturalness": "motion",
    "temporal_continuity": "transition",
    "spatial_continuity": "style_continuity",
    "viewing_experience": "viewing_value",
}


def build_rubric(plan: dict[str, Any]) -> dict[str, Any]:
    """Call before render and persist as record.rubric; selected genres are explicit."""
    selections = [
        s["entry_id"] for s in (plan.get("intent") or {}).get("selections", [])
    ]
    genres = sorted({s.split(".", 1)[1] for s in selections if s.startswith("C01.")})
    ids = [
        "intent_alignment",
        "text_legibility",
        "style_continuity",
        "motion",
        "transition",
        "audio_sync",
        "audio_quality",
        "pacing",
        "viewing_value",
    ]
    for genre in genres:
        ids.extend(_GENRES.get(genre, []))
    for selection in selections:
        if selection.startswith("C12."):
            name = selection.split(".", 1)[1]
            candidate = _C12.get(name, name)
            if candidate in _CRITERIA:
                ids.append(candidate)
    rubric = {
        "schema_version": 1,
        "policy": "evidence-bound-quality-v1",
        "phase": "pre_render",
        "plan_id": plan.get("id"),
        "revision": plan.get("revision"),
        "plan_sha256": content_hash(plan),
        "genres": genres or ["generic"],
        "max_pass_uncertainty": 0.25,
        "criteria": [
            {
                "id": cid,
                "stage": _CRITERIA[cid][0],
                "modalities": list(_CRITERIA[cid][1]),
                "requirement": _CRITERIA[cid][2],
                "required": True,
            }
            for cid in dict.fromkeys(ids)
        ],
        "targets": [
            {
                "scene_id": sc["scene_id"],
                "start_time": sc.get("start_time"),
                "end_time": sc.get("end_time"),
                "narrative_goal": sc.get("narrative_goal"),
                "shots": deepcopy(sc.get("shots", [])),
            }
            for sc in plan.get("scenes", [])
        ],
    }
    return {**rubric, "sha256": content_hash(rubric)}


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "verdict": "unknown",
        "uncertainty": 1.0,
        "reason": reason,
        "checks": [],
        "evaluators": [],
    }


def _verdict(checks: list[dict[str, Any]]) -> str:
    values = [check["verdict"] for check in checks]
    if "fail" in values:
        return "fail"
    return "pass" if values and all(v == "pass" for v in values) else "unknown"


def _evidence_valid(item: dict[str, Any], output_sha: str) -> bool:
    if not isinstance(item, dict):
        return False
    kind = item.get("kind")
    try:
        path = Path(item["path"])
        if not path.is_file() or file_hash(path) != item.get("sha256"):
            return False
        if kind == "reference":
            return True
        if item.get("source_sha256") != output_sha:
            return False
        if kind in {"video", "audio", "frame"}:
            start, end = item.get("start_time"), item.get("end_time")
            return (
                isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and math.isfinite(start)
                and math.isfinite(end)
                and 0 <= start <= end
                and (kind == "frame" or start < end)
                and (kind != "video" or item["sha256"] == output_sha)
            )
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return False


def _covers(item: dict[str, Any], target: dict[str, Any]) -> bool:
    if item["kind"] == "reference":
        return True
    start, end = target.get("start_time"), target.get("end_time")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return False
    if item["kind"] == "frame":
        return start <= item["start_time"] < end
    return item["start_time"] <= start and item["end_time"] >= end


def _review_stage(
    stage: str, rubric: dict[str, Any], receipts: list[dict[str, Any]], output_sha: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    accepted, rejected = [], []
    if not isinstance(receipts, list):
        result = _unknown("quality_evidence must be a receipt array")
        result["rejected_receipts"] = ["invalid receipt collection"]
        return result, []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            rejected.append("receipt must be an object")
            continue
        if receipt.get("stage") != stage:
            continue
        evaluator = receipt.get("evaluator") or {}
        if (
            not isinstance(evaluator, dict)
            or not isinstance(receipt.get("criteria"), list)
            or not isinstance(receipt.get("defects", []), list)
        ):
            rejected.append("invalid evaluator/criteria/defects structure")
            continue
        if (
            receipt.get("output_sha256") != output_sha
            or receipt.get("rubric_sha256") != rubric["sha256"]
            or not all(
                isinstance(evaluator.get(k), str) and evaluator[k].strip()
                for k in ("id", "version", "source")
            )
            or evaluator.get("independent") is not True
        ):
            rejected.append(
                "stale/unbound receipt or missing independent evaluator provenance"
            )
        else:
            accepted.append(receipt)
    checks, defects = [], []
    for criterion in (c for c in rubric["criteria"] if c["stage"] == stage):
        for target in rubric["targets"]:
            scene_id = target["scene_id"]
            observations = []
            for receipt in accepted:
                for raw in receipt.get("criteria", []):
                    if (
                        not isinstance(raw, dict)
                        or not isinstance(raw.get("scene_ids"), list)
                        or not isinstance(raw.get("evidence", []), list)
                    ):
                        continue
                    if raw.get("criterion_id") != criterion[
                        "id"
                    ] or scene_id not in raw.get("scene_ids", []):
                        continue
                    uncertainty = raw.get("uncertainty")
                    if (
                        type(uncertainty) not in (int, float)
                        or not math.isfinite(uncertainty)
                        or not 0 <= uncertainty <= 1
                    ):
                        continue
                    evidence = [
                        deepcopy(e)
                        for e in raw.get("evidence", [])
                        if _evidence_valid(e, output_sha) and _covers(e, target)
                    ]
                    kinds = {e["kind"] for e in evidence}
                    # Video can support a visual judgement; stills cannot support temporal/audio checks.
                    if "video" in kinds:
                        kinds.add("frame")
                    complete = set(criterion["modalities"]) <= kinds
                    verdict = raw.get("verdict", "unknown")
                    if verdict not in {"pass", "fail", "unknown"}:
                        verdict = "unknown"
                    if not complete or uncertainty > rubric["max_pass_uncertainty"]:
                        verdict = "unknown"
                    observations.append(
                        {
                            "verdict": verdict,
                            "uncertainty": uncertainty if complete else 1.0,
                            "evidence": evidence,
                            "evaluator": deepcopy(receipt["evaluator"]),
                        }
                    )
            value = _verdict(observations)
            checks.append(
                {
                    "criterion_id": criterion["id"],
                    "scene_id": scene_id,
                    "verdict": value,
                    "uncertainty": max(
                        [o["uncertainty"] for o in observations], default=1.0
                    ),
                    "observations": observations,
                }
            )
    for receipt in accepted:
        for defect in receipt.get("defects", []):
            if not isinstance(defect, dict) or not isinstance(
                defect.get("evidence", []), list
            ):
                continue
            evidence = [
                e for e in defect.get("evidence", []) if _evidence_valid(e, output_sha)
            ]
            if evidence:
                defects.append(
                    {
                        **deepcopy(defect),
                        "evidence": evidence,
                        "evaluator": deepcopy(receipt["evaluator"]),
                        "stage": stage,
                    }
                )
    result = {
        "verdict": _verdict(checks),
        "uncertainty": max([c["uncertainty"] for c in checks], default=1.0),
        "checks": checks,
        "evaluators": [deepcopy(r["evaluator"]) for r in accepted],
        "rejected_receipts": rejected,
        "reason": "review receipts evaluated"
        if accepted
        else "independent review not executed for current output/rubric",
    }
    # Explicit, evidenced defects cannot be contradicted by all-pass criterion rows.
    if defects:
        result["verdict"] = "fail"
    return result, defects


def evaluate_production(
    plan: dict[str, Any], record: dict[str, Any], *, media_path: Path | None = None
) -> dict[str, Any]:
    """Read current media, accept hash-bound review receipts, return JSON-serializable report.

    Does not save, dispatch, run a paid evaluator or modify inputs. Missing evidence
    remains unknown; callers must persist the report separately from machine QA.
    """
    rubric = build_rubric(plan)
    final = record.get("final") or {}
    value = media_path or final.get("file")
    path = Path(value) if value else None
    machine = _unknown("media not available; cached QA is not reused")
    output_sha = None
    defects = []
    if path is not None and path.is_file():
        output_sha = file_hash(path)
        try:
            measured = engines.qa_media(
                path,
                aspect=plan.get("aspect", "16:9"),
                planned_seconds=sum(
                    float(sc.get("seconds", 0)) for sc in plan.get("scenes", [])
                ),
            )
            checks = measured.get("checks", [])
            verdict = (
                "pass"
                if measured.get("passed") is True
                and checks
                and all(c.get("passed") is True for c in checks)
                else "fail"
            )
            machine = {
                **measured,
                "verdict": verdict,
                "uncertainty": 0.0,
                "evaluator": {
                    "id": "engines.qa_media",
                    "source": "ffmpeg measurements",
                },
                "output_sha256": output_sha,
            }
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            machine = _unknown(
                f"machine QA execution error: {type(exc).__name__}: {exc}"
            )
    semantic, viewing = (
        _unknown("independent visual review not executed"),
        _unknown("viewing/listening not executed"),
    )
    if output_sha:
        receipts = record.get("quality_evidence", [])
        semantic, semantic_defects = _review_stage(
            "semantic_qa", rubric, receipts, output_sha
        )
        viewing, viewing_defects = _review_stage(
            "viewing_qa", rubric, receipts, output_sha
        )
        defects = semantic_defects + viewing_defects
    return {
        "schema_version": 1,
        "rubric": rubric,
        "output_sha256": output_sha,
        "rubric_matches_record": (record.get("rubric") or {}).get("sha256")
        == rubric["sha256"],
        "rubric_timing": "not_attested_by_evaluator",
        "machine_qa": machine,
        "semantic_qa": semantic,
        "viewing_qa": viewing,
        "defects": defects,
        "repair_plan": plan_partial_repair(plan, record, defects),
        "overall_verdict": _verdict([machine, semantic, viewing]),
    }


_REPAIRS = {
    "fake_text": (
        "captions",
        ["replace_with_typeset_overlay", "regenerate_clean_background_then_typeset"],
    ),
    "text_legibility": ("captions", ["reflow_typeset_captions", "split_caption_cues"]),
    "character_inconsistency": (
        "visual",
        ["regenerate_with_character_reference", "switch_to_locked_character_asset"],
    ),
    "character_consistency": (
        "visual",
        ["regenerate_with_character_reference", "switch_to_locked_character_asset"],
    ),
    "motion": ("motion", ["retime_and_reanimate_shot", "switch_motion_strategy"]),
    "voice": (
        "voice",
        ["resynthesize_target_utterance", "change_pronunciation_or_voice_strategy"],
    ),
    "audio_sync": ("voice", ["realign_target_audio", "resynthesize_and_retime"]),
    "audio_quality": ("voice", ["remix_target_audio", "replace_target_audio_source"]),
    "transition": ("transition", ["reedit_boundary", "switch_to_explicit_cut"]),
    "intent_alignment": ("visual", ["revise_shot_direction", "switch_visual_strategy"]),
}


def plan_partial_repair(
    plan: dict[str, Any], record: dict[str, Any], defects: list[dict[str, Any]]
) -> dict[str, Any]:
    """Propose cache impact only. Core owns execution, leases and authorization.

    Segment compositing may include good shots; asset generation must reuse them.
    Absolute timeline shifts require core's normal run-key recalculation as well.
    """
    scenes = {s["scene_id"]: s for s in plan.get("scenes", [])}
    actions, blocked = [], []
    affected, bad_shots = set(), set()
    for defect in defects:
        sid, shot, code = (
            defect.get("scene_id"),
            defect.get("shot_id"),
            defect.get("code"),
        )
        if sid not in scenes or code not in _REPAIRS:
            blocked.append(
                {"defect": deepcopy(defect), "reason": "unknown scene or repair code"}
            )
            continue
        shots = {s["shot_id"] for s in scenes[sid].get("shots", [])}
        if shot is not None and shot not in shots:
            blocked.append(
                {
                    "defect": deepcopy(defect),
                    "reason": "shot is not part of target scene",
                }
            )
            continue
        layer, strategies = _REPAIRS[code]
        if defect.get("layer", layer) != layer:
            blocked.append(
                {
                    "defect": deepcopy(defect),
                    "reason": "layer conflicts with repair code",
                }
            )
            continue
        history = [
            h
            for h in record.get("repair_history", [])
            if h.get("code") == code
            and h.get("scene_id") == sid
            and h.get("shot_id") == shot
            and h.get("outcome") == "failed"
        ]
        switch = len(history) >= 2
        strategy = strategies[1 if switch else 0]
        if sum(h.get("strategy") == strategies[-1] for h in history) >= 2:
            blocked.append(
                {
                    "defect": deepcopy(defect),
                    "reason": "both strategies exhausted; independent redirection required",
                }
            )
            continue
        affected.add(sid)
        bad_shots.update({shot} if shot else shots)
        actions.append(
            {
                "scene_id": sid,
                "shot_id": shot,
                "code": code,
                "layer": layer,
                "strategy": strategy,
                "strategy_switched": switch,
                "prior_failures": len(history),
                "evidence": deepcopy(defect.get("evidence", [])),
                "requires_reevaluation": ["machine_qa", "semantic_qa", "viewing_qa"],
            }
        )
    segments = record.get("segments", [])
    invalid_segments = [
        s["id"] for s in segments if affected.intersection(s.get("scene_ids", []))
    ]
    preserve_keys = {
        s["id"]: s["run_key"]
        for s in segments
        if s["id"] not in invalid_segments
        and s.get("run_key")
        and s.get("status") == "succeeded"
    }
    assets = record.get("assets") or {}
    asset_keys, invalid_asset_keys = [], []
    for group in ("scenes", "voices"):
        for sid in assets.get(group, {}):
            touched = any(
                a["scene_id"] == sid
                and (
                    a["layer"] == "visual"
                    if group == "scenes"
                    else a["layer"] == "voice"
                )
                for a in actions
            )
            if not touched:
                asset_keys.append(f"{group}.{sid}")
            else:
                invalid_asset_keys.append(f"{group}.{sid}")
    for shot in assets.get("shots", {}):
        if shot in bad_shots and any(
            a["layer"] == "visual" and (a["shot_id"] == shot or a["shot_id"] is None)
            for a in actions
        ):
            invalid_asset_keys.append(f"shots.{shot}")
        else:
            asset_keys.append(f"shots.{shot}")
    return {
        "schema_version": 1,
        "status": "blocked" if blocked else "planned" if actions else "not_needed",
        "actions": actions,
        "blocked": blocked,
        "invalidate": {
            "scene_ids": sorted(affected),
            "shot_ids": sorted(bad_shots),
            "layers": sorted({a["layer"] for a in actions}),
            "asset_keys": invalid_asset_keys,
            "segment_ids": invalid_segments,
            "final": bool(actions),
            "quality_evidence": bool(actions),
        },
        "preserve": {
            "scene_ids": sorted(set(scenes) - affected),
            "shot_ids": sorted(
                {sh["shot_id"] for s in scenes.values() for sh in s.get("shots", [])}
                - bad_shots
            ),
            "segment_run_keys": preserve_keys,
            "asset_groups": [
                k for k in assets if k not in {"scenes", "voices", "shots"}
            ],
            "asset_keys": asset_keys,
        },
        "cache_contract": "invalidate only listed generation layers; recompose affected segments; preserve other assets/run keys; bind all new reviews to new output hash",
    }
