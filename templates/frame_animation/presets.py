"""프레임 애니메이션 2D 모션 프리셋 및 표준 키포즈 템플릿.

캐릭터 리깅 레이어(머리, 몸통, 사지, 표정)를 활용하여
걷기(walk cycle), 손흔들기(wave), 점프 및 환호(jump & cheer) 등의
실제 2D 동작을 생성한다.
"""

from __future__ import annotations

import math
from typing import Any
from ttakkari.creative.v2.frame_animation import (
    AnimationScene,
    CameraKeyframe,
    LayerTransform,
    RiggedPose,
    SubtitleCue,
)


def make_walk_cycle_poses(
    start_time: float,
    duration: float,
    steps: int = 4,
    distance_x: float = 300.0,
    base_x: float = -150.0,
    base_y: float = 0.0,
) -> list[tuple[float, RiggedPose]]:
    """좌우 다리와 팔이 교차하며 전진하는 걷기 동작 키포즈 시퀀스 생성."""
    poses: list[tuple[float, RiggedPose]] = []
    num_phases = steps * 2  # 반보(half step) 단위
    dt = duration / num_phases

    for i in range(num_phases + 1):
        t = start_time + i * dt
        prog = min(1.0, i / num_phases)
        cur_x = base_x + distance_x * prog

        # 보행 시 약간의 바운스 (수직 위아래 모션)
        phase_rad = i * math.pi / 2.0
        bounce_y = -abs(math.sin(phase_rad)) * 14.0

        # 다리 및 팔 스윙 각도 (사인 곡선)
        leg_l_angle = math.sin(phase_rad) * 28.0
        leg_r_angle = -math.sin(phase_rad) * 28.0
        arm_l_angle = -math.sin(phase_rad) * 32.0
        arm_r_angle = math.sin(phase_rad) * 32.0

        pose = RiggedPose(
            pose_id=f"walk_{i}",
            expression="normal",
            root_x=cur_x,
            root_y=base_y + bounce_y,
            parts={
                "arm_left": LayerTransform(rotation_deg=arm_l_angle),
                "arm_right": LayerTransform(rotation_deg=arm_r_angle),
                "leg_left": LayerTransform(rotation_deg=leg_l_angle),
                "leg_right": LayerTransform(rotation_deg=leg_r_angle),
                "head_base": LayerTransform(rotation_deg=math.sin(phase_rad * 0.5) * 4.0),
                "face": LayerTransform(variant="normal"),
            },
        )
        poses.append((round(t, 3), pose))
    return poses


def make_wave_poses(
    start_time: float,
    duration: float,
    waves: int = 4,
    char_x: float = 150.0,
    char_y: float = 0.0,
) -> list[tuple[float, RiggedPose]]:
    """손을 머리 위로 들어 좌우로 흔들며 인사하는 동작 키포즈 시퀀스 생성."""
    poses: list[tuple[float, RiggedPose]] = []
    # 1. 손 올리기 (0.3초)
    raise_dt = min(0.4, duration * 0.2)
    poses.append((
        round(start_time, 3),
        RiggedPose(
            pose_id="wave_start",
            expression="smile",
            root_x=char_x,
            root_y=char_y,
            parts={
                "arm_left": LayerTransform(rotation_deg=-20.0),
                "arm_right": LayerTransform(rotation_deg=10.0),
                "leg_left": LayerTransform(rotation_deg=0.0),
                "leg_right": LayerTransform(rotation_deg=0.0),
                "face": LayerTransform(variant="smile"),
            },
        )
    ))
    poses.append((
        round(start_time + raise_dt, 3),
        RiggedPose(
            pose_id="wave_up",
            expression="smile",
            root_x=char_x,
            root_y=char_y,
            parts={
                "arm_left": LayerTransform(rotation_deg=-140.0),  # 번쩍 든 팔
                "arm_right": LayerTransform(rotation_deg=15.0),
                "leg_left": LayerTransform(rotation_deg=0.0),
                "leg_right": LayerTransform(rotation_deg=0.0),
                "head_base": LayerTransform(rotation_deg=-6.0),
                "face": LayerTransform(variant="smile"),
            },
        )
    ))

    # 2. 손 흔들기
    wave_duration = duration - raise_dt
    num_swings = waves * 2
    dt = wave_duration / num_swings
    for i in range(1, num_swings + 1):
        t = start_time + raise_dt + i * dt
        swing_phase = i * math.pi / 2.0
        swing_angle = -140.0 + math.sin(swing_phase) * 25.0
        head_tilt = -6.0 + math.sin(swing_phase) * 5.0
        expr = "wink" if (i % 4 == 0) else "smile"

        poses.append((
            round(t, 3),
            RiggedPose(
                pose_id=f"wave_swing_{i}",
                expression=expr,
                root_x=char_x,
                root_y=char_y,
                parts={
                    "arm_left": LayerTransform(rotation_deg=swing_angle),
                    "arm_right": LayerTransform(rotation_deg=10.0 + math.sin(swing_phase) * 5.0),
                    "leg_left": LayerTransform(rotation_deg=0.0),
                    "leg_right": LayerTransform(rotation_deg=0.0),
                    "head_base": LayerTransform(rotation_deg=head_tilt),
                    "face": LayerTransform(variant=expr),
                },
            )
        ))
    return poses


def make_jump_and_cheer_poses(
    start_time: float,
    duration: float,
    char_x: float = 150.0,
    char_y: float = 0.0,
) -> list[tuple[float, RiggedPose]]:
    """웅크렸다 위로 높이 점프하며 두 팔을 벌려 환호하는 모션."""
    poses: list[tuple[float, RiggedPose]] = []
    # 구간 분할:
    # 0.0 -> 준비(웅크리기)
    # 0.3 -> 최고점 점프 (두 팔 번쩍, 다리 굽힘, 환호 표정)
    # 0.7 -> 착지 (충격 흡수 웅크림)
    # 1.0 -> 기립 안착
    step_t = duration / 4.0

    # 1. 웅크리기 (Anticipation / Squash)
    poses.append((
        round(start_time, 3),
        RiggedPose(
            pose_id="jump_anticipation",
            expression="normal",
            root_x=char_x,
            root_y=char_y + 25.0,  # 아래로 낮춤
            root_scale=0.95,
            parts={
                "arm_left": LayerTransform(rotation_deg=35.0),
                "arm_right": LayerTransform(rotation_deg=-35.0),
                "leg_left": LayerTransform(rotation_deg=-15.0),
                "leg_right": LayerTransform(rotation_deg=15.0),
                "head_base": LayerTransform(rotation_deg=0.0),
                "face": LayerTransform(variant="normal"),
            },
        )
    ))

    # 2. 도약 및 최고점 (Peak / Stretch)
    poses.append((
        round(start_time + step_t, 3),
        RiggedPose(
            pose_id="jump_peak",
            expression="smile",
            root_x=char_x,
            root_y=char_y - 90.0,  # 공중으로 솟구침
            root_scale=1.05,
            parts={
                "arm_left": LayerTransform(rotation_deg=-155.0),  # 양팔 번쩍
                "arm_right": LayerTransform(rotation_deg=155.0),
                "leg_left": LayerTransform(rotation_deg=-20.0),
                "leg_right": LayerTransform(rotation_deg=20.0),
                "head_base": LayerTransform(rotation_deg=0.0),
                "face": LayerTransform(variant="smile"),
            },
        )
    ))

    # 3. 체공 유지
    poses.append((
        round(start_time + step_t * 2, 3),
        RiggedPose(
            pose_id="jump_float",
            expression="smile",
            root_x=char_x,
            root_y=char_y - 70.0,
            root_scale=1.02,
            parts={
                "arm_left": LayerTransform(rotation_deg=-165.0),
                "arm_right": LayerTransform(rotation_deg=165.0),
                "leg_left": LayerTransform(rotation_deg=-10.0),
                "leg_right": LayerTransform(rotation_deg=10.0),
                "head_base": LayerTransform(rotation_deg=0.0),
                "face": LayerTransform(variant="wink"),
            },
        )
    ))

    # 4. 착지 (Landing Squash)
    poses.append((
        round(start_time + step_t * 3, 3),
        RiggedPose(
            pose_id="jump_landing",
            expression="smile",
            root_x=char_x,
            root_y=char_y + 18.0,
            root_scale=0.96,
            parts={
                "arm_left": LayerTransform(rotation_deg=-70.0),
                "arm_right": LayerTransform(rotation_deg=70.0),
                "leg_left": LayerTransform(rotation_deg=-12.0),
                "leg_right": LayerTransform(rotation_deg=12.0),
                "head_base": LayerTransform(rotation_deg=0.0),
                "face": LayerTransform(variant="smile"),
            },
        )
    ))

    # 5. 복귀 (Settle)
    poses.append((
        round(start_time + duration, 3),
        RiggedPose(
            pose_id="jump_settle",
            expression="smile",
            root_x=char_x,
            root_y=char_y,
            root_scale=1.0,
            parts={
                "arm_left": LayerTransform(rotation_deg=-20.0),
                "arm_right": LayerTransform(rotation_deg=20.0),
                "leg_left": LayerTransform(rotation_deg=0.0),
                "leg_right": LayerTransform(rotation_deg=0.0),
                "head_base": LayerTransform(rotation_deg=0.0),
                "face": LayerTransform(variant="smile"),
            },
        )
    ))
    return poses
