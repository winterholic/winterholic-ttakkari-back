import os
from pathlib import Path
import pytest

from ttakkari.creative.v2.engines import EngineAdapter, Limits, Preflight, adapter_by_id
from ttakkari.creative.v2.hyperframes import (
    HyperFramesAdapter,
    HyperFramesRenderOptions,
    find_browser,
    find_ffprobe,
    run_lint,
)


def test_hyperframes_adapter_protocol_conformance():
    adapter = HyperFramesAdapter()
    assert adapter.id == "skill.hyperframes"
    assert "skill.hyperframes" in adapter.providers
    assert "tool.hyperframes" in adapter.providers
    assert isinstance(adapter.limits, Limits)
    assert adapter.limits.min_scenes == adapter.limits.max_scenes == 3
    assert adapter.limits.aspects == ("16:9",)
    assert adapter.style_params == {
        "theme": ("dark",),
        "animation": ("technical",),
        "transition": ("cut",),
        "caption_style": ("bottom_card",),
    }
    assert callable(adapter.preflight)

    discovered = adapter_by_id(adapter.id)
    assert discovered is not None
    assert discovered.limits == adapter.limits
    assert discovered.style_params == adapter.style_params


def test_hyperframes_preflight_live():
    adapter = HyperFramesAdapter()
    pre = adapter.preflight()
    assert isinstance(pre, Preflight)
    if any("browser IPC" in item for item in pre.missing):
        assert pre.ok is False
        assert any("CLI doctor/render not attempted" in item for item in pre.missing)
        return
    assert pre.ok is True
    assert pre.version is not None
    assert "hyperframes" in pre.version
    assert len(pre.missing) == 0


def test_binary_finders():
    browser = find_browser()
    assert browser is not None
    assert Path(browser).exists()

    probe = find_ffprobe()
    assert probe is not None
    assert Path(probe).exists()


def test_hyperframes_lint_on_fixture(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    vendor = project_dir / "vendor"
    vendor.mkdir()

    # copy gsap
    gsap_source = Path("/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107/benchmark_tests/test_hf_minimal/vendor/gsap.min.js")
    if gsap_source.is_file():
        (vendor / "gsap.min.js").write_bytes(gsap_source.read_bytes())

    index_html = project_dir / "index.html"
    index_html.write_text("""<!doctype html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <script src="vendor/gsap.min.js"></script>
  <style>
    @font-face { font-family: "Pretendard"; src: local("Apple SD Gothic Neo"); }
    body { font-family: "Pretendard", sans-serif; }
    #root { width: 1920px; height: 1080px; }
  </style>
</head>
<body>
  <div id="root" data-composition-id="main" data-width="1920" data-height="1080" data-duration="2">
    <div id="c1" class="clip" data-start="0" data-duration="2" data-track-index="0"></div>
  </div>
  <script>
    window.__timelines = window.__timelines || {};
    window.__timelines["main"] = gsap.timeline({ paused: true });
  </script>
</body>
</html>""", encoding="utf-8")

    lint_result = run_lint(project_dir)
    assert lint_result.get("ok") is True
    assert lint_result.get("errorCount") == 0


def test_render_segment_applies_spec_content_and_reports_real_render(monkeypatch, tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    spec = {
        "plan_id": "plan-test",
        "segment_id": "seg-kafka",
        "seconds": 18,
        "width": 1280,
        "height": 720,
        "fps": 24,
        "title": "Kafka delivery, end to end",
        "scenes": [
            {"title": "Producer · 프로듀서", "narration": "Key 해시로 파티션을 선택합니다."},
            {"title": "Broker · 브로커", "narration": "레코드를 커밋 로그에 기록합니다."},
            {"title": "Consumer · 컨슈머", "narration": "오프셋을 커밋하고 다음 레코드를 읽습니다."},
        ],
    }
    monkeypatch.setattr(hyperframes.ADAPTER, "preflight", lambda: Preflight(True, "hyperframes test", []))
    monkeypatch.setattr(hyperframes, "run_lint", lambda project: {"ok": True, "errorCount": 0})

    def fake_render(project_dir, output_mp4, options):
        assert options.quality == "delivery"
        rendered_html = (project_dir / "index.html").read_text(encoding="utf-8")
        assert "Kafka delivery, end to end" in rendered_html
        assert "Key 해시로 파티션을 선택합니다." in rendered_html
        assert "data-width=\"1280\"" in rendered_html
        assert "data-duration=\"18\"" in rendered_html
        return {
            "success": True,
            "returncode": 0,
            "output_file": str(output_mp4),
            "size_bytes": 123,
            "elapsed_seconds": 2.5,
            "stdout": "render complete",
            "stderr": "",
        }

    monkeypatch.setattr(hyperframes, "render_project", fake_render)
    result = hyperframes.render(
        spec,
        output_path=tmp_path / "out.mp4",
        workdir=tmp_path / "work",
    )

    assert result["success"] is True
    assert result["real_execution"] is True
    assert result["receipt"]["commands"][-1][0:3] == ["npx", "-y", "hyperframes"]


def test_render_segment_maps_core_style_dna_to_fixed_template_values(monkeypatch, tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    spec = {
        "plan_id": "plan-core-contract",
        "revision": 2,
        "segment_id": "seg-01",
        "seconds": 16,
        "aspect": "16:9",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "style_dna": {
            "status": "resolved",
            "sha256": "a" * 64,
            "dna": {
                "visual_identity": {
                    "text": "dark technical card style",
                    "params": {"theme": "dark", "caption_style": "bottom_card"},
                },
                "motion_language": {
                    "text": "precise technical motion",
                    "params": {"animation": "technical", "transition": "cut"},
                },
                "palette": [{"hex": "#000000", "role": "background"}],
            },
        },
        "scenes": [
            {"scene_id": "producer", "title": "Producer", "narration": "Publishes an event.", "seconds": 5.1, "camera": [], "motion": []},
            {"scene_id": "broker", "title": "Broker", "narration": "Appends a record.", "seconds": 5.4, "camera": [], "motion": []},
            {"scene_id": "consumer", "title": "Consumer", "narration": "Commits an offset.", "seconds": 5.5, "camera": [], "motion": []},
        ],
        "editorial_scenes": [{"scene_id": "producer", "title": "Producer"}],
    }
    monkeypatch.setattr(hyperframes.ADAPTER, "preflight", lambda: Preflight(True, "test", []))
    monkeypatch.setattr(hyperframes, "run_lint", lambda _project: {"ok": True, "errorCount": 0})
    monkeypatch.setattr(
        hyperframes,
        "render_project",
        lambda *_args, **_kwargs: {
            "success": True,
            "returncode": 0,
            "output_file": str(tmp_path / "out.mp4"),
            "elapsed_seconds": 1.0,
        },
    )

    result = hyperframes.render(spec, output_path=tmp_path / "out.mp4", workdir=tmp_path / "work")

    assert result["success"] is True
    assert result["receipt"]["style_dna_contract"] == {
        "status": "partial_match",
        "sha256": "a" * 64,
        "matched_params": {
            "theme": "dark",
            "animation": "technical",
            "transition": "cut",
            "caption_style": "bottom_card",
        },
        "unapplied_fields": [
            {"path": "motion_language.text", "value": "precise technical motion"},
            {"path": "palette", "value": [{"hex": "#000000", "role": "background"}]},
            {"path": "visual_identity.text", "value": "dark technical card style"},
        ],
    }


def test_hyperframes_style_dna_prevalidation_names_unsupported_core_value():
    style_dna = {
        "status": "resolved",
        "sha256": "b" * 64,
        "dna": {"motion_language": {"text": "quick pop", "params": {"animation": "pop"}}},
    }

    with pytest.raises(ValueError, match=r"motion_language\.params\.animation=.*pop.*technical"):
        HyperFramesAdapter().validate_style_dna(style_dna)


def test_render_segment_rejects_unsupported_core_style_dna_before_preflight(monkeypatch, tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    monkeypatch.setattr(
        hyperframes.ADAPTER,
        "preflight",
        lambda: pytest.fail("unsupported Style DNA must fail before browser preflight"),
    )
    spec = {
        "plan_id": "plan-core-contract",
        "revision": 2,
        "segment_id": "seg-01",
        "seconds": 16,
        "aspect": "16:9",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "style_dna": {
            "status": "resolved",
            "sha256": "c" * 64,
            "dna": {"motion_language": {"text": "quick pop", "params": {"animation": "pop"}}},
        },
        "scenes": [
            {"scene_id": "producer", "title": "Producer", "narration": "Publishes an event.", "seconds": 5.1, "camera": [], "motion": []},
            {"scene_id": "broker", "title": "Broker", "narration": "Appends a record.", "seconds": 5.4, "camera": [], "motion": []},
            {"scene_id": "consumer", "title": "Consumer", "narration": "Commits an offset.", "seconds": 5.5, "camera": [], "motion": []},
        ],
        "editorial_scenes": [{"scene_id": "producer", "title": "Producer"}],
    }

    result = hyperframes.render(spec, output_path=tmp_path / "out.mp4", workdir=tmp_path / "work")

    assert result["success"] is False
    assert result["stage"] == "prepare"
    assert "motion_language.params.animation" in result["error"]


def test_render_segment_returns_preflight_failure_without_render_fallback(monkeypatch, tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    spec = {
        "seconds": 16,
        "scenes": [
            {"title": "Producer", "narration": "Publishes an event."},
            {"title": "Broker", "narration": "Appends a record."},
            {"title": "Consumer", "narration": "Commits an offset."},
        ],
    }
    monkeypatch.setattr(
        hyperframes.ADAPTER,
        "preflight",
        lambda: Preflight(False, None, ["Chrome unavailable in this execution environment"]),
    )
    monkeypatch.setattr(
        hyperframes,
        "render_project",
        lambda *_args, **_kwargs: pytest.fail("render must not run after failed preflight"),
    )

    result = hyperframes.render(spec, output_path=tmp_path / "out.mp4", workdir=tmp_path / "work")

    assert result["success"] is False
    assert result["stage"] == "preflight"
    assert "Chrome unavailable" in result["error"]


def test_render_segment_rejects_unmapped_scene_count(monkeypatch, tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    result = hyperframes.render(
        {"seconds": 16, "scenes": [{"title": "Only one", "narration": "No silent fallback."}]},
        output_path=tmp_path / "out.mp4",
        workdir=tmp_path / "work",
    )

    assert result["success"] is False
    assert result["stage"] == "prepare"
    assert "3개 scenes" in result["error"]


@pytest.mark.parametrize("scene_count", [1, 4])
def test_render_segment_rejects_scene_counts_outside_fixed_template(scene_count, tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    scenes = [{"title": f"Scene {i}", "narration": f"Narration {i}"} for i in range(scene_count)]
    result = hyperframes.render(
        {"seconds": 16, "scenes": scenes},
        output_path=tmp_path / "out.mp4",
        workdir=tmp_path / "work",
    )

    assert result["success"] is False
    assert result["stage"] == "prepare"
    assert "3개 scenes" in result["error"]


def test_render_segment_rejects_portrait_before_preflight(monkeypatch, tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    monkeypatch.setattr(
        hyperframes.ADAPTER,
        "preflight",
        lambda: pytest.fail("unsupported aspect must fail before browser preflight"),
    )
    result = hyperframes.render(
        {
            "seconds": 16,
            "width": 1080,
            "height": 1920,
            "scenes": [
                {"title": "Producer", "narration": "Publishes an event."},
                {"title": "Broker", "narration": "Appends a record."},
                {"title": "Consumer", "narration": "Commits an offset."},
            ],
        },
        output_path=tmp_path / "out.mp4",
        workdir=tmp_path / "work",
    )

    assert result["success"] is False
    assert result["stage"] == "prepare"
    assert "16:9 출력만 지원" in result["error"]


@pytest.mark.parametrize(
    "style",
    [
        {"theme": "light"},
        {"animation": "pop"},
        {"transition": "fade"},
        {"caption_style": "title_card"},
    ],
)
def test_render_segment_rejects_discovery_only_style_values_before_preflight(style, monkeypatch, tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    monkeypatch.setattr(
        hyperframes.ADAPTER,
        "preflight",
        lambda: pytest.fail("unsupported style must fail before browser preflight"),
    )
    result = hyperframes.render(
        {
            "seconds": 16,
            "style": style,
            "scenes": [
                {"title": "Producer", "narration": "Publishes an event."},
                {"title": "Broker", "narration": "Appends a record."},
                {"title": "Consumer", "narration": "Commits an offset."},
            ],
        },
        output_path=tmp_path / "out.mp4",
        workdir=tmp_path / "work",
    )

    assert result["success"] is False
    assert result["stage"] == "prepare"
    assert next(iter(style)) in result["error"]


def test_render_segment_rejects_style_fields_the_template_does_not_apply(tmp_path):
    import ttakkari.creative.v2.hyperframes as hyperframes

    result = hyperframes.render(
        {
            "seconds": 16,
            "style": {"theme": "light"},
            "scenes": [
                {"title": "Producer", "narration": "Publishes an event."},
                {"title": "Broker", "narration": "Appends a record."},
                {"title": "Consumer", "narration": "Commits an offset."},
            ],
        },
        output_path=tmp_path / "out.mp4",
        workdir=tmp_path / "work",
    )

    assert result["success"] is False
    assert result["stage"] == "prepare"
    assert "theme=light" in result["error"]


def test_preflight_blocks_cli_doctor_when_browser_ipc_is_denied(monkeypatch):
    import subprocess
    import ttakkari.creative.v2.hyperframes as hyperframes

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(
        hyperframes,
        "inspect_permissions",
        lambda: {"status": "blocked", "browser_launched": False, "checks": []},
        raising=False,
    )
    monkeypatch.setattr(hyperframes.subprocess, "run", fake_run)

    result = HyperFramesAdapter().preflight()

    assert result.ok is False
    assert any("browser IPC blocked" in item for item in result.missing)
    assert not any(command and command[0] == "npx" for command in calls)
