"""One controlled caption repair using an existing repository 2D video.

No producer/model/browser/Blender is started. Uses the existing composition path.
Run once with: PYTHONPATH=src uv run python <this-file> <current-Run-outbox>
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from ttakkari.creative.render import ffmpeg, inspect_media
from ttakkari.creative.service import Studio
from ttakkari.creative.v2 import dispatch, production_review
from ttakkari.creative.v2.duration import SceneEdit
from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection
from ttakkari.creative.v2.planner import ProductionDirection
from ttakkari.creative.v2.plans import PlanStore, build_plan


def write(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def run(args):
    return subprocess.run(
        [ffmpeg(), "-v", "error", "-nostdin", *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def frame_hashes(path, crop=None):
    args = ["-i", path, "-map", "0:v:0"]
    if crop:
        args += ["-vf", crop]
    output = run([*args, "-f", "framemd5", "-"])
    return [
        line.rsplit(",", 1)[-1].strip()
        for line in output.splitlines()
        if line and not line.startswith("#")
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "evidence.json").exists():
        raise SystemExit(
            "existing evidence: do not repeat production; inspect evidence.json"
        )
    root = Path(__file__).resolve().parents[3]
    source = (
        root
        / "outbox/benchmark_b_work/corrected_20261107/run_02_remediated/ttakkari_2d_animation_final.mp4"
    )
    source_sha = dispatch._sha256_file(source)
    shutil.copy2(source, out / "source-2d.mp4")
    plans, studio = PlanStore(out / "plans"), Studio(out / "studio")
    intent = IntentSpec(
        selections=[Selection(entry_id="C08.three_act", role="primary", confidence=1)],
        info_items=[
            InfoItem(id=f"i{n}", title=f"보존 장면 {n + 1}", narration="")
            for n in range(3)
        ],
    )
    direction = ProductionDirection.model_validate(
        {
            "source": "user_structured",
            "reviewed": True,
            "intent": "기존 2D 영상의 모션과 자산을 보존하며 한 장면의 시험용 문구만 바꾼다",
            "domain_knowledge": ["기존 제작물 재사용; 새 2D 생성 없음"],
            "scenes": [
                {
                    "scene_id": f"i{n}",
                    "strategy": "slideshow",
                    "rationale": "uploaded existing 2D video playback; no generation",
                    "shots": [
                        {
                            "shot_id": f"shot-{n}",
                            "action": "기존 영상 프레임 재생",
                            "layers": ["visual", "captions", "audio"],
                        }
                    ],
                }
                for n in range(3)
            ],
        }
    )
    plan = plans.create(
        build_plan("부분 수정 경로 통제 실험", intent, director=direction)
    )
    plan = plans.edit(
        plan["id"],
        1,
        scenes=[
            SceneEdit(
                id=s["scene_id"],
                title="시험용 오기: 동작 정지" if i == 2 else s["title"],
                narration="",
                seconds=3,
                info_weight=s["info_weight"],
                essential=s["essential"],
            )
            for i, s in enumerate(plan["scenes"])
        ],
    )
    plan = plans.approve(plan["id"], 2)
    for scene in plan["scenes"]:
        dispatch.put_asset(
            plans,
            plan["id"],
            kind="scene",
            scene_id=scene["scene_id"],
            source=source,
            name=source.name,
            meta=inspect_media(source),
        )
    audio = out / "master.wav"
    run(["-y", "-i", source, "-t", "9", "-vn", "-c:a", "pcm_s16le", audio])
    dispatch.put_asset(
        plans,
        plan["id"],
        kind="music",
        scene_id=None,
        source=audio,
        name=audio.name,
        meta=inspect_media(audio),
    )
    dispatch.dispatch(plans, studio, plan["id"], 2, "title_only")
    before = dispatch.request_render(plans, studio, plan["id"], 2)
    assert before["state"] == "succeeded", before
    before_plan = plans.get(plan["id"])
    store = dispatch.ProductionStore(plans)
    before_data = store.read(plan["id"])
    old = before_data["dispatches"][-1]
    write(out / "before-plan.json", before_plan)
    write(out / "before-production.json", before_data)
    before_path, _, _ = dispatch.result_media(plans, plan["id"], 2, "final")
    shutil.copy2(before_path, out / "before-full.mp4")
    before_scene = Path(old["segments"][2]["output"]["file"])
    shutil.copy2(before_scene, out / "scene-before.mp4")
    run(
        [
            "-y",
            "-ss",
            "1.5",
            "-i",
            before_scene,
            "-frames:v",
            "1",
            out / "scene-before.png",
        ]
    )
    defect = {
        "scene_id": "i2",
        "shot_id": "shot-2",
        "code": "text_legibility",
        "layer": "captions",
        "origin": "controlled_injected_caption; not a user-discovered defect",
        "evidence": [
            {
                "path": str(out / "scene-before.png"),
                "sha256": dispatch._sha256_file(out / "scene-before.png"),
                "kind": "frame",
                "start_time": 7.5,
                "end_time": 7.5,
                "source_sha256": old["final"]["sha256"],
            }
        ],
    }
    proposal = dispatch.plan_repair(plans, plan["id"], 2, [defect])
    write(out / "repair-proposal.json", proposal)
    application = dispatch.apply_repair(
        plans,
        plan["id"],
        2,
        proposal["proposal_sha256"],
        [
            {
                "scene_id": "i2",
                "shot_id": "shot-2",
                "layer": "captions",
                "field": "title",
                "text": "원본 동작을 보존한 부분 수정",
            }
        ],
    )
    assert not plans.get(plan["id"])["render_gate"]
    write(out / "repair-application.json", application)
    try:
        dispatch.request_render(plans, studio, plan["id"], 3)
    except ValueError as exc:
        approval_rejection = str(exc)
    else:
        raise AssertionError("repair bypassed approval")
    plans.approve(plan["id"], 3)
    dispatch.dispatch(plans, studio, plan["id"], 3, "title_only")
    after = dispatch.request_render(plans, studio, plan["id"], 3)
    assert after["state"] == "succeeded", after
    review = production_review.run_review(plans, studio, plan["id"], 3)
    after_data = store.read(plan["id"])
    new = after_data["dispatches"][-1]
    write(out / "after-plan.json", plans.get(plan["id"]))
    write(out / "after-production.json", after_data)
    after_path, _, _ = dispatch.result_media(plans, plan["id"], 3, "final")
    shutil.copy2(after_path, out / "after-full.mp4")
    after_scene = Path(new["segments"][2]["output"]["file"])
    shutil.copy2(after_scene, out / "scene-after.mp4")
    run(
        [
            "-y",
            "-ss",
            "1.5",
            "-i",
            after_scene,
            "-frames:v",
            "1",
            out / "scene-after.png",
        ]
    )
    before_md5, after_md5 = frame_hashes(before_path), frame_hashes(after_path)
    assert len(before_md5) == len(after_md5)
    changed = [
        i for i, (a, b) in enumerate(zip(before_md5, after_md5, strict=True)) if a != b
    ]
    fps = 24
    assert changed and min(changed) >= 144 and max(changed) < 216, changed
    assert before_data["assets"] == after_data["assets"]
    preservation = []
    for old_seg, new_seg in zip(old["segments"][:2], new["segments"][:2], strict=True):
        assert (
            old_seg["run_key"] == new_seg["run_key"]
            and old_seg["output"] == new_seg["output"]
        )
        assert new_seg["cached_from_previous_revision"] is True
        preservation.append(
            {
                "segment_id": old_seg["id"],
                "scene_ids": old_seg["scene_ids"],
                "before_run_key": old_seg["run_key"],
                "after_run_key": new_seg["run_key"],
                "before_sha256": old_seg["output"]["sha256"],
                "after_sha256": new_seg["output"]["sha256"],
            }
        )
    motion = {}
    for label, path in [("before", before_scene), ("after", after_scene)]:
        hashes = frame_hashes(path, "crop=1280:400:0:0")
        assert hashes[7] != hashes[17], f"{label}: motion flattened"
        motion[label] = {
            "frame_7_md5": hashes[7],
            "frame_17_md5": hashes[17],
            "crop": "1280:400:0:0",
            "different": True,
        }
    evidence = {
        "experiment": "controlled injected caption wording; NOT a real user defect or independent quality pass",
        "source": {
            "path": str(source.relative_to(root)),
            "sha256": source_sha,
            "reused": True,
            "new_2d_render": False,
        },
        "plan_id": plan["id"],
        "before_revision": 2,
        "after_revision": 3,
        "target": {
            "scene_id": "i2",
            "shot_id": "shot-2",
            "layer": "captions",
            "before": plan["scenes"][2]["title"],
            "after": "원본 동작을 보존한 부분 수정",
        },
        "before_output_sha256": old["final"]["sha256"],
        "after_output_sha256": new["final"]["sha256"],
        "before_target": {
            "run_key": old["segments"][2]["run_key"],
            "sha256": old["segments"][2]["output"]["sha256"],
        },
        "after_target": {
            "run_key": new["segments"][2]["run_key"],
            "sha256": new["segments"][2]["output"]["sha256"],
        },
        "preserved_segments": preservation,
        "asset_sha256": {
            k: v["sha256"] for k, v in before_data["assets"]["scenes"].items()
        },
        "music_sha256": before_data["assets"]["music"]["sha256"],
        "assets_equal": True,
        "decoded_frames": len(changed) and len(before_md5),
        "fps": fps,
        "changed_frames_zero_based": changed,
        "changed_time_range_seconds": [min(changed) / fps, (max(changed) + 1) / fps],
        "unchanged_frames": len(before_md5) - len(changed),
        "before_frame_md5": before_md5,
        "after_frame_md5": after_md5,
        "source_motion_observation": motion,
        "approval_rejection": approval_rejection,
        "review": review,
        "quality": {
            stage: after["quality_report"][stage]["verdict"]
            for stage in ["machine_qa", "semantic_qa", "viewing_qa"]
        },
        "compositor_version": dispatch.COMPOSITOR_VERSION,
    }
    write(out / "evidence.json", evidence)
    print(
        f"preserved_segments={len(preservation)} assets_equal=True source_sha256={source_sha}"
    )
    print(
        f"changed_frames={min(changed)}..{max(changed)} ({len(changed)}/{len(before_md5)}) target=i2/captions; original motion retained"
    )
    print(
        f"review={review['status']} semantic=unknown viewing=unknown; new_2d_render=False"
    )


if __name__ == "__main__":
    main()
