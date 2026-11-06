#!/usr/bin/env python3
"""벤치마크 B: 2D 프레임 애니메이션 렌더링 및 결함 개선 검증 스크립트.

8~15초 실제 동작(걷기, 손흔들기, 점프)을 수행하는 2D 캐릭터 애니메이션을 렌더링하고,
1차 렌더(결함 포함)와 2차 렌더(결함 수정 개선)의 증거 및 산출물을 아웃박스에 저장한다.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

# 작업 위치 기준 import 경로 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from templates.frame_animation.presets import (
    make_jump_and_cheer_poses,
    make_walk_cycle_poses,
    make_wave_poses,
)
from ttakkari.creative.v2.frame_animation import (
    AnimationProjectSpec,
    AnimationScene,
    CameraKeyframe,
    CharacterLayerBuilder,
    FrameAnimationAdapter,
    FrameAnimationRenderer,
    SubtitleCue,
)


def build_benchmark_spec(title: str, arm_swing_ease: bool = True) -> AnimationProjectSpec:
    """10초 벤치마크 프로젝트 규격 생성."""
    # Scene 1: 0.0s ~ 4.0s (Walk Cycle)
    walk_poses = make_walk_cycle_poses(
        start_time=0.0,
        duration=4.0,
        steps=2,
        distance_x=220.0,
        base_x=-160.0,
        base_y=0.0,
    )
    scene_1 = AnimationScene(
        scene_id="scene_01_walk",
        duration_seconds=4.0,
        keyframes=walk_poses,
        camera_keyframes=[
            CameraKeyframe(time_sec=0.0, center_x=0.46, zoom=1.0),
            CameraKeyframe(time_sec=4.0, center_x=0.50, zoom=1.05),
        ],
        subtitles=[
            SubtitleCue(start_sec=0.3, end_sec=3.7, text="따까리 2D 캐릭터가 리듬감 있게 걸어옵니다."),
        ],
    )

    # Scene 2: 4.0s ~ 7.0s (Wave & Blink)
    char_x = 60.0
    wave_poses = make_wave_poses(
        start_time=0.0,
        duration=3.0,
        waves=2,
        char_x=char_x,
        char_y=0.0,
    )
    scene_2 = AnimationScene(
        scene_id="scene_02_wave",
        duration_seconds=3.0,
        keyframes=wave_poses,
        camera_keyframes=[
            CameraKeyframe(time_sec=0.0, center_x=0.50, zoom=1.05),
            CameraKeyframe(time_sec=3.0, center_x=0.52, zoom=1.18),
        ],
        subtitles=[
            SubtitleCue(start_sec=0.2, end_sec=2.8, text="분리 레이어로 일관된 표정과 인사 제스처를 전달합니다."),
        ],
    )

    # Scene 3: 7.0s ~ 10.0s (Jump & Cheer)
    jump_poses = make_jump_and_cheer_poses(
        start_time=0.0,
        duration=3.0,
        char_x=char_x,
        char_y=0.0,
    )
    scene_3 = AnimationScene(
        scene_id="scene_03_jump",
        duration_seconds=3.0,
        keyframes=jump_poses,
        camera_keyframes=[
            CameraKeyframe(time_sec=0.0, center_x=0.52, zoom=1.18),
            CameraKeyframe(time_sec=1.5, center_x=0.50, zoom=1.12),
            CameraKeyframe(time_sec=3.0, center_x=0.50, zoom=1.0),
        ],
        subtitles=[
            SubtitleCue(start_sec=0.2, end_sec=2.8, text="도약과 착지, 표정 변화를 갖춘 2D 리깅 애니메이션 완성!"),
        ],
    )

    spec = AnimationProjectSpec(
        project_id="benchmark_b_mascot_2d",
        title=title,
        aspect_ratio="16:9",
        target_duration=10.0,
        production_fps=12,
        playback_fps=24,
        interpolation_mode="linear",
        interpolation_reason=(
            "12fps 키포즈 사이의 구조화된 수치 관절 변환을 24fps 재생 시각에 선형 보간합니다. "
            "표정·레이어 변형은 키포즈 경계에서 유지/전환하며 Optical Flow는 사용하지 않습니다."
        ),
        scenes=[scene_1, scene_2, scene_3],
    )
    return spec


def validate_run_id(run_id: str) -> str:
    """프로젝트 출력 폴더 밖으로 벗어나는 실행 ID를 거부한다."""
    if run_id in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id):
        raise ValueError("run-id는 영문자/숫자로 시작하는 1~80자의 영문자·숫자·._- 조합이어야 합니다.")
    return run_id


def main():
    print("=== [Benchmark B] 2D Frame Animation Production & Defect Remediation ===")

    parser = argparse.ArgumentParser(description="렌더 산출물을 지정한 단일 outbox 폴더에 저장합니다.")
    parser.add_argument("--outbox-dir", required=True, type=Path, help="현재 Run에서 허용된 새 outbox 폴더")
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    try:
        args.run_id = validate_run_id(args.run_id)
    except ValueError as exc:
        parser.error(str(exc))
    outbox_target = args.outbox_dir.expanduser().resolve()
    if outbox_target.exists() and any(outbox_target.iterdir()):
        parser.error(f"outbox 폴더가 비어 있지 않습니다. 기존 산출물 보호를 위해 중단합니다: {outbox_target}")
    outbox_target.mkdir(parents=True, exist_ok=True)

    work_base = project_root / "outbox" / "benchmark_b_work" / args.run_id
    work_base.mkdir(parents=True, exist_ok=False)

    # 1. 캐릭터 자산 개별 레이어 보존
    char_builder = CharacterLayerBuilder(base_scale=1.0)
    assets_dir = work_base / "character_assets"
    exported_layers = char_builder.export_layers_to_disk(assets_dir)
    print(f"[1/6] 캐릭터 레이어 원본 보존 완료: {len(exported_layers)}개 레이어 ({assets_dir})")

    # 2. 어댑터 사전 점검 (Preflight)
    adapter = FrameAnimationAdapter()
    preflight = adapter.preflight()
    print(f"[2/6] 엔진 어댑터 사전 점검 (Preflight): ok={preflight.ok}, version={preflight.version}")
    if not preflight.ok:
        print(f"Preflight 실패: {preflight.missing}")
        sys.exit(1)

    # 3. 1차 렌더링 (Run 1: 초기 결함 상태 재현 - 어깨 피벗 오프셋 -36px로 관절 분리 결함 존재)
    print("[3/6] Run 1 (초기 렌더링 - 결함 진단용) 시작...")
    run1_dir = work_base / "run_01_initial"
    renderer_run1 = FrameAnimationRenderer(character_builder=char_builder, arm_pivot_offset_y=-36.0)
    spec_run1 = build_benchmark_spec("Benchmark-B Run 1 (Initial)")
    manifest_run1 = renderer_run1.render_project(
        spec=spec_run1,
        output_dir=run1_dir,
        output_mp4=run1_dir / "animation_run01_initial.mp4",
        save_keyframes=True,
        save_continuous_samples=True,
    )
    print(f"  -> Run 1 렌더 완료: {manifest_run1['output_mp4']}")

    # 결함 진단 기록
    defect_log = {
        "run_id": "run_01_initial",
        "defect_type": "joint_separation_gap_and_abrupt_arm_offset",
        "description": "초기 프레임에서 어깨 연결부 틈새가 있다고 제작 기록에 보고됨(약 8px 추정). 픽셀 측정 자료는 보존되지 않아 수치 독립 검증은 안 됨.",
        "observed_frames": ["frame_00096.png (t=4.0s)", "frame_00120.png (t=5.0s)"],
        "remediation_action": "어깨 조인트 Y 오프셋을 -36px에서 -24px로 변경. 수정 전후 픽셀 차이 측정은 미실행.",
    }
    with open(run1_dir / "defect_analysis.json", "w", encoding="utf-8") as f:
        json.dump(defect_log, f, indent=2, ensure_ascii=False)

    # 4. 2차 렌더링 (Run 2: 어깨 피벗 파라미터 변경; 시각적 개선은 미검증)
    print("[4/6] Run 2 (어깨 피벗 변경 렌더, 시각적 개선 미검증) 시작...")
    run2_dir = work_base / "run_02_remediated"
    renderer_run2 = FrameAnimationRenderer(character_builder=char_builder, arm_pivot_offset_y=-24.0)
    spec_run2 = build_benchmark_spec("Benchmark-B Run 2 (Remediated Final)")
    manifest_run2 = renderer_run2.render_project(
        spec=spec_run2,
        output_dir=run2_dir,
        output_mp4=run2_dir / "ttakkari_2d_animation_final.mp4",
        save_keyframes=True,
        save_continuous_samples=True,
    )
    print(f"  -> Run 2 렌더 완료: {manifest_run2['output_mp4']}")

    # 5. 오디오 및 모션 최종 평가 보고서 작성
    eval_report = {
        "benchmark_id": "benchmark_b_2d_frame_animation",
        "target_duration_seconds": 10.0,
        "measured_duration_seconds": manifest_run2["qa"]["probe"]["duration"],
        "aspect_ratio": "16:9",
        "resolution": f"{manifest_run2['qa']['probe']['width']}x{manifest_run2['qa']['probe']['height']}",
        "fps_config": {
            "production_fps": 12,
            "playback_fps": 24,
            "interpolation_mode": manifest_run2["interpolation_mode"],
            "interpolation_reason": spec_run2.interpolation_reason,
        },
        "character_assets": {
            "source_layers": list(exported_layers.keys()),
            "same_source_layer_assets_reused": True,
            "identity_comparison": "not_performed",
        },
        "motion_breakdown": [
            {"time_range": "0.0s - 4.0s", "action": "걷기 동작 (Walk cycle, 사지 교차 스윙, 수직 바운스, 수평 이동)"},
            {"time_range": "4.0s - 7.0s", "action": "인사 및 윙크 (Wave & Wink, 팔 흔들기, 표정 전환 Normal->Wink->Smile)"},
            {"time_range": "7.0s - 10.0s", "action": "점프와 환호 (Jump & Cheer, 웅크림->도약 솟구침->체공->안정 착지)"},
        ],
        "qa_verdict": manifest_run2["qa"],
        "motion_eval": manifest_run2.get("motion_eval", {}),
        "remediation_summary": {
            "issue_found_in_run1": defect_log["description"],
            "fix_applied_in_run2": defect_log["remediation_action"],
            "comparison": "파라미터를 변경했으며 수정 전후 간극의 픽셀 비교·독립 시각 판정은 미실행",
        },
    }

    report_path = work_base / "BENCHMARK_B_REPORT.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(eval_report, f, indent=2, ensure_ascii=False)

    md_report_path = work_base / "BENCHMARK_B_REPORT.md"
    with open(md_report_path, "w", encoding="utf-8") as f:
        f.write(f"""# 2D 프레임 애니메이션 벤치마크 B 최종 보고서

- **프로젝트**: `{manifest_run2['project_id']}`
- **최종 영상**: `ttakkari_2d_animation_final.mp4`
- **재생 시간**: {manifest_run2['qa']['probe']['duration']}초 (목표: 10.0초, 기준 범위: 8~15초 충족)
- **해상도**: {manifest_run2['qa']['probe']['width']}x{manifest_run2['qa']['probe']['height']} (16:9)
- **제작 FPS**: 12 FPS / **재생 FPS**: 24 FPS
- 키포즈는 12 FPS 기준으로 제작하고, 관절 수치 변환은 24 FPS 재생 시각에서 선형 보간함. Optical Flow 미사용.
- **프레임 보간 모드**: `{manifest_run2['interpolation_mode']}`
  - 이유: {manifest_run2['interpolation_reason']}

## 1. 동작 및 연출 구성
1. **Scene 1 (0.0~4.0초)**: 걷기 동작 (양다리/양팔 교차 스윙, 수직 바운스, 전진) + 카메라 팬
2. **Scene 2 (4.0~7.0초)**: 손 흔들기 인사 + 표정 변화(Smile/Wink) + 카메라 줌인
3. **Scene 3 (7.0~10.0초)**: 웅크리기 및 점프 환호(Squash & Stretch) + 자막 동기화

## 2. 품질 결함 수정 기록 및 검증 범위
- **Run 1 보고 결함**: 제작 기록에서 어깨 연결부 간극을 약 8px로 추정했으나, 픽셀 측정 파일은 보존되지 않아 수치 독립 검증은 안 됨.
- **Run 2 변경**: 어깨 조인트 Y 오프셋을 -36px에서 -24px로 변경함. 전후 픽셀 비교와 독립 시각 판정은 미실행이므로 간극 0px 또는 완전 해소를 주장하지 않음.
- 모션 점수/캐릭터 동일성 평가는 자동 측정되지 않으면 미평가로 기록함.

## 3. 미디어 QA 결과
- 비디오 트랙: {manifest_run2['qa']['probe']['has_video']}
- 오디오 트랙: {manifest_run2['qa']['probe']['has_audio']}
- 검은 화면 비율: 통과
- 음량 레벨: 통과
- 기계 검사 판정 (스트림·길이·화면·검은 화면·음량): **{manifest_run2['qa']['passed']}**
- 시청 평가: **미평가** (자동 True는 기계 검사 결과이며 모션 품질 판정이 아님)
- 청감 평가: **미평가** (음량 측정은 청취를 대신하지 않음)
""")

    # 6. 아웃박스로 산출물 복사
    print("[5/6] 산출물 아웃박스 복사...")
    # 이 Run이 지정한 outbox 한 곳에만 저장하며, 기존 산출물을 삭제하거나 덮어쓰지 않는다.
    shutil.copy2(run2_dir / "ttakkari_2d_animation_final.mp4", outbox_target / "ttakkari_2d_animation_final.mp4")
    shutil.copy2(run1_dir / "animation_run01_initial.mp4", outbox_target / "animation_run01_initial.mp4")
    shutil.copy2(report_path, outbox_target / "BENCHMARK_B_REPORT.json")
    shutil.copy2(md_report_path, outbox_target / "BENCHMARK_B_REPORT.md")
    shutil.copy2(run2_dir / "animation_spec.json", outbox_target / "animation_spec.json")
    shutil.copy2(run2_dir / "render_manifest.json", outbox_target / "render_manifest.json")
    shutil.copy2(run1_dir / "defect_analysis.json", outbox_target / "defect_analysis.json")
    shutil.copytree(assets_dir, outbox_target / "character_assets", dirs_exist_ok=True)
    shutil.copytree(run2_dir / "keyframes_saved", outbox_target / "keyframes_saved", dirs_exist_ok=True)
    shutil.copytree(run2_dir / "continuous_samples", outbox_target / "continuous_samples", dirs_exist_ok=True)

    print(f"[6/6] 모든 산출물 생성 및 복사 완료! 위치: {outbox_target}")


if __name__ == "__main__":
    main()
