"""Creative v2 Scene3DAdapter 및 3D 씬 파이프라인 TDD 테스트."""

from __future__ import annotations

from ttakkari.creative.v2 import engines
from ttakkari.creative.v2.scene_3d import (
    ADAPTER,
    AuditReport,
    Scene3DAdapter,
    get_lead_contract,
    preflight,
)


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


def test_scene_3d_adapter_preflight():
    """preflight 점검이 올바르게 실행되고 필수 도구들을 보고하는지 검증."""
    pf = preflight()

    assert pf.ok is True
    assert pf.version is not None
    assert "Node" in pf.version or "ffmpeg" in pf.version.lower()
    assert len(pf.missing) == 0


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
