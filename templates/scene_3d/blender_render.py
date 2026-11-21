"""Blender 기반 3D 씬 프로그래밍 렌더러.

헤드리스 환경(CPU/GPU)에서 카메라 동선, 조명 애니메이션, 오브젝트 변형/공전이 포함된
8~15초 3D 씬을 렌더링하고 MP4 영상으로 합성한다.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time

try:
    import bpy
except ImportError:
    bpy = None


def _look_at_euler(camera_location, target_location):
    """Blender XYZ Euler 회전으로 카메라 local -Z를 목표점에 정렬한다."""
    dx = target_location[0] - camera_location[0]
    dy = target_location[1] - camera_location[1]
    dz = target_location[2] - camera_location[2]
    dist_xy = math.hypot(dx, dy)
    if dist_xy == 0 and dz == 0:
        raise ValueError("camera position and target must differ")
    pitch = math.atan2(dz, dist_xy)
    yaw = math.atan2(dy, dx)
    return (math.pi / 2 + pitch, 0.0, yaw - math.pi / 2)


def _euler_forward(rotation):
    """XYZ Euler, roll-free camera's local -Z forward axis in world coordinates."""
    x, _, z = rotation
    return (-math.sin(x) * math.sin(z), math.sin(x) * math.cos(z), -math.cos(x))


def _target_alignment(camera_location, target_location, rotation):
    forward = _euler_forward(rotation)
    target = tuple(target_location[i] - camera_location[i] for i in range(3))
    length = math.sqrt(sum(value * value for value in target))
    if length == 0:
        return -1.0
    return sum(forward[i] * target[i] / length for i in range(3))


def setup_scene(
    width: int = 1280,
    height: int = 720,
    total_frames: int = 240,
    fps: int = 24,
    samples: int = 8,
    render_config: dict | None = None,
):
    render_config = render_config or {}
    style = render_config.get("style", {})
    scene_config = render_config.get("scene", {"scene_type": "crystal_orbit"})
    if scene_config.get("scene_type", "crystal_orbit") != "crystal_orbit":
        raise ValueError("unsupported scene_type; only crystal_orbit is implemented")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.resolution_x = width
    sc.render.resolution_y = height
    sc.render.fps = fps
    sc.frame_start = 1
    sc.frame_end = total_frames
    sc.render.image_settings.file_format = "PNG"

    # Cycles CPU로 안정적 헤드리스 렌더링
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = False

    # 월드 배경 (어두운 시네마틱 스페이스)
    world = bpy.data.worlds.new("CinematicWorld")
    sc.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    light_colors = {
        "cinematic_fog": ((0.015, 0.02, 0.035, 1.0), (0.2, 0.7, 1.0), (1.0, 0.6, 0.2)),
        "studio_clean": ((0.16, 0.18, 0.22, 1.0), (0.55, 0.72, 1.0), (1.0, 0.82, 0.58)),
        "cyberpunk_neon": ((0.025, 0.005, 0.05, 1.0), (1.0, 0.05, 0.55), (0.05, 0.65, 1.0)),
        "monochrome_dramatic": ((0.008, 0.008, 0.012, 1.0), (0.85, 0.88, 1.0), (0.45, 0.5, 0.62)),
    }
    bg_color, pulse_color, orbit_color = light_colors.get(style.get("lighting", "cinematic_fog"), light_colors["cinematic_fog"])
    bg.inputs[0].default_value = bg_color
    bg.inputs[1].default_value = 0.65 if style.get("lighting") == "studio_clean" else 0.5

    # 머티리얼 헬퍼
    def make_mat(name, color, rough=0.3, metal=0.0, emit=0.0):
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        bsdf = m.node_tree.nodes["Principled BSDF"]
        bsdf.inputs["Base Color"].default_value = color
        bsdf.inputs["Roughness"].default_value = rough
        bsdf.inputs["Metallic"].default_value = metal
        if emit > 0:
            bsdf.inputs["Emission Color"].default_value = color
            bsdf.inputs["Emission Strength"].default_value = emit
        return m

    # 바닥 그리드 플레인
    bpy.ops.mesh.primitive_plane_add(size=30, location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "Floor"
    floor.data.materials.append(make_mat("FloorMat", (0.05, 0.07, 0.12, 1.0), rough=0.4, metal=0.8))

    # 중앙 크리스탈 (아이코스피어)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=1.2, location=(0, 0, 1.8))
    crystal = bpy.context.object
    crystal.name = "HeroCrystal"
    grade_colors = {
        "teal_orange": (0.2, 0.8, 1.0, 1.0),
        "neutral": (0.68, 0.78, 0.9, 1.0),
        "filmic_warm": (1.0, 0.46, 0.19, 1.0),
        "high_contrast": (0.78, 0.92, 1.0, 1.0),
    }
    crystal_mat = make_mat("CrystalMat", grade_colors.get(style.get("color_grade", "teal_orange"), grade_colors["teal_orange"]), rough=0.2, metal=0.1, emit=5.0)
    crystal.data.materials.append(crystal_mat)

    # 궤도 회전 링
    bpy.ops.mesh.primitive_torus_add(major_radius=2.6, minor_radius=0.08, location=(0, 0, 1.8))
    ring = bpy.context.object
    ring.name = "OrbitRing"
    ring.data.materials.append(make_mat("RingMat", (0.9, 0.7, 0.2, 1.0), rough=0.15, metal=0.9))

    # 궤도 큐브들 (4개)
    satellites = []
    for i in range(4):
        bpy.ops.mesh.primitive_cube_add(size=0.45, location=(0, 0, 0))
        sat = bpy.context.object
        sat.name = f"Satellite_{i}"
        sat.data.materials.append(make_mat(f"SatMat_{i}", (0.8, 0.85, 0.95, 1.0), rough=0.2, metal=0.85))
        satellites.append(sat)

    # 조명: 동적 포인트 라이트 2개 + 에어리어 라이트 1개
    bpy.ops.object.light_add(type="POINT", location=(0, 0, 3.5))
    pulse_light = bpy.context.object
    pulse_light.name = "PulseLight"
    pulse_light.data.energy = 250
    pulse_light.data.color = pulse_color

    bpy.ops.object.light_add(type="POINT", location=(4, -4, 4))
    orbit_light = bpy.context.object
    orbit_light.name = "OrbitLight"
    orbit_light.data.energy = 400
    orbit_light.data.color = orbit_color

    # 카메라
    bpy.ops.object.camera_add(location=(0, -8, 4), rotation=(math.radians(70), 0, 0))
    cam = bpy.context.object
    cam.name = "MainCamera"
    cam.data.lens = 38
    sc.camera = cam

    # 애니메이션 키프레임 생성 (카메라, 조명, 오브젝트)
    for f in range(1, total_frames + 1):
        sc.frame_set(f)
        u = (f - 1) / (total_frames - 1)
        t = (f - 1) / fps

        # 1. 크리스탈: 회전 + 상하 부유
        crystal.rotation_euler = (t * 0.8, t * 1.2, t * 0.5)
        crystal.location.z = 1.8 + math.sin(t * 2.5) * 0.25
        crystal.keyframe_insert(data_path="rotation_euler", frame=f)
        crystal.keyframe_insert(data_path="location", frame=f)

        # 2. 궤도 링: 기울어진 회전
        ring.rotation_euler = (math.radians(25) + math.sin(t * 1.5) * 0.2, t * -1.8, math.cos(t * 1.2) * 0.3)
        ring.keyframe_insert(data_path="rotation_euler", frame=f)

        # 3. 위성 큐브들: 원형 궤도 공전
        for idx, sat in enumerate(satellites):
            phase = idx * (math.pi / 2) + t * 1.4
            radius = 2.6
            sat.location = (
                math.cos(phase) * radius,
                math.sin(phase) * radius,
                1.8 + math.sin(phase * 2) * 0.4,
            )
            sat.rotation_euler = (t * 2, t * 3 + idx, 0)
            sat.keyframe_insert(data_path="location", frame=f)
            sat.keyframe_insert(data_path="rotation_euler", frame=f)

        # 4. 조명 애니메이션 (조명 움직임 및 펄스)
        orbit_light.location = (
            math.cos(t * 0.9) * 6.0,
            math.sin(t * 0.9) * 6.0,
            4.0 + math.sin(t * 1.8) * 1.5,
        )
        orbit_light.keyframe_insert(data_path="location", frame=f)

        pulse_energy = 250 + math.sin(t * 4.0) * 150
        pulse_light.data.energy = pulse_energy
        pulse_light.data.keyframe_insert(data_path="energy", frame=f)

        # 5. 카메라 모션 (복합 시네마틱 궤적: 크레인 다운 -> 오빗 회전 -> 풀백)
        camera_motion = style.get("camera_motion", "orbit_pulse")
        cam_angle = t * (0.12 if camera_motion == "crane_down" else 0.24 if camera_motion == "tracking_rackfocus" else 0.08 if camera_motion == "handheld_subtle" else 0.65) - math.pi / 2
        cam_dist = 8.5 - 1.2 * u if camera_motion == "crane_down" else 6.5 if camera_motion == "tracking_rackfocus" else 7.5 + math.sin(t * 0.5) * 1.8
        cam_z = 5.0 - 2.0 * u if camera_motion == "crane_down" else 2.6 if camera_motion == "tracking_rackfocus" else 3.2 + math.cos(t * 0.4) * 1.2
        if camera_motion == "handheld_subtle":
            cam_z += math.sin(t * 7.0) * 0.035
        cam.location = (
            math.cos(cam_angle) * cam_dist,
            math.sin(cam_angle) * cam_dist,
            cam_z,
        )
        # 카메라가 크리스탈 위치를 바라보도록 회전 계산
        target = crystal.location
        cam.rotation_euler = _look_at_euler(tuple(cam.location), tuple(target))
        aim_dot = _target_alignment(tuple(cam.location), tuple(target), tuple(cam.rotation_euler))
        if aim_dot < 0.999999:
            raise RuntimeError(f"camera target alignment failed at frame {f}: dot={aim_dot:.6f}")

        cam.keyframe_insert(data_path="location", frame=f)
        cam.keyframe_insert(data_path="rotation_euler", frame=f)

    return sc


def render_animation(output_dir: str, total_frames: int, ffmpeg_bin: str | None = None, render_config: dict | None = None) -> str:
    os.makedirs(output_dir, exist_ok=True)
    sc = bpy.context.scene

    frames_dir = os.path.join(output_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    print(f"Blender rendering {total_frames} frames to {frames_dir}...")
    t0 = time.time()
    transform_frames = []
    for f in range(1, total_frames + 1):
        sc.frame_set(f)
        frame_path = os.path.join(frames_dir, f"frame_{f:04d}.png")
        sc.render.filepath = frame_path
        bpy.ops.render.render(write_still=True)
        cam = sc.camera
        hero = sc.objects.get("HeroCrystal")
        camera_location = tuple(float(v) for v in cam.location)
        camera_rotation = tuple(float(v) for v in cam.rotation_euler)
        target_location = tuple(float(v) for v in hero.location)
        aim_dot = _target_alignment(camera_location, target_location, camera_rotation)
        transform_frames.append({
            "frame": f,
            "camera_location": camera_location,
            "camera_rotation_euler_xyz": camera_rotation,
            "target_location": target_location,
            "camera_target_dot": aim_dot,
            "hero_rotation_euler_xyz": tuple(float(v) for v in hero.rotation_euler),
        })
        if f % 24 == 0 or f == total_frames:
            elapsed = time.time() - t0
            print(f"Rendered frame {f}/{total_frames} ({elapsed:.1f}s, {elapsed/f:.2f}s/frame)")

    evidence_path = os.path.join(output_dir, "motion_evidence.json")
    with open(evidence_path, "w", encoding="utf-8") as evidence_file:
        json.dump({"render_config": render_config or {}, "fps": sc.render.fps, "frame_count": total_frames,
                   "transform_frames": transform_frames}, evidence_file, indent=2)
    print(f"Motion evidence saved to {evidence_path}; min camera_target_dot={min(x['camera_target_dot'] for x in transform_frames):.6f}")

    video_path = os.path.join(output_dir, "blender_scene_3d.mp4")
    ff = ffmpeg_bin or "ffmpeg"
    cmd = [
        ff,
        "-y",
        "-framerate",
        str(sc.render.fps),
        "-i",
        os.path.join(frames_dir, "frame_%04d.png"),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "20",
        video_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"Video encoded to {video_path}")
    return video_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--frames", type=int, default=192)  # 8s at 24fps
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--samples", type=int, default=4)
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--config", default="{}")
    args = parser.parse_args()

    config = json.loads(args.config)
    setup_scene(args.width, args.height, args.frames, args.fps, args.samples, config)
    render_animation(args.out, args.frames, args.ffmpeg, config)
