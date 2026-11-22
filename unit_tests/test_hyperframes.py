import json
import re
import subprocess
from pathlib import Path

import pytest

from ttakkari.creative.v2.engines import Limits, Preflight, adapter_by_id
from ttakkari.creative.v2.hyperframes import (
    HyperFramesAdapter,
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
    assert (adapter.limits.min_scenes, adapter.limits.max_scenes) == (3, 5)
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
    gsap_source = Path(__file__).parents[1] / "templates" / "hyperframes" / "kafka_message_flow" / "vendor" / "gsap.min.js"
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
    from ttakkari.creative.v2 import hyperframes

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
        # Template layout is fixed at 1920x1080; HyperFrames would crop or double-shrink any other size.
        assert 'data-width="1920"' in rendered_html and 'data-height="1080"' in rendered_html
        assert "style.transform" not in rendered_html
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
    assert (result["width"], result["height"]) == (1920, 1080)
    assert (result["requested_width"], result["requested_height"]) == (1280, 720)
    assert result["real_execution"] is True
    assert result["receipt"]["commands"][-1][0:3] == ["npx", "-y", "hyperframes"]


def test_render_segment_maps_core_style_dna_to_fixed_template_values(monkeypatch, tmp_path):
    from ttakkari.creative.v2 import hyperframes

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


def test_kafka_project_uses_reviewed_scene_durations_on_its_render_timeline(tmp_path):
    from ttakkari.creative.v2.hyperframes import _prepare_spec_project

    project = _prepare_spec_project({
        "seconds": 18,
        "scenes": [
            {"title": title, "narration": "데이터가 이동합니다.", "seconds": seconds}
            for title, seconds in (("Producer", 3), ("Broker", 8), ("Consumer", 7))
        ],
    }, tmp_path)
    output = (project / "index.html").read_text()
    assert '"scene_seconds": [3.0, 8.0, 7.0]' in output
    assert 'window.__timelines["main"] = renderTimeline;' in output
    assert "tl.timeScale" not in output


def test_kafka_project_keeps_template_title_without_explicit_title(tmp_path):
    from ttakkari.creative.v2.hyperframes import _prepare_spec_project

    project = _prepare_spec_project({
        "seconds": 16,
        "scenes": [{"title": t, "narration": "흐름"} for t in ("Producer", "Broker", "Consumer")],
    }, tmp_path)
    assert '"title": ""' in (project / "index.html").read_text()


def test_kafka_default_caption_mode_keeps_titles_without_spoken_narration(tmp_path):
    from ttakkari.creative.v2.hyperframes import _prepare_spec_project

    project = _prepare_spec_project({
        "seconds": 16,
        "scenes": [{"title": title, "narration": ""} for title in ("발행", "로그 기록", "소비")],
    }, tmp_path)
    html = (project / "index.html").read_text()
    for title in ("발행", "로그 기록", "소비"):
        assert f'{{"title": "{title}", "narration": ""}}' in html


def test_render_segment_rejects_unsupported_core_style_dna_before_preflight(monkeypatch, tmp_path):
    from ttakkari.creative.v2 import hyperframes

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
    from ttakkari.creative.v2 import hyperframes

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
    from ttakkari.creative.v2 import hyperframes

    result = hyperframes.render(
        {"seconds": 16, "scenes": [{"title": "Only one", "narration": "No silent fallback."}]},
        output_path=tmp_path / "out.mp4",
        workdir=tmp_path / "work",
    )

    assert result["success"] is False
    assert result["stage"] == "prepare"
    assert "3~5개 scenes" in result["error"]


@pytest.mark.parametrize("scene_count", [1, 2, 6])
def test_render_segment_rejects_scene_counts_outside_fixed_template(scene_count, tmp_path):
    from ttakkari.creative.v2 import hyperframes

    scenes = [{"title": f"Scene {i}", "narration": f"Narration {i}"} for i in range(scene_count)]
    result = hyperframes.render(
        {"seconds": 16, "scenes": scenes},
        output_path=tmp_path / "out.mp4",
        workdir=tmp_path / "work",
    )

    assert result["success"] is False
    assert result["stage"] == "prepare"
    assert "3~5개 scenes" in result["error"]


def test_render_segment_rejects_portrait_before_preflight(monkeypatch, tmp_path):
    from ttakkari.creative.v2 import hyperframes

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
    from ttakkari.creative.v2 import hyperframes

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
    from ttakkari.creative.v2 import hyperframes

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

    from ttakkari.creative.v2 import hyperframes

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


_PACKET_PROBE = """
<script>
(() => {
  const rect = (selector) => {
    const r = document.querySelector(selector).getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
  };
  const out = {};
  for (const t of %s) {
    window.__timelines.main.seek(t, false);
    out[t] = {
      packet: rect("#packet"),
      newRecord: rect("#block-p1-new"),
      rowP0: rect(".partition-row:nth-child(1)"),
      rowP1: rect("#row-p1"),
      itemC1: rect(".consumer-item:nth-child(1)"),
      nameC1: rect(".consumer-item:nth-child(1) .consumer-name"),
      itemC2: rect("#item-c2"),
      newRecordP0: rect("#block-p0-new"),
      rowP0Id: rect("#row-p0"),
      itemC1Id: rect("#item-c1"),
    };
  }
  document.body.insertAdjacentHTML("beforeend", "<pre id=probe>" + JSON.stringify(out) + "</pre>");
})();
</script>
"""


def _probe_kafka_packet(project: Path, times: list[float]) -> dict[str, dict[str, dict[str, float]]]:
    browser = find_browser()
    if browser is None:
        pytest.skip("headless browser not available")
    html_path = project / "index.html"
    html = html_path.read_text(encoding="utf-8")
    html_path.write_text(html.replace("</body>", (_PACKET_PROBE % json.dumps(times)) + "</body>"), encoding="utf-8")
    dom = subprocess.run(
        [browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1920,1080",
         "--virtual-time-budget=3000", "--dump-dom", html_path.as_uri()],
        capture_output=True, text=True, timeout=60, check=False,
    ).stdout
    match = re.search(r'<pre id="probe">(.*?)</pre>', dom, re.DOTALL)
    assert match, "packet probe did not run in the browser"
    return {float(t): v for t, v in json.loads(match.group(1)).items()}


def _center(rect: dict[str, float]) -> tuple[float, float]:
    return (rect["left"] + rect["right"]) / 2, (rect["top"] + rect["bottom"]) / 2


def _inside(point: tuple[float, float], rect: dict[str, float]) -> bool:
    return rect["left"] <= point[0] <= rect["right"] and rect["top"] <= point[1] <= rect["bottom"]


def _overlaps(a: dict[str, float], b: dict[str, float]) -> bool:
    return a["left"] < b["right"] and a["right"] > b["left"] and a["top"] < b["bottom"] and a["bottom"] > b["top"]


def test_kafka_packet_follows_partition_1_and_consumer_2(tmp_path):
    from ttakkari.creative.v2.hyperframes import _prepare_spec_project

    project = _prepare_spec_project({
        "seconds": 16,
        "title": "분산 메시지 흐름 및 커밋 로그 파이프라인",
        "caption_mode": "title_only",
        "scenes": [{"title": t, "narration": ""} for t in ("발행 · Publish", "로그 기록 · Append to Log", "소비 · Consume")],
    }, tmp_path)
    publish_times = [3.5, 3.9, 4.2, 4.5, 4.8, 5.0]
    consume_times = [11.5, 11.8, 12.0, 12.3, 12.5]
    frames = _probe_kafka_packet(project, publish_times + consume_times)

    # 발행 단계: Producer를 떠난 점은 어느 시점에도 Partition 0 행을 가로지르지 않고 새 레코드 칸에 도착한다.
    for t in publish_times:
        assert not _overlaps(frames[t]["packet"], frames[t]["rowP0"]), f"packet crosses Partition 0 at {t}s"
    assert _inside(_center(frames[4.5]["packet"]), frames[4.5]["newRecord"])
    assert _inside(_center(frames[4.5]["packet"]), frames[4.5]["rowP1"])

    # 소비 단계: 점은 하이라이트되는 Consumer 2 카드 가장자리에 머물고 Consumer 1 카드와 글자를 가리지 않는다.
    for t in consume_times:
        assert not _overlaps(frames[t]["packet"], frames[t]["itemC1"]), f"packet touches Consumer 1 at {t}s"
        assert not _overlaps(frames[t]["packet"], frames[t]["nameC1"])
    final = frames[12.0]
    cx, cy = _center(final["packet"])
    assert final["itemC2"]["top"] <= cy <= final["itemC2"]["bottom"]
    assert abs(cx - final["itemC2"]["left"]) <= 16


def test_kafka_packet_targets_are_computed_not_hardcoded_pixels():
    html = (Path(__file__).parents[1] / "templates" / "hyperframes" / "kafka_message_flow" / "index.html").read_text(encoding="utf-8")
    script = html[html.index("<script>\n    window.__timelines"):]
    packet_tweens = re.findall(r'tl\.(?:fromTo|to)\("#packet"[^;]*;', script)
    assert len(packet_tweens) >= 3
    for tween in packet_tweens:
        assert not re.search(r"\b[xy]:\s*-?\d", tween), f"hardcoded packet pixel in: {tween}"
    # 점이 향하는 요소는 하이라이트되는 요소와 같다.
    for selector in ('#block-p1-new', '#item-c2', '#row-p1'):
        assert f'"{selector}"' in script


FIVE_ACT_SCENES = [
    {"title": t, "narration": "", "seconds": sec}
    for t, sec in (("발행", 4.5), ("기록", 6), ("소비", 5.5), ("병렬 파티션", 7), ("오프셋 커밋", 7))
]


def test_kafka_project_takes_scene_count_and_seconds_from_the_plan(tmp_path):
    from ttakkari.creative.v2.hyperframes import _audio_seconds, _prepare_spec_project

    project = _prepare_spec_project({"seconds": 30, "scenes": FIVE_ACT_SCENES, "caption_mode": "title_only"}, tmp_path)
    html = (project / "index.html").read_text()
    assert '"scene_seconds": [4.5, 6.0, 5.5, 7.0, 7.0]' in html
    assert "const nativeCuts = [0.0, 4.5, 10.5, 16.0, 23.0, 30.0];" in html
    assert 'data-duration="30"' in html and '<audio id="bgm" src="audio.wav" data-start="0" data-duration="30">' in html
    # 템플릿 음악은 16초다. 계획 길이만큼 채워 뒤쪽이 무음이 되지 않게 한다.
    assert _audio_seconds(project / "audio.wav") == pytest.approx(30, abs=0.1)


def test_kafka_project_scales_native_acts_when_only_total_seconds_is_given(tmp_path):
    from ttakkari.creative.v2.hyperframes import _prepare_spec_project

    project = _prepare_spec_project({
        "seconds": 15,
        "scenes": [{"title": t} for t in ("a", "b", "c", "d", "e")],
    }, tmp_path)
    assert '"scene_seconds": [2.25, 3.0, 2.75, 3.5, 3.5]' in (project / "index.html").read_text()


def test_kafka_second_record_reaches_partition_0_and_consumer_1(tmp_path):
    from ttakkari.creative.v2.hyperframes import _prepare_spec_project

    project = _prepare_spec_project({"seconds": 30, "scenes": FIVE_ACT_SCENES, "caption_mode": "title_only"}, tmp_path)
    # 장면 길이가 원본 막 길이와 같아 렌더 시각이 곧 원본 시각이다.
    entry_times, block_time, consumer_time = [17.2, 17.6, 17.9, 18.2], 19.0, 21.4
    frames = _probe_kafka_packet(project, [*entry_times, block_time, consumer_time])

    for t in entry_times:
        assert not _overlaps(frames[t]["packet"], frames[t]["rowP1"]), f"packet crosses Partition 1 at {t}s"
    assert _inside(_center(frames[block_time]["packet"]), frames[block_time]["newRecordP0"])
    assert _inside(_center(frames[block_time]["packet"]), frames[block_time]["rowP0Id"])
    at_consumer = frames[consumer_time]
    _, cy = _center(at_consumer["packet"])
    assert at_consumer["itemC1Id"]["top"] <= cy <= at_consumer["itemC1Id"]["bottom"]
    assert not _overlaps(at_consumer["packet"], at_consumer["itemC2"])
