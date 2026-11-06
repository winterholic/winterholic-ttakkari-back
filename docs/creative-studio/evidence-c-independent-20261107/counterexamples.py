"""Independent fixture checks for Creative Studio C; never starts Blender."""
from __future__ import annotations

import importlib
import json
import math
import runpy
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCENE = importlib.import_module("ttakkari.creative.v2.scene_3d")
RENDERER = runpy.run_path(str(Path("templates/scene_3d/blender_render.py").resolve()))


def rotate_local_minus_z_xyz(euler):
    """Apply Rz(z) * Ry(y) * Rx(x) to local (0, 0, -1), independently."""
    x, y, z = euler
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    # Third column of Rz*Ry*Rx, negated for local -Z.
    return (-cz * sy * cx - sz * sx,
            -sz * sy * cx + cz * sx,
            -cy * cx)


def aim_dot(camera, target, euler):
    forward = rotate_local_minus_z_xyz(euler)
    d = tuple(target[i] - camera[i] for i in range(3))
    norm = math.sqrt(sum(v * v for v in d))
    return sum(forward[i] * d[i] / norm for i in range(3))


def assert_rejected(fn, label):
    try:
        fn()
    except (ValueError, RuntimeError) as exc:
        return f"{label}: rejected ({type(exc).__name__}: {exc})"
    raise AssertionError(f"{label}: unexpectedly accepted")


def valid_evidence(frames):
    return {"frame_count": frames, "transform_frames": [
        {"camera_target_dot": 1.0,
         "hero_rotation_euler_xyz": (0.0, 0.0, i * 0.02),
         "camera_location": (i * 0.02, 0.0, 0.0)} for i in range(frames)]}


def direct_renderer_fixture(evidence_mode="valid", captured=None):
    with tempfile.TemporaryDirectory(prefix="scene3d-c-fixture-") as td:
        out = Path(td)
        def fake_run(cmd, **kwargs):
            if captured is not None:
                captured["cmd"] = cmd
                captured["config"] = json.loads(cmd[cmd.index("--config") + 1])
            (out / "blender_scene_3d.mp4").write_bytes(b"fixture-only")
            if evidence_mode != "missing":
                ev = valid_evidence(72)
                if evidence_mode == "mismatch":
                    ev["frame_count"] = 71
                (out / "motion_evidence.json").write_text(json.dumps(ev))
            return SimpleNamespace(returncode=0, stdout="fixture renderer", stderr="")
        adapter = SCENE.Scene3DAdapter(bpy_python=Path("/fixture/not-run"))
        with patch.object(SCENE, "ffmpeg", return_value="/fixture/ffmpeg"), \
             patch.object(SCENE.subprocess, "run", side_effect=fake_run):
            return adapter.render_blender_video(
                out, frames=72, fps=24, width=960, height=540,
                render_config={"style": {"lighting": "cyberpunk_neon", "camera_motion": "crane_down",
                                          "color_grade": "filmic_warm"},
                               "scene": {"scene_type": "crystal_orbit"}})


def main():
    results = []
    look = RENDERER["_look_at_euler"]
    # Prior formula at the documented first frame, then current formula.
    camera, target = (0.0, -7.5, 4.4), (0.0, 0.0, 1.8)
    dx, dy, dz = (target[i] - camera[i] for i in range(3))
    pitch, yaw = math.atan2(dz, math.hypot(dx, dy)), math.atan2(dy, dx)
    old_euler = (math.pi / 2 - pitch, 0.0, yaw + math.pi / 2)
    old_dot = aim_dot(camera, target, old_euler)
    assert abs(old_dot + 1.0) < 1e-12
    results.append(f"camera legacy first-frame dot={old_dot:.6f} (back-facing)")

    # Exercise four actual camera-motion parameterizations at endpoints and midpoints.
    motion_results = []
    for motion in ("crane_down", "tracking_rackfocus", "orbit_pulse", "handheld_subtle"):
        for u in (0.0, 0.5, 1.0):
            t = 15.0 * u  # 16-second trajectory; geometrical sampling only
            angle = t * (0.12 if motion == "crane_down" else 0.24 if motion == "tracking_rackfocus" else 0.08 if motion == "handheld_subtle" else 0.65) - math.pi / 2
            dist = 8.5 - 1.2 * u if motion == "crane_down" else 6.5 if motion == "tracking_rackfocus" else 7.5 + math.sin(t * 0.5) * 1.8
            z = 5.0 - 2.0 * u if motion == "crane_down" else 2.6 if motion == "tracking_rackfocus" else 3.2 + math.cos(t * 0.4) * 1.2
            if motion == "handheld_subtle":
                z += math.sin(t * 7.0) * 0.035
            cam = (math.cos(angle) * dist, math.sin(angle) * dist, z)
            tar = (0.0, 0.0, 1.8 + math.sin(t * 2.5) * 0.25)
            dot = aim_dot(cam, tar, look(cam, tar))
            assert dot > 0.999999999
            motion_results.append(dot)
    results.append(f"camera corrected Euler/local -Z: {len(motion_results)} trajectory samples, min_dot={min(motion_results):.12f}")
    results.append("screen brightness/framing/naturalness: NOT VERIFIED (geometry only)")

    # Audit failure must stop before the renderer call.
    adapter = SCENE.Scene3DAdapter(bpy_python=Path("/fixture/not-run"))
    with patch.object(adapter, "preflight", return_value=SimpleNamespace(ok=True, missing=[], version="fixture")), \
         patch.object(adapter, "audit_scene", return_value=SCENE.AuditReport(False, "v2", [], "fixture audit fail")), \
         patch.object(adapter, "render_blender_video", side_effect=AssertionError("renderer called")):
        results.append(assert_rejected(lambda: adapter.render_segment({"seconds": 3}, output_path=Path("/tmp/no-write.mp4")), "audit-failure gate"))

    # Missing bpy path fails preflight without importing bpy or launching Blender.
    with tempfile.TemporaryDirectory(prefix="scene3d-no-bpy-") as td:
        adapter = SCENE.Scene3DAdapter(bpy_python=Path(td) / "missing-python", template_dir=Path(td))
        pf = adapter.preflight()
        assert not pf.ok and any("Blender Python 실행 파일을 찾을 수 없습니다" in x for x in pf.missing)
        results.append("missing bpy executable: preflight rejected without bpy import")

    # Unsupported inputs rejected; supported presets reach the actual renderer command JSON.
    for label, spec in [
        ("unsupported scene", {"scenes": [{"scene_type": "city_flythrough"}]}),
        ("unsupported style", {"style": {"camera_motion": "dolly_zoom"}}),
    ]:
        results.append(assert_rejected(lambda s=spec: SCENE._normalize_render_input(s), label))
    captured = {}
    direct_renderer_fixture(captured=captured)
    assert captured["config"]["style"]["lighting"] == "cyberpunk_neon"
    assert captured["config"]["style"]["camera_motion"] == "crane_down"
    results.append("supported scene/style: passed to renderer --config JSON")

    # Oversized direct calls fail before subprocess.
    adapter = SCENE.Scene3DAdapter(bpy_python=Path("/fixture/not-run"))
    for label, kwargs in [
        ("fps", {"frames": 72, "fps": 61}),
        ("resolution", {"frames": 72, "fps": 24, "width": 3841}),
        ("duration", {"frames": 1, "fps": 24}),
        ("frame-count", {"frames": 3601, "fps": 60}),
    ]:
        with patch.object(SCENE.subprocess, "run", side_effect=AssertionError("subprocess called")):
            results.append(assert_rejected(lambda kw=kwargs: adapter.render_blender_video(Path("/tmp/no-write"), **kw), f"direct renderer {label} cap"))

    # Direct renderer must reject missing or inconsistent per-frame evidence.
    for mode, label in (("missing", "missing motion evidence"), ("mismatch", "motion evidence frame-count mismatch")):
        with tempfile.TemporaryDirectory(prefix="scene3d-evidence-") as td:
            out = Path(td)
            def fake_run(cmd, **kwargs):
                (out / "blender_scene_3d.mp4").write_bytes(b"fixture-only")
                if mode != "missing":
                    ev = valid_evidence(72)
                    ev["frame_count"] = 71
                    (out / "motion_evidence.json").write_text(json.dumps(ev))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(SCENE, "ffmpeg", return_value="/fixture/ffmpeg"), patch.object(SCENE.subprocess, "run", side_effect=fake_run):
                results.append(assert_rejected(lambda: adapter.render_blender_video(out, frames=72, fps=24), label))

    # The current public segment path compares the probe's MP4 duration after rendering.
    with tempfile.TemporaryDirectory(prefix="scene3d-duration-") as td:
        root = Path(td)
        out = root / "out.mp4"
        source = root / "source.mp4"
        source.write_bytes(b"fixture-only")
        adapter = SCENE.Scene3DAdapter(bpy_python=Path("/fixture/not-run"))
        def fake_render(work_dir, **kwargs):
            (work_dir / "motion_evidence.json").write_text(json.dumps(valid_evidence(kwargs["frames"])))
            return source
        with patch.object(adapter, "preflight", return_value=SimpleNamespace(ok=True, missing=[], version="fixture")), \
             patch.object(adapter, "audit_scene", return_value=SCENE.AuditReport(True, "v2", [], "fixture audit ok")), \
             patch.object(adapter, "render_blender_video", side_effect=fake_render), \
             patch.object(SCENE, "_probe_duration", return_value=5.0):
            results.append(assert_rejected(lambda: adapter.render_segment({"seconds": 8}, output_path=out, workdir=root / "work"), "actual MP4 duration mismatch"))

    print("COUNTEREXAMPLES=PASS (fixtures only; no Blender process/render)")
    for item in results:
        print("- " + item)


if __name__ == "__main__":
    main()
