"""프레임 애니메이션 2D 엔진 및 렌더러 단위 테스트."""


import pytest
from PIL import Image

from templates.frame_animation.presets import (
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
    LayerTransform,
    RiggedPose,
    SubtitleCue,
    _build_motion_eval,
    transform_and_paste,
)


def test_frame_animation_adapter_preflight():
    adapter = FrameAnimationAdapter()
    assert adapter.id == "skill.frame_animation_2d"
    assert "skill.frame_animation_2d" in adapter.providers
    assert "tool.ffmpeg" in adapter.providers
    pre = adapter.preflight()
    assert pre.ok is True
    assert pre.version is not None
    assert len(pre.missing) == 0


def test_animation_spec_validation():
    # 1. 정상 규격 (10초, 16:9, 장면 1개)
    scene = AnimationScene(
        scene_id="sc_01",
        duration_seconds=10.0,
        keyframes=[(0.0, RiggedPose(pose_id="idle"))],
    )
    spec = AnimationProjectSpec(
        project_id="anim_test_01",
        title="테스트 애니메이션",
        target_duration=10.0,
        aspect_ratio="16:9",
        scenes=[scene],
    )
    errors = spec.validate()
    assert len(errors) == 0

    # 2. 재생 시간 위반 (< 8.0초)
    bad_spec = AnimationProjectSpec(
        project_id="anim_bad",
        title="너무 짧음",
        target_duration=5.0,
        scenes=[AnimationScene(scene_id="s1", duration_seconds=5.0)],
    )
    errs = bad_spec.validate()
    assert any("8.0~15.0초" in e or "8~15초" in e for e in errs)

    # 3. 지원하지 않는 화면비
    bad_aspect = AnimationProjectSpec(
        project_id="anim_bad_aspect",
        title="잘못된 화면비",
        target_duration=10.0,
        aspect_ratio="21:9",
        scenes=[AnimationScene(scene_id="s1", duration_seconds=10.0)],
    )
    errs = bad_aspect.validate()
    assert any("화면비" in e for e in errs)


def test_character_layer_builder():
    builder = CharacterLayerBuilder(base_scale=1.0)
    layers = builder.create_character_layers()

    # 필수 리깅 파츠 검증
    expected_parts = [
        "torso",
        "head_base",
        "face_normal",
        "face_smile",
        "face_wink",
        "arm_left",
        "arm_right",
        "leg_left",
        "leg_right",
    ]
    for part in expected_parts:
        assert part in layers
        assert isinstance(layers[part], Image.Image)
        assert layers[part].mode == "RGBA"
        # 알파 채널이 비어있지 않음 확인
        bbox = layers[part].getbbox()
        assert bbox is not None


def test_transform_and_paste_alpha_composite():
    canvas = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    layer = Image.new("RGBA", (100, 100), (255, 0, 0, 255))

    # 피벗을 (0.5, 0.5)로 설정하고 중앙에 회전하여 합성
    transform_and_paste(
        canvas=canvas,
        layer_img=layer,
        center_pos=(200, 200),
        pivot_norm=(0.5, 0.5),
        rotation_deg=45.0,
        scale=(1.0, 1.0),
        alpha=1.0,
    )
    # 중심점 부근 픽셀이 붉은색이어야 함
    pixel = canvas.getpixel((200, 200))
    assert pixel[0] == 255
    assert pixel[3] > 0


def test_render_animation_short_scene(tmp_path):
    renderer = FrameAnimationRenderer()

    # 8.0초 테스트 씬 (제작 FPS 12, 재생 FPS 24)
    walk_poses = make_walk_cycle_poses(start_time=0.0, duration=4.0, steps=2, distance_x=100.0)
    wave_poses = make_wave_poses(start_time=4.0, duration=4.0, waves=2, char_x=-50.0)
    all_poses = walk_poses + wave_poses

    scene = AnimationScene(
        scene_id="scene_01",
        duration_seconds=8.0,
        keyframes=all_poses,
        camera_keyframes=[
            CameraKeyframe(time_sec=0.0, center_x=0.5, zoom=1.0),
            CameraKeyframe(time_sec=8.0, center_x=0.5, zoom=1.1),
        ],
        subtitles=[
            SubtitleCue(start_sec=0.5, end_sec=3.5, text="따까리가 힘차게 걸어옵니다!"),
            SubtitleCue(start_sec=4.0, end_sec=7.5, text="반갑게 인사를 건넵니다!"),
        ],
    )

    spec = AnimationProjectSpec(
        project_id="test_render_01",
        title="단위 테스트 8초 렌더",
        target_duration=8.0,
        aspect_ratio="16:9",
        production_fps=12,
        playback_fps=24,
        interpolation_mode="hold",
        scenes=[scene],
    )

    out_mp4 = tmp_path / "test_output.mp4"
    manifest = renderer.render_project(
        spec=spec,
        output_dir=tmp_path / "work",
        output_mp4=out_mp4,
        save_keyframes=True,
    )

    assert out_mp4.is_file()
    assert out_mp4.stat().st_size > 50000
    assert manifest["qa"]["passed"] is True
    assert manifest["qa"]["probe"]["has_video"] is True
    assert manifest["qa"]["probe"]["has_audio"] is True
    motion_eval = manifest["motion_eval"]
    assert motion_eval["same_source_layer_assets_reused"] is True
    assert motion_eval["motion_continuity_score"] is None
    assert motion_eval["evaluation_verdict"].startswith("NOT_RATED")
    # 재생 시간 오차 검사
    duration = manifest["qa"]["probe"]["duration"]
    assert abs(duration - 8.0) <= 0.4


def test_walk_cycle_has_measurable_limb_swing_and_bounce():
    poses = make_walk_cycle_poses(0.0, 4.0, steps=2)
    leg_angles = [pose.parts["leg_left"].rotation_deg for _, pose in poses]
    vertical_offsets = [pose.root_y for _, pose in poses]

    assert max(leg_angles) - min(leg_angles) >= 40.0
    assert max(vertical_offsets) - min(vertical_offsets) >= 10.0


def test_risky_root_displacement_is_not_reported_as_viewing_quality_pass():
    motion_eval = _build_motion_eval(
        average_displacement=42.5,
        max_displacement=180.0,
        production_frames=120,
    )

    assert motion_eval["teleport_glitch_detected"] is True
    assert motion_eval["motion_continuity_score"] is None
    assert motion_eval["motion_continuity_rating"] == "not_evaluated"
    assert motion_eval["evaluation_verdict"].startswith("NOT_RATED")
    assert motion_eval["identity_comparison"] == "not_performed"


def test_wave_cycle_has_multiple_arm_angles():
    poses = make_wave_poses(0.0, 3.0, waves=2)
    arm_angles = [pose.parts["arm_left"].rotation_deg for _, pose in poses]

    assert len(set(arm_angles)) >= 3
    assert max(arm_angles) - min(arm_angles) >= 30.0


def test_pose_linear_mode_interpolates_joint_rotation_but_keeps_discrete_face():
    renderer = FrameAnimationRenderer()
    start = RiggedPose(
        pose_id="start",
        parts={
            "arm_left": LayerTransform(rotation_deg=-20.0),
            "face": LayerTransform(variant="smile"),
        },
    )
    end = RiggedPose(
        pose_id="end",
        root_x=100.0,
        parts={
            "arm_left": LayerTransform(rotation_deg=-140.0),
            "face": LayerTransform(variant="wink"),
        },
    )
    frames = [(0.0, start), (1.0, end)]

    middle = renderer.evaluate_pose(frames, 0.5, mode="linear")
    held = renderer.evaluate_pose(frames, 0.5, mode="hold")

    assert middle.parts["arm_left"].rotation_deg == pytest.approx(-80.0)
    assert middle.root_x == pytest.approx(50.0)
    assert middle.parts["face"].variant == "smile"
    assert held is start
