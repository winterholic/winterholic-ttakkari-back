"""실제 Remotion·Three.js 렌더 시험(CPU + Chrome만 사용, GPU 모델 없음).

Chrome을 띄우고 GB급 메모리를 쓰므로 기본 실행에서는 건너뛴다.
실행:  TTAKKARI_REAL_ENGINE_TESTS=1 PYTHONPATH=src python -m pytest unit_tests/test_creative_real_engines.py
같은 시나리오를 더 길게 보여 주는 재현 스크립트는 scripts/repro/creative_engines_smoke.py다.
"""

from __future__ import annotations

import os

import pytest
from creative_support import make_pipeline, new_production

from ttakkari.creative.engines.ffmpeg_engine import FfmpegAdapter
from ttakkari.creative.engines.remotion_engine import RemotionAdapter
from ttakkari.creative.engines.three_engine import ThreeAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("TTAKKARI_REAL_ENGINE_TESTS") != "1",
    reason="실제 렌더 시험은 TTAKKARI_REAL_ENGINE_TESTS=1일 때만 실행한다",
)


def _real_pipeline(tmp_path):
    adapters = [FfmpegAdapter(), RemotionAdapter(), ThreeAdapter()]
    pipeline, store, registry = make_pipeline(
        tmp_path, *adapters, allow_mock=False, measure=False
    )
    for adapter in adapters:
        registry.run_smoke(adapter.engine_id)
        registry.set_policy(
            adapter.engine_id, "auto_eligible", "실제 smoke 통과 후 시험 승격"
        )
    return pipeline, store, registry


def test_remotion_and_three_shots_render_through_g0_attempts_with_real_receipts(
    tmp_path,
):
    pipeline, store, registry = _real_pipeline(tmp_path)
    assert {c["engine"] for c in registry.candidates()} == {
        "ffmpeg",
        "remotion",
        "three",
    }
    pid, timeline = new_production(
        store,
        shots=2,
        frames=24,
        size=(320, 180),
        cues=[{"frame": 6, "kind": "beat"}, {"frame": 30, "kind": "beat"}],
    )
    plans = [
        {
            "shot_id": "shot-1",
            "start_f": 0,
            "end_f": 24,
            "intent": "제목",
            "engine": "remotion",
            "profile": "title-card",
            "params": {"title": "시험", "subtitle": "remotion"},
            "seed": 1,
            "selection": "auto",
            "reason": "시험",
        },
        {
            "shot_id": "shot-2",
            "start_f": 24,
            "end_f": 48,
            "intent": "3D",
            "engine": "three",
            "profile": "orbit-scene",
            "params": {"degrees": 45.0},
            "seed": 7,
            "selection": "auto",
            "reason": "시험",
        },
    ]
    pipeline.apply_shot_plan(pid, timeline["revision"], plans)
    for shot_id in ("shot-1", "shot-2"):
        attempt = pipeline.render_shot(pid, shot_id, review=True)
        assert attempt["state"] == "review_pending"
        receipt = pipeline.attempt_receipt(
            attempt, lambda sha: store.read_artifact(pid, sha)
        )
        assert receipt["real_execution"] is True and receipt["execution_mode"] == "real"
        assert receipt["metrics"]["rss_peak_mb"] > 100
        assert receipt["outputs"][0]["media"]["video"]["width"] == 320
    state = store.get_production(pid)
    assert pipeline.defective_shots(pid) == []
    reports = [r for r in state["review_reports"].values() if r["kind"] == "shot"]
    assert [r["verdict"] for r in reports] == ["pass", "pass"]
    three_receipt = pipeline.attempt_receipt(
        state["attempts"][1], lambda sha: store.read_artifact(pid, sha)
    )
    assert (
        "SwiftShader" in three_receipt["provenance"]["gl_renderer"]
    )  # GPU가 아니라 CPU WebGL로 렌더했다는 증거


def test_adapter_smoke_reports_time_memory_and_frame_determinism(tmp_path):
    three = ThreeAdapter().smoke()
    assert (
        three["decode_ok"]
        and three["decoded_frames"] == 24
        and three["frame_deterministic_across_order"] is True
    )
    remotion = RemotionAdapter().smoke()
    assert (
        remotion["decode_ok"]
        and remotion["decoded_frames"] == 24
        and remotion["provenance"]["remotion"] == "4.0.537"
    )
