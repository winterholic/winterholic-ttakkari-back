"""One real local Studio dispatch. User-structured direction, no model inference.

Run from repository root with PYTHONPATH=src:. and an empty output directory.
"""

import argparse
import hashlib
import json
import shutil
import wave
from pathlib import Path

from ttakkari.creative.render import inspect_media
from ttakkari.creative.service import Studio
from ttakkari.creative.v2 import dispatch
from ttakkari.creative.v2.duration import DurationRequest
from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection
from ttakkari.creative.v2.planner import ProductionDirection
from ttakkari.creative.v2.plans import PlanStore, build_plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    plans, studio = PlanStore(out / "plans"), Studio(out / "studio")
    if args.collect_only:
        collect(
            out, plans, json.loads((out / "result.json").read_text(encoding="utf-8"))
        )
        return
    assert not (out / "result.json").exists(), "Do not repeat the completed render"
    director = ProductionDirection(
        source="user_structured",
        reviewed=True,
        intent="지원 걷기 프리셋의 명시적 linear 보간 선택 전달 확인",
        scenes=[
            {
                "scene_id": "walk",
                "strategy": "frame_animation_2d",
                "preset": "walk_cycle",
                "interpolation_mode": "linear",
                "rationale": "관절 키포즈 사이 수치 선형 보간을 명시적으로 선택",
                "shots": [
                    {
                        "shot_id": "walk-shot",
                        "action": "지원 걷기 프리셋 사용",
                        "layers": ["visual", "audio"],
                    }
                ],
            }
        ],
    )
    plan = plans.create(
        build_plan(
            "명시적 linear 보간으로 마스코트 걷기 프리셋 제작",
            IntentSpec(
                selections=[
                    Selection(entry_id="C08.three_act", role="primary", confidence=1)
                ],
                info_items=[
                    InfoItem(id="walk", title="명시적 linear 보간", narration="")
                ],
            ),
            duration=DurationRequest(mode="preferred", seconds=8),
            director=director,
        )
    )
    assert not plan["render_gate"] and plan["approval"] is None
    plan = plans.approve(plan["id"], plan["revision"])
    assert plan["render_gate"], plan
    assert len(plan["render_plan"]["segments"]) == 1
    seconds = plan["render_plan"]["segments"][0]["seconds"]
    # Silence is an explicit master track; this experiment does not assess sound.
    music = out / "silent-master.wav"
    with wave.open(str(music), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\0\0" * round((seconds + 1) * 22050))
    dispatch.put_asset(
        plans,
        plan["id"],
        kind="music",
        scene_id=None,
        source=music,
        name=music.name,
        meta=inspect_media(music),
    )
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    result = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    collect(out, plans, result)


def collect(out, plans, result):
    """Collect existing evidence, including QA failures, without rendering again."""
    assert result["state"] in {"succeeded", "qa_failed"}, result
    plan = plans.get(result["plan_id"])
    store = dispatch.ProductionStore(plans)
    record = store.read(plan["id"])["dispatches"][0]
    segment = record["segments"][0]
    key = segment["run_key"]
    work = store.folder(plan["id"]) / "engine-work" / key
    execution = json.loads((work / "input.json").read_text(encoding="utf-8"))
    project = json.loads((work / "animation_spec.json").read_text(encoding="utf-8"))
    manifest = json.loads((work / "render_manifest.json").read_text(encoding="utf-8"))
    assert (
        execution["interpolation_mode"]
        == project["interpolation_mode"]
        == manifest["interpolation_mode"]
        == "linear"
    )
    assert execution["revision"] == plan["approval"]["revision"] == plan["revision"]
    final, _, _ = dispatch.result_media(plans, plan["id"], plan["revision"], "final")
    shutil.copy2(final, out / "studio-linear-final.mp4")
    shutil.copy2(work / "source.mp4", out / "studio-linear-producer.mp4")
    for name in (
        "input.json",
        "animation_spec.json",
        "render_manifest.json",
        "receipt.json",
    ):
        shutil.copy2(work / name, out / name)
    (out / "approved-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "environment": "Codex worker sandbox, local Studio/PlanStore/ProductionStore",
        "inference": "not performed; user_structured direction",
        "plan_id": plan["id"],
        "revision": plan["revision"],
        "run_key": key,
        "state": result["state"],
        "selection": execution["interpolation_mode"],
        "project_selection": project["interpolation_mode"],
        "frames": manifest["total_frames_rendered"],
        "quality": result["quality_report"]["overall_verdict"],
        "final_sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
        "producer_sha256": segment["producer_output"]["sha256"],
        "final_probe": inspect_media(final),
        "audio": "explicit silent master; listening not performed",
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
