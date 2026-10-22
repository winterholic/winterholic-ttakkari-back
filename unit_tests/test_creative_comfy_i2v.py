"""creative 파이프라인의 Comfy I2V 샷(`wan22-5b-i2v-draft`) 계약 시험. 컨트롤러는 대역이다(실제 생성 아님).

기준 이미지는 이 제작이 소유한 CAS 자산(이름+sha256)만 쓸 수 있고, 검증은 lease를 잡는 prepare보다 먼저 일어난다.
"""

from __future__ import annotations

import hashlib

import pytest
from creative_support import (
    FakeComfyController,
    make_pipeline,
    new_production,
    png_bytes,
)

from ttakkari.creative.engines.comfy_engine import ComfyAdapter
from ttakkari.creative.engines.mock import SimulatedCrash
from ttakkari.creative.pipeline import PipelineError

I2V = "wan22-5b-i2v-draft"
NAME = "keyframe_flux_cafe.png"


def _setup(tmp_path, monkeypatch, controller=None, *, image=None, name=NAME):
    import ttakkari.creative.engines.comfy_engine as module

    monkeypatch.setattr(module, "POLL_SECONDS", 0.01)
    controller = controller or FakeComfyController(tmp_path)
    adapter = ComfyAdapter(controller=controller, map_dir=tmp_path / "map", mock=True)
    pipeline, store, registry = make_pipeline(tmp_path, adapter, measure=False)
    pid, timeline = new_production(store, shots=1)
    data = image if image is not None else png_bytes()
    sha = store.put_artifact(pid, name, data, media_type="image/png")["sha256"]
    return pipeline, store, registry, controller, pid, timeline, sha


def _plan(sha, *, profile=I2V, assets=True, selection="explicit-lab"):
    return [
        {
            "shot_id": "shot-1",
            "start_f": 0,
            "end_f": 24,
            "intent": "카페 창가",
            "engine": "comfy",
            "profile": profile,
            "params": {"prompt": "cafe window at night"},
            "seed": 2101,
            "selection": selection,
            "reason": "E1 준비 시험",
            **({"assets": [sha]} if assets else {}),
        }
    ]


def test_i2v_shot_validates_the_owned_asset_before_prepare_and_records_input_provenance(
    tmp_path, monkeypatch
):
    pipeline, store, _registry, controller, pid, timeline, sha = _setup(
        tmp_path, monkeypatch
    )
    pipeline.apply_shot_plan(pid, timeline["revision"], _plan(sha))
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert attempt["state"] == "review_pending"
    spec = attempt["resolved_graph"]
    assert spec["input_assets"] == [{"sha256": sha, "name": NAME, "role": "source"}]
    sent = controller.prepared[0]
    assert (
        sent["profile"] == I2V
        and sent["image"].sha256 == sha
        and sent["image"].asset_id == NAME
    )
    receipt = pipeline.attempt_receipt(attempt, lambda s: store.read_artifact(pid, s))
    assert receipt["real_execution"] is False  # 컨트롤러 대역
    assert receipt["provenance"]["input_image"]["sha256"] == sha
    assert receipt["provenance"]["profile_verification"] == "prepared-unverified"


@pytest.mark.parametrize("mode", ["wrong-sha", "graph-mismatch"])
def test_receipt_that_does_not_bind_the_requested_image_is_not_a_success(
    tmp_path, monkeypatch, mode
):
    controller = FakeComfyController(tmp_path)
    controller.input_receipt = mode
    pipeline, _store, _registry, controller, pid, timeline, sha = _setup(
        tmp_path, monkeypatch, controller
    )
    pipeline.apply_shot_plan(pid, timeline["revision"], _plan(sha))
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert attempt["state"] == "failed" and attempt["outputs"] == []
    assert "receipt_inconsistent" in attempt["error_details"][0]


def test_invalid_image_bytes_fail_before_any_prepare_so_no_lease_is_taken(
    tmp_path, monkeypatch
):
    junk = b"not an image" * 64
    pipeline, _store, _registry, controller, pid, timeline, sha = _setup(
        tmp_path, monkeypatch, image=junk
    )
    pipeline.apply_shot_plan(pid, timeline["revision"], _plan(sha))
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert (
        attempt["state"] == "failed"
        and "invalid_input_image" in attempt["error_details"][0]
    )
    assert (
        controller.prepared == [] and controller.started == []
    )  # prepare(=lease 획득)에 도달하지 않았다


def test_plan_rejects_assets_outside_this_production_and_profile_asset_rules(
    tmp_path, monkeypatch
):
    pipeline, store, _registry, controller, pid, timeline, sha = _setup(
        tmp_path, monkeypatch
    )
    foreign = store.create_production("다른 제작")["manifest"]["production_id"]
    foreign_sha = store.put_artifact(
        foreign, NAME, png_bytes(color=(1, 1, 1)), media_type="image/png"
    )["sha256"]
    with pytest.raises(PipelineError, match="이 제작의 자산 목록에 없습니다"):
        pipeline.apply_shot_plan(
            pid, timeline["revision"], _plan(foreign_sha)
        )  # 다른 제작의 sha
    with pytest.raises(PipelineError, match="이 제작의 자산 목록에 없습니다"):
        pipeline.apply_shot_plan(
            pid, timeline["revision"], _plan(hashlib.sha256(b"x").hexdigest())
        )
    with pytest.raises(PipelineError, match="정확히 하나"):
        pipeline.apply_shot_plan(pid, timeline["revision"], _plan(sha, assets=False))
    with pytest.raises(PipelineError, match="입력 자산을 받지 않습니다"):
        pipeline.apply_shot_plan(
            pid, timeline["revision"], _plan(sha, profile="wan22-5b-draft")
        )  # T2V에 이미지
    with pytest.raises(PipelineError, match="자동 선택 후보가 아닙니다"):
        pipeline.apply_shot_plan(
            pid, timeline["revision"], _plan(sha, selection="auto")
        )  # Comfy는 명시 lab 선택만
    assert store.get_production(pid)["shots"]["shot-1"][-1]["engine"] == "cpu-fake"
    assert controller.prepared == []


def test_bad_asset_names_never_reach_the_adapter(tmp_path, monkeypatch):
    # 업로드 API가 허용하는 이름이라도 managed 입력 규칙(id 정규식)에 맞지 않으면 계획 단계에서 거부된다.
    pipeline, store, _registry, controller, pid, timeline, _sha = _setup(
        tmp_path, monkeypatch
    )
    odd = store.put_artifact(
        pid, "UPPER.PNG", png_bytes(color=(4, 5, 6)), media_type="image/png"
    )["sha256"]
    with pytest.raises(PipelineError, match=r"id\(이름\)와 sha256이 잘못됐습니다"):
        pipeline.apply_shot_plan(pid, timeline["revision"], _plan(odd))
    assert controller.prepared == []


def test_crash_resume_prepares_once_with_the_image_and_never_resubmits(
    tmp_path, monkeypatch
):
    controller = FakeComfyController(tmp_path, polls_before_done=3)
    pipeline, _store, _registry, controller, pid, timeline, sha = _setup(
        tmp_path, monkeypatch, controller
    )
    pipeline.apply_shot_plan(pid, timeline["revision"], _plan(sha))
    attempt = pipeline.submit_shot(pid, "shot-1")
    original, calls = controller.get, {"n": 0}

    def crashing(task_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise SimulatedCrash()
        return original(task_id)

    controller.get = crashing
    with pytest.raises(SimulatedCrash):
        pipeline.execute(pid, attempt["attempt_id"])
    controller.get = original
    resumed = pipeline.resume(pid, review=False)
    assert resumed[0]["state"] == "review_pending"
    assert (
        len(controller.prepared) == 1
        and controller.prepared[0]["image"].sha256 == sha
        and len(controller.started) == 1
    )
