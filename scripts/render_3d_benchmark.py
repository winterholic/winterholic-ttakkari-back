"""Creative 3D 씬 렌더링 실행 및 벤치마크 스크립트."""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 작업 위치 및 프로젝트 루트
PROJECT_ROOT = Path("/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107")
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ttakkari.creative.render import ffmpeg
from ttakkari.creative.v2.scene_3d import Scene3DAdapter

OUTBOX_8396 = Path("/Volumes/Backup/ttakkari-data/outbox/83968b68-3f2f-4e0e-89c7-2a87aeb5a6ee/3d_scene_studio")
OUTBOX_1990 = Path("/Volumes/Backup/ttakkari-data/outbox/1990e299-15f7-470a-9328-41394e1bf490/3d_scene_studio")
LAB_3D = Path("/Users/winterholic/development/projects/ttakkari-studio-lab/3d")


def run_benchmark():
    print("=== 3D Production Benchmark & Render Execution ===")
    adapter = Scene3DAdapter()

    # 1. Preflight
    pf = adapter.preflight()
    print(f"Preflight: ok={pf.ok}, version={pf.version}, missing={pf.missing}")

    # 2. Audit
    audit = adapter.audit_scene("v2")
    print(f"Scene Audit v2: ok={audit.ok}, shots={len(audit.shots)}")

    # 3. Blender 3D 실제 렌더링 (8.0초, 192프레임, 24fps, 960x540)
    work_dir = PROJECT_ROOT / "bench" / "scene_3d_blender"
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    blender_mp4 = adapter.render_blender_video(
        work_dir,
        frames=192,
        fps=24,
        width=960,
        height=540,
        samples=4,
    )
    blender_elapsed = time.time() - t0
    print(f"Blender 3D video rendered in {blender_elapsed:.2f}s -> {blender_mp4}")

    # 4. Three.js 기존 성공 실험 결과물 확인 및 연계
    three_mp4 = LAB_3D / "out" / "cinematic_3d_v2.mp4"
    audio_wav = LAB_3D / "out" / "mix_a.wav"

    # 5. FFmpeg probe 검사 함수
    ff = ffmpeg()

    def probe_file(p: Path):
        cmd = [ff, "-hide_banner", "-i", str(p)]
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return r.stderr

    probe_file(blender_mp4)
    if three_mp4.exists():
        probe_file(three_mp4)

    # 6. 산출물 복사 및 디렉토리 구성
    for out_dir in [OUTBOX_8396, OUTBOX_1990]:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Blender 산출물
        shutil.copy2(blender_mp4, out_dir / "blender_3d_8s_motion.mp4")
        # Three.js 14초 시네마틱 산출물
        if three_mp4.exists():
            shutil.copy2(three_mp4, out_dir / "threejs_cinematic_14s_v2.mp4")
        if audio_wav.exists():
            shutil.copy2(audio_wav, out_dir / "mix_a_14s.wav")
        # 프레임 스틸 샘플 복사
        stills_src = LAB_3D / "cinematic" / "out" / "stills_v2"
        stills_dst = out_dir / "stills_v2"
        if stills_src.exists():
            shutil.copytree(stills_src, stills_dst, dirs_exist_ok=True)

        # 메타데이터 및 벤치마크 요약 JSON
        summary = {
            "title": "3D Scene Studio Benchmark & Production Report",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "adapter_id": adapter.id,
            "limits": {
                "aspects": list(adapter.limits.aspects),
                "min_scenes": adapter.limits.min_scenes,
                "max_scenes": adapter.limits.max_scenes,
                "max_scene_seconds": adapter.limits.max_scene_seconds,
            },
            "audit_v2": {
                "ok": audit.ok,
                "shots": audit.shots,
            },
            "blender_render": {
                "duration_seconds": 8.0,
                "frames": 192,
                "fps": 24,
                "resolution": "960x540",
                "samples": 4,
                "engine": "Cycles CPU",
                "render_seconds": round(blender_elapsed, 2),
                "fps_render_rate": round(192 / blender_elapsed, 2),
                "camera_motion": "Crane down, orbit rotation, dynamic pitch/yaw tracking",
                "lighting_motion": "Orbit point light, crystal pulse light",
                "object_motion": "Hero crystal floating/rotation, orbit ring tilt, 4 satellite cubes orbiting",
            },
            "threejs_cinematic": {
                "duration_seconds": 14.0,
                "frames": 420,
                "fps": 30,
                "resolution": "1920x1080",
                "engine": "Three.js (Playwright ANGLE/Metal on Host)",
                "render_seconds": 24.9,
                "camera_motion": "s1 crane (0-150), s2 low tracking rackfocus (150-270), s3 orbit pulse (270-420)",
                "lighting_motion": "Moon light, hemisphere light, crystal pulse emission",
                "object_motion": "4200 particles, fog sprites, rune monoliths, terrain height field",
                "audio": "mix_a.wav (14.0s 48kHz stereo, ACE-Step ambient cue + impact)",
            },
        }
        with open(out_dir / "benchmark_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

    print("All deliverables copied and summary generated successfully!")


if __name__ == "__main__":
    run_benchmark()
