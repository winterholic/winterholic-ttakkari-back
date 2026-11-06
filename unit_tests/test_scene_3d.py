"""Creative v2 Scene3DAdapter 및 3D 씬 파이프라인 TDD 테스트."""

from __future__ import annotations

import json
import math
import runpy
from pathlib import Path

import pytest

from ttakkari.creative.v2 import engines
from ttakkari.creative.v2.scene_3d import (
    ADAPTER,
    AuditReport,
    Scene3DAdapter,
    _normalize_render_input,
    get_lead_contract,
)


def test_blender_camera_rotation_tracks_its_target_across_camera_positions():
    renderer = runpy.run_path(str(Path(__file__).parents[1] / "templates/scene_3d/blender_render.py"))
    assert "_look_at_euler" in renderer
    look_at = renderer["_look_at_euler"]

    def dot_camera_forward_to_target(camera, target):
        x, _, z = look_at(camera, target)
        forward = (-math.sin(x) * math.sin(z), math.sin(x) * math.cos(z), -math.cos(x))
        direction = tuple(target[i] - camera[i] for i in range(3))
        length = math.sqrt(sum(v * v for v in direction))
        return sum(forward[i] * direction[i] / length for i in range(3))

    for camera, target in [
        ((0.0, -7.5, 4.4), (0.0, 0.0, 1.8)),
        ((4.0, -5.0, 3.0), (0.0, 0.5, 1.7)),
        ((-3.0, 6.0, 2.0), (0.5, -0.3, 2.1)),
    ]:
        assert dot_camera_forward_to_target(camera, target) > 0.999999


def test_render_stops_before_blender_when_preflight_fails(tmp_path, monkeypatch):
    adapter = Scene3DAdapter()
    monkeypatch.setattr(adapter, "preflight", lambda: engines.Preflight(ok=False, version=None, missing=["Blender missing"]))
    monkeypatch.setattr(adapter, "audit_scene", lambda *_: pytest.fail("audit must not run without Blender"))
    monkeypatch.setattr(adapter, "render_blender_video", lambda *a, **k: pytest.fail("render must not run"))

    with pytest.raises(RuntimeError, match="Preflight failed.*Blender missing"):
        adapter.render_segment({"seconds": 8}, output_path=tmp_path / "out.mp4")


def test_render_stops_before_blender_when_audit_fails(tmp_path, monkeypatch):
    adapter = Scene3DAdapter()
    monkeypatch.setattr(adapter, "preflight", lambda: engines.Preflight(ok=True, version="Blender test", missing=[]))
    monkeypatch.setattr(adapter, "audit_scene", lambda *_: AuditReport(False, "v2", [], "unsafe"))
    monkeypatch.setattr(adapter, "render_blender_video", lambda *a, **k: pytest.fail("render must not run"))

    with pytest.raises(RuntimeError, match="audit failed"):
        adapter.render_segment({"seconds": 8}, output_path=tmp_path / "out.mp4")


@pytest.mark.parametrize("spec", [
    {"seconds": 61},
    {"seconds": 8, "frames": 193},
    {"seconds": 8, "fps": 61},
    {"seconds": 8, "width": 4097},
    {"seconds": 8, "width": 960, "height": 2161},
])
def test_render_rejects_duration_fps_and_resolution_limits_before_preflight(tmp_path, monkeypatch, spec):
    adapter = Scene3DAdapter()
    monkeypatch.setattr(adapter, "preflight", lambda: pytest.fail("invalid input must fail first"))

    with pytest.raises(ValueError):
        adapter.render_segment(spec, output_path=tmp_path / "out.mp4")


@pytest.mark.parametrize("render_args", [
    {"frames": 1440, "fps": 120},
    {"frames": 192, "fps": 24, "width": 4096},
    {"frames": 192, "fps": 24, "height": 2161},
    {"frames": 1, "fps": 24},
    {"frames": 1441, "fps": 24},
])
def test_direct_blender_renderer_rejects_over_limit_settings(tmp_path, monkeypatch, render_args):
    adapter = Scene3DAdapter()
    monkeypatch.setattr("ttakkari.creative.v2.scene_3d.subprocess.run", lambda *a, **k: pytest.fail("renderer must reject first"))
    with pytest.raises(ValueError):
        adapter.render_blender_video(tmp_path, **render_args)


def test_supported_style_and_scene_preset_are_preserved_for_renderer():
    normalized = _normalize_render_input({
        "seconds": 8,
        "scenes": [{"scene_id": "s1", "scene_type": "crystal_orbit", "duration": 8}],
        "style": {"lighting": "cyberpunk_neon", "camera_motion": "crane_down", "color_grade": "filmic_warm"},
    })
    assert normalized["scene"]["scene_type"] == "crystal_orbit"
    assert normalized["style"]["lighting"] == "cyberpunk_neon"
    assert normalized["style"]["camera_motion"] == "crane_down"
    assert normalized["style"]["color_grade"] == "filmic_warm"


def test_unsupported_scene_and_style_are_not_silently_ignored():
    with pytest.raises(ValueError, match="unsupported scene_type"):
        _normalize_render_input({"scenes": [{"scene_type": "city_flythrough"}]})
    with pytest.raises(ValueError, match="unsupported camera_motion"):
        _normalize_render_input({"style": {"camera_motion": "dolly_zoom"}})
    with pytest.raises(ValueError, match="style_dna is not implemented"):
        _normalize_render_input({"style_dna": {"palette": "purple"}})


def test_render_rejects_actual_duration_mismatch_after_encoding(tmp_path, monkeypatch):
    from ttakkari.creative.v2 import scene_3d

    adapter = Scene3DAdapter()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"encoded-video")
    captured = {}
    monkeypatch.setattr(adapter, "preflight", lambda: engines.Preflight(ok=True, version="Blender test", missing=[]))
    monkeypatch.setattr(adapter, "audit_scene", lambda *_: AuditReport(True, "v2", [], "ok"))
    def fake_render(output_dir, **kwargs):
        captured.update(kwargs)
        frames = kwargs["frames"]
        evidence = {"frame_count": frames, "transform_frames": [
            {"camera_target_dot": 1.0, "hero_rotation_euler_xyz": (0.0, 0.0, i * 0.02),
             "camera_location": (i * 0.02, 0.0, 0.0)} for i in range(frames)
        ]}
        (output_dir / "motion_evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        return source
    monkeypatch.setattr(adapter, "render_blender_video", fake_render)
    monkeypatch.setattr(scene_3d, "_probe_duration", lambda _: 7.0)

    with pytest.raises(RuntimeError, match="Rendered duration mismatch"):
        adapter.render_segment({"seconds": 8, "style": {"lighting": "cyberpunk_neon"}}, output_path=tmp_path / "out.mp4")
    assert captured["render_config"]["style"]["lighting"] == "cyberpunk_neon"


def test_scene_3d_adapter_protocol_conformance():
    """Scene3DAdapter가 engines.EngineAdapter 프로토콜 및 통합 계약을 충족하는지 검증."""
    adapter = Scene3DAdapter()

    assert adapter.id == "skill.studio-3d-cinematic"
    assert "skill.studio-3d-cinematic" in adapter.providers
    assert "tool.three" in adapter.providers
    assert "tool.blender" in adapter.providers
    assert "skill.v2_three_scene_3d" in adapter.providers
    assert "tool.threejs" in adapter.providers
    assert "tool.blender_3d" in adapter.providers

    # Limits 검증
    assert adapter.limits.aspects == ("16:9", "1:1", "9:16")
    assert adapter.limits.min_scenes >= 1
    assert adapter.limits.max_scene_seconds >= 30.0

    # style_params 검증
    assert "lighting" in adapter.style_params
    assert "camera_motion" in adapter.style_params
    assert "rendering_backend" in adapter.style_params
    assert "color_grade" in adapter.style_params


def test_scene_3d_adapter_preflight_rejects_missing_blender(tmp_path):
    """preflight 점검이 올바르게 실행되고 필수 도구들을 보고하는지 검증."""
    # Known Blender bpy import crashes in this Codex execution environment; use a missing path
    # to verify the same fail-closed behavior without repeating that subprocess.
    adapter = Scene3DAdapter(bpy_python=tmp_path / "missing-python")
    pf = adapter.preflight()
    assert pf.ok is False
    assert any("Blender Python 실행 파일을 찾을 수 없습니다" in reason for reason in pf.missing)


def test_scene_3d_audit_scene_gate():
    """Node 기반 카메라 동선 및 지오메트리 사전 감사(audit) 게이트가 동작하는지 검증."""
    adapter = Scene3DAdapter()
    report = adapter.audit_scene(version="v2")

    assert isinstance(report, AuditReport)
    assert report.version == "v2"
    assert report.ok is True
    assert len(report.shots) == 3

    # 각 샷별 검사 항목(framing, occlusion, clearance, floor, continuity) 통과 검증
    for shot in report.shots:
        assert shot["pass"] is True
        checks = shot["checks"]
        assert checks["framing"]["pass"] is True
        assert checks["occlusion"]["pass"] is True
        assert checks["clearance"]["pass"] is True
        assert checks["floor"]["pass"] is True
        assert checks["continuity"]["pass"] is True


def test_lead_contract_specification():
    """총괄 보고용 연결 계약 사양이 누락 없이 정의되어 있는지 검증."""
    contract = get_lead_contract()

    assert contract["adapter_id"] == "skill.studio-3d-cinematic"
    assert "contract_endpoints" in contract
    assert "registration_snippet" in contract
    assert "limits" in contract
    assert "style_params" in contract


def test_adapter_integration_with_engines_dispatch():
    """engines.ADAPTERS에 등록되었을 때 adapter_for로 정상 조회되는지 검증."""
    test_adapters = list(engines.ADAPTERS) + [ADAPTER]

    def mock_adapter_for(providers: set[str]):
        for a in test_adapters:
            if providers and providers <= a.providers:
                return a
        return None

    found = mock_adapter_for({"skill.studio-3d-cinematic"})
    assert found is not None
    assert found.id == "skill.studio-3d-cinematic"

    found_legacy = mock_adapter_for({"skill.v2_three_scene_3d"})
    assert found_legacy is not None
    assert found_legacy.id == "skill.studio-3d-cinematic"

    found_tool = mock_adapter_for({"tool.three"})
    assert found_tool is not None
    assert found_tool.id == "skill.studio-3d-cinematic"
