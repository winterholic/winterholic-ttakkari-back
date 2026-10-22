"""엔진 어댑터 ↔ G0 attempt/receipt 계약 시험(CPU mock만 사용).

이 파일의 통과는 어댑터 수명주기·상태 계약의 통과이며 실제 렌더·생성 성공이 아니다.
receipt는 execution_mode=mock, real_execution=False로 남는다는 것도 함께 확인한다.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from creative_support import (
    FakeComfyController,
    FakeTtsProvider,
    ffmpeg_plans,
    make_pipeline,
    new_production,
)

from ttakkari.creative.director import ConflictError
from ttakkari.creative.engines.base import (
    EngineError,
    ExecutionContext,
    validate_params,
)
from ttakkari.creative.engines.comfy_engine import ComfyAdapter
from ttakkari.creative.engines.mock import MockEngineAdapter, SimulatedCrash
from ttakkari.creative.engines.registry import EngineRegistry, RegistryError
from ttakkari.creative.engines.tts_engine import TtsAdapter
from ttakkari.creative.pipeline import PipelineError


def _plans(count=3, engine="mock", profile="clip"):
    return [
        {
            "shot_id": f"shot-{i + 1}",
            "start_f": i * 24,
            "end_f": (i + 1) * 24,
            "intent": f"장면 {i + 1}",
            "engine": engine,
            "profile": profile,
            "params": {"label": f"s{i + 1}"},
            "seed": i + 1,
            "selection": "auto",
            "reason": "시험",
        }
        for i in range(count)
    ]


def _rev(store, pid, shot_id):
    return store.get_production(pid)["shots"][shot_id][-1]["revision"]


def _setup(tmp_path, adapter=None, shots=3):
    adapter = adapter or MockEngineAdapter()
    pipeline, store, registry = make_pipeline(tmp_path, adapter)
    pid, timeline = new_production(store, shots=shots)
    pipeline.apply_shot_plan(pid, timeline["revision"], _plans(shots))
    return pipeline, store, registry, adapter, pid


# ---------- 정상 흐름과 receipt


def test_mock_attempt_has_receipt_inside_outputs_and_is_marked_not_real(tmp_path):
    pipeline, store, _registry, adapter, pid = _setup(tmp_path)
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert attempt["state"] == "review_pending"
    state = store.get_production(pid)
    receipt = pipeline.attempt_receipt(
        attempt, lambda sha: store.read_artifact(pid, sha)
    )
    assert receipt["execution_mode"] == "mock" and receipt["real_execution"] is False
    assert receipt["outputs"][0]["sha256"] == attempt["outputs"][0]["sha256"]
    assert len(attempt["outputs"]) == 2  # 영상 + engine receipt
    assert attempt["resolved_graph"]["engine"] == "mock"
    assert attempt["input_sha256"] and attempt["resolved_graph_sha256"]
    assert state["stale"]["shots"] == [
        "shot-2",
        "shot-3",
    ]  # 렌더된 샷만 stale에서 빠진다
    assert len(adapter.calls) == 1


def test_same_idempotency_key_runs_the_adapter_once(tmp_path):
    pipeline, _store, _registry, adapter, pid = _setup(tmp_path)
    first = pipeline.submit_shot(pid, "shot-1")
    second = pipeline.submit_shot(pid, "shot-1")
    assert first["attempt_id"] == second["attempt_id"]
    pipeline.execute(pid, first["attempt_id"])
    pipeline.execute(pid, first["attempt_id"])
    assert len(adapter.calls) == 1


# ---------- crash 재개


def test_crash_leaves_running_claim_and_resume_reruns_once_without_duplicate_outputs(
    tmp_path,
):
    pipeline, store, _registry, adapter, pid = _setup(tmp_path)
    adapter.plan(crash_times=1)
    attempt = pipeline.submit_shot(pid, "shot-1")
    with pytest.raises(SimulatedCrash):
        pipeline.execute(pid, attempt["attempt_id"])
    crashed = store.get_production(pid)["attempts"][0]
    assert crashed["state"] == "running" and crashed["execution_claim"]
    adapter.plan()
    resumed = pipeline.resume(pid, review=False)
    assert [a["state"] for a in resumed] == ["review_pending"]
    assert len(adapter.calls) == 2
    final = store.get_production(pid)["attempts"][0]
    assert len(final["outputs"]) == 2 and final["execution_claim"] is None
    assert pipeline.resume(pid, review=False) == []  # 끝난 시도는 다시 실행하지 않는다


# ---------- 빈 결과·수집 실패


@pytest.mark.parametrize("behavior", [{"empty": True}, {"empty_bytes": True}])
def test_empty_results_fail_as_empty_output_and_never_review_pending(
    tmp_path, behavior
):
    pipeline, store, _registry, adapter, pid = _setup(tmp_path)
    adapter.plan(**behavior)
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert attempt["state"] == "failed" and attempt["error_kind"] == "empty_output"
    assert attempt["outputs"] == []
    assert (
        "shot-1" in store.get_production(pid)["stale"]["shots"]
    )  # 실패한 샷은 stale 표시가 그대로 남는다


def test_engine_failure_is_recorded_with_engine_error_kind_and_retry_creates_new_attempt(
    tmp_path,
):
    pipeline, store, _registry, adapter, pid = _setup(tmp_path)
    adapter.plan(fail="렌더 프로세스가 비정상 종료했습니다")
    failed = pipeline.render_shot(pid, "shot-1", review=False)
    assert failed["state"] == "failed" and failed["error_kind"] == "collect_failed"
    assert "[engine_error:mock_failed]" in failed["error_details"][0]
    # 같은 키로 다시 제출하면 기존 실패 시도를 돌려주고 실행하지 않는다.
    again = pipeline.submit_shot(pid, "shot-1")
    assert again["attempt_id"] == failed["attempt_id"] and len(adapter.calls) == 1
    adapter.plan()
    retried = pipeline.render_shot(pid, "shot-1", retry=True, review=False)
    assert (
        retried["attempt_id"] != failed["attempt_id"]
        and retried["state"] == "review_pending"
    )
    assert len(store.get_production(pid)["attempts"]) == 2


# ---------- 중복·늦은 이벤트


def test_duplicate_and_late_events_do_not_change_terminal_attempt(tmp_path):
    pipeline, store, _registry, _adapter, pid = _setup(tmp_path)
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    aid = attempt["attempt_id"]
    before = store.get_production(pid)["attempts"][0]
    for event_id, sequence, state in (
        ("e1", 50, "running"),
        ("e1", 50, "running"),
        ("late", 3, "failed"),
        ("e2", 60, "cancelled"),
    ):
        pipeline.store.ingest_event(
            pid, aid, event_id=event_id, sequence=sequence, state=state
        )
    after = store.get_production(pid)["attempts"][0]
    assert after["state"] == "review_pending" == before["state"]
    assert after["outputs"] == before["outputs"]
    events = [e for e in store.get_production(pid)["events"] if e["event_id"] == "e1"]
    assert len(events) == 1  # 같은 event_id는 한 번만 기록


def test_engine_emitted_running_events_are_deduplicated_and_do_not_regress_state(
    tmp_path,
):
    pipeline, store, _registry, adapter, pid = _setup(tmp_path)
    adapter.plan(emit=True)
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert attempt["state"] == "review_pending"
    events = [
        e
        for e in store.get_production(pid)["events"]
        if e["event_id"] == f"engine:{attempt['attempt_id']}:running"
    ]
    assert len(events) == 1  # ctx.emit을 두 번 불러도 한 번만 기록


# ---------- 취소


def test_cancel_stops_a_waiting_engine_and_blocks_new_submissions(tmp_path):
    pipeline, _store, _registry, adapter, pid = _setup(tmp_path)
    adapter.plan(wait_for_cancel=True)
    attempt = pipeline.submit_shot(pid, "shot-1")
    result: dict = {}

    def run():
        result["attempt"] = pipeline.execute(pid, attempt["attempt_id"])

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 5
    while not adapter.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    pipeline.store.cancel_attempt(pid, attempt["attempt_id"])
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result["attempt"]["state"] == "cancelled"
    assert result["attempt"]["outputs"] == []
    with pytest.raises(ValueError, match="취소"):
        pipeline.submit_shot(pid, "shot-2")
    assert pipeline.resume(pid) == []
    # 취소된 시도는 결함 샷이 아니다.
    assert pipeline.defective_shots(pid) == []


def test_slot_wait_is_cancellable_so_cancelled_attempt_never_holds_the_engine_slot(
    tmp_path,
):
    pipeline, store, _registry, adapter, pid = _setup(tmp_path)
    assert pipeline._slots.acquire(timeout=1)  # 다른 실행이 슬롯을 점유 중인 상황
    try:
        attempt = pipeline.submit_shot(pid, "shot-1")
        out: dict = {}
        worker = threading.Thread(
            target=lambda: out.setdefault(
                "a", pipeline.execute(pid, attempt["attempt_id"])
            )
        )
        worker.start()
        time.sleep(0.5)
        store.cancel_attempt(pid, attempt["attempt_id"])
        worker.join(timeout=10)
        assert out["a"]["state"] == "cancelled" and adapter.calls == []
    finally:
        pipeline._slots.release()


# ---------- revision 충돌과 stale 전파


def test_revision_conflict_on_revise_and_double_feedback(tmp_path):
    pipeline, store, _registry, _adapter, pid = _setup(tmp_path)
    before = dict(store.get_production(pid)["manifest"]["active_shot_revisions"])
    base = before["shot-2"]
    shot = pipeline.revise_shot(
        pid,
        "shot-2",
        expected_revision=base,
        params={"label": "new"},
        selection="explicit-lab",
    )
    assert shot["revision"] == base + 1
    with pytest.raises(ConflictError):
        pipeline.revise_shot(
            pid,
            "shot-2",
            expected_revision=base,
            params={"label": "other"},
            selection="explicit-lab",
        )
    # 다른 샷의 revision은 바뀌지 않았다.
    manifest = store.get_production(pid)["manifest"]["active_shot_revisions"]
    assert manifest == {**before, "shot-2": base + 1}


def test_stale_propagation_is_scoped_and_final_assembly_refuses_stale(tmp_path):
    pipeline, store, _registry, _adapter, pid = _setup(tmp_path)
    for shot_id in ("shot-1", "shot-2", "shot-3"):
        pipeline.render_shot(pid, shot_id, review=False)
    state = store.get_production(pid)
    assert state["stale"]["shots"] == []
    outputs_before = pipeline.current_outputs(state)
    pipeline.revise_shot(
        pid,
        "shot-2",
        expected_revision=_rev(store, pid, "shot-2"),
        params={"label": "changed"},
        selection="explicit-lab",
    )
    state = store.get_production(pid)
    assert state["stale"]["shots"] == ["shot-2"]
    assert {"shot-1", "shot-3"} <= set(state["stale"]["transition_neighbors"])
    with pytest.raises(PipelineError, match="stale"):
        pipeline.compose_final(pid)
    # 바이블이 바뀌면 모든 샷의 자산이 stale
    store.update_bible(
        pid,
        expected_revision=store.get_production(pid)["bibles"][-1]["revision"],
        values={"palette": ["#000000", "#ffffff"]},
    )
    assert store.get_production(pid)["stale"]["bible_assets"] == [
        "shot-1",
        "shot-2",
        "shot-3",
    ]
    # 바이블이 바뀌면 이전 바이블로 만든 출력은 현재 출력이 아니다.
    assert pipeline.current_outputs(store.get_production(pid)) == {}
    with pytest.raises(
        PipelineError, match="stale 샷이 있습니다: shot-1, shot-2, shot-3"
    ):
        pipeline.compose_final(pid)
    assert outputs_before["shot-1"] != ""


def test_rerender_only_changes_target_shot_and_neighbor_hashes_are_unchanged(tmp_path):
    pipeline, store, _registry, _adapter, pid = _setup(tmp_path)
    for shot_id in ("shot-1", "shot-2", "shot-3"):
        pipeline.render_shot(pid, shot_id, review=False)
    before = pipeline.current_outputs(store.get_production(pid))
    attempts_before = {
        a["shot_id"]: a["attempt_id"] for a in store.get_production(pid)["attempts"]
    }
    revs_before = dict(store.get_production(pid)["manifest"]["active_shot_revisions"])
    base = revs_before["shot-2"]
    pipeline.revise_shot(
        pid,
        "shot-2",
        expected_revision=base,
        params={"label": "fixed"},
        seed=99,
        selection="explicit-lab",
        reason="결함 교정",
    )
    pipeline.render_shot(pid, "shot-2", review=False)
    state = store.get_production(pid)
    after = pipeline.current_outputs(state)
    assert after["shot-1"] == before["shot-1"] and after["shot-3"] == before["shot-3"]
    assert after["shot-2"] != before["shot-2"]
    assert state["manifest"]["active_shot_revisions"] == {
        **revs_before,
        "shot-2": base + 1,
    }
    assert len(state["attempts"]) == 4
    assert {
        a["shot_id"]
        for a in state["attempts"]
        if a["attempt_id"] not in attempts_before.values()
    } == {"shot-2"}
    assert state["stale"]["shots"] == []


# ---------- 레지스트리: 실측 전용 가용성


def test_unmeasured_mock_and_stale_engines_are_not_auto_candidates(tmp_path):
    clock = {"now": 1_000.0}
    adapter = MockEngineAdapter()
    registry = EngineRegistry(
        tmp_path / "r.json",
        [adapter],
        fresh_seconds=60,
        clock=lambda: clock["now"],
        allow_mock_selection=False,
    )
    assert registry.candidates() == []
    registry.measure("mock")
    assert registry.candidates() == []  # mock은 측정돼도 자동 선택 대상이 아니다
    with pytest.raises(RegistryError):
        registry.run_smoke("mock")
    with pytest.raises(RegistryError):
        registry.record_verification("mock", "lab_executed", {"kind": "real_smoke"})
    registry.record_mock_contract("mock", {"tests": 1})
    assert registry.status("mock")["verification"]["level"] == "untested"
    assert registry.status("mock")["mock_contract"][0]["tests"] == 1

    allowed = EngineRegistry(
        tmp_path / "r2.json",
        [adapter],
        fresh_seconds=60,
        clock=lambda: clock["now"],
        allow_mock_selection=True,
    )
    assert allowed.candidates() == []  # 미측정
    allowed.measure("mock")
    assert allowed.candidates() == []  # 정책이 experimental
    with pytest.raises(RegistryError, match="자동 선택으로 올릴 수 없습니다"):
        EngineRegistry(tmp_path / "r3.json", [adapter]).set_policy(
            "mock", "auto_eligible", "미측정"
        )
    allowed.set_policy("mock", "auto_eligible", "시험")
    assert allowed.candidates() == [{"engine": "mock", "profile": "clip"}]
    clock["now"] += 120  # 측정이 오래되면 후보에서 빠진다
    assert allowed.candidates() == []
    allowed.measure("mock")
    assert allowed.candidates() == [{"engine": "mock", "profile": "clip"}]
    allowed.set_policy("mock", "held", "중단")
    assert allowed.candidates() == []


def test_failed_remeasure_drops_auto_policy_and_gpu_busy_hides_gpu_profiles(tmp_path):
    class Flaky(MockEngineAdapter):
        ok = True

        def probe(self):
            if not self.ok:
                raise RuntimeError("도구가 사라졌습니다")
            return super().probe()

    flaky = Flaky("flaky")
    registry = EngineRegistry(tmp_path / "r.json", [flaky], allow_mock_selection=True)
    registry.measure("flaky")
    registry.set_policy("flaky", "auto_eligible", "시험")
    assert registry.candidates()
    flaky.ok = False
    result = registry.measure("flaky")
    assert (
        result["state"] == "probe_failed" and "도구가 사라졌습니다" in result["error"]
    )
    assert registry.status("flaky")["policy"]["value"] == "experimental"
    assert registry.candidates() == []

    gpu = MockEngineAdapter("gpuish")
    gpu.profiles = {
        "g": gpu.profiles["clip"].__class__(
            "g", frozenset({"gen"}), "x", "video", resource_family="gpu"
        )
    }
    reg2 = EngineRegistry(
        tmp_path / "r2.json",
        [gpu],
        allow_mock_selection=True,
        resource_busy=lambda family: "lease 사용 중" if family == "gpu" else None,
    )
    reg2.measure("gpuish")
    reg2.set_policy("gpuish", "auto_eligible", "시험")
    assert reg2.candidates() == []


def test_pipeline_rejects_engine_not_eligible_for_auto_but_allows_explicit_lab_with_valid_spec(
    tmp_path,
):
    adapter = MockEngineAdapter()
    other = MockEngineAdapter("mock2")
    pipeline, store, registry = make_pipeline(tmp_path, adapter, measure=False)
    registry.measure("mock")  # 측정만 하고 정책은 experimental
    pid, timeline = new_production(store, shots=1)
    with pytest.raises(PipelineError, match="자동 선택 후보가 아닙니다"):
        pipeline.apply_shot_plan(pid, timeline["revision"], _plans(1))
    assert (
        store.get_production(pid)["shots"]["shot-1"][-1]["engine"] == "cpu-fake"
    )  # 아무것도 기록되지 않았다
    explicit = _plans(1)
    explicit[0]["selection"] = "explicit-lab"
    pipeline.apply_shot_plan(pid, timeline["revision"], explicit)
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    receipt = pipeline.attempt_receipt(
        attempt, lambda sha: store.read_artifact(pid, sha)
    )
    assert receipt["selection"] == "explicit-lab"
    assert other.engine_id == "mock2"


# ---------- 매개변수 스키마


def test_param_schema_rejects_unknown_wrongly_typed_and_out_of_range_values():
    schema = {
        "n": {"type": "int", "default": 1, "min": 1, "max": 3},
        "t": {"type": "str", "required": True, "max_len": 5},
        "e": {"type": "enum", "values": ["a"], "default": "a"},
    }
    assert validate_params(schema, {"t": "ok"}) == []
    assert len(validate_params(schema, {"n": True, "t": "ok"})) == 1
    assert len(validate_params(schema, {"n": 9, "t": "ok"})) == 1
    assert len(validate_params(schema, {"t": "too long", "x": 1})) == 2
    assert len(validate_params(schema, {})) == 1  # t 필수


# ---------- Comfy 어댑터(managed 컨트롤러 대역)


def _comfy_pipeline(tmp_path, controller, monkeypatch):
    adapter = ComfyAdapter(
        controller=controller, map_dir=tmp_path / "comfy-map", mock=True
    )
    import ttakkari.creative.engines.comfy_engine as module

    monkeypatch.setattr(module, "POLL_SECONDS", 0.01)
    pipeline, store, _registry = make_pipeline(tmp_path, adapter, measure=False)
    pid, timeline = new_production(store, shots=1)
    plan = [
        {
            "shot_id": "shot-1",
            "start_f": 0,
            "end_f": 24,
            "intent": "생성 샷",
            "engine": "comfy",
            "profile": "wan22-5b-draft",
            "params": {"prompt": "여우가 달린다"},
            "seed": 7,
            "selection": "explicit-lab",
            "reason": "계약 시험",
        }
    ]
    pipeline.apply_shot_plan(pid, timeline["revision"], plan)
    return pipeline, store, adapter, pid


def test_comfy_contract_success_records_provenance_and_marks_non_exact_profile(
    tmp_path, monkeypatch
):
    controller = FakeComfyController(tmp_path)
    pipeline, store, adapter, pid = _comfy_pipeline(tmp_path, controller, monkeypatch)
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert attempt["state"] == "review_pending"
    receipt = pipeline.attempt_receipt(
        attempt, lambda sha: store.read_artifact(pid, sha)
    )
    assert receipt["provenance"]["external_prompt_id"] == "prompt-1"
    assert receipt["provenance"]["seed"] == 7
    # 컨트롤러가 대역이므로 실제 생성이 아니라고 receipt에 남아야 한다.
    assert receipt["real_execution"] is False and receipt["execution_mode"] == "mock"
    assert controller.prepared[0][
        "profile"
    ] == "wan22-5b-draft" and controller.started == [controller.prepared[0]["task_id"]]
    assert adapter.profiles["wan22-5b-draft"].frame_exact is False
    assert adapter.probe().state in {
        "present",
        "absent",
        "probe_failed",
    }  # 서버를 띄우지 않으므로 available이 될 수 없다


def test_comfy_crash_resume_reuses_stored_task_and_never_resubmits(
    tmp_path, monkeypatch
):
    controller = FakeComfyController(tmp_path, polls_before_done=3)
    pipeline, store, _adapter, pid = _comfy_pipeline(tmp_path, controller, monkeypatch)
    attempt = pipeline.submit_shot(pid, "shot-1")
    original_get = controller.get
    calls = {"n": 0}

    def crashing_get(task_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise SimulatedCrash()
        return original_get(task_id)

    controller.get = crashing_get
    with pytest.raises(SimulatedCrash):
        pipeline.execute(pid, attempt["attempt_id"])
    assert store.get_production(pid)["attempts"][0]["state"] == "running"
    controller.get = original_get
    resumed = pipeline.resume(pid, review=False)
    assert resumed[0]["state"] == "review_pending"
    assert (
        len(controller.prepared) == 1 and len(controller.started) == 1
    )  # 중복 제출 없음
    receipt = pipeline.attempt_receipt(
        resumed[0], lambda sha: store.read_artifact(pid, sha)
    )
    assert receipt["metrics"]["resumed_existing_task"] is True


@pytest.mark.parametrize(
    "outcome,kind",
    [
        ("failed", "collect_failed"),
        ("empty", "empty_output"),
        ("inconsistent", "collect_failed"),
    ],
)
def test_comfy_failure_empty_and_inconsistent_receipt_never_become_success(
    tmp_path, outcome, kind, monkeypatch
):
    controller = FakeComfyController(tmp_path, outcome=outcome)
    pipeline, _store, _adapter, pid = _comfy_pipeline(tmp_path, controller, monkeypatch)
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert attempt["state"] == "failed" and attempt["error_kind"] == kind
    assert attempt["outputs"] == []


def test_comfy_needs_reconcile_uses_reconcile_and_hash_mismatch_is_collection_failure(
    tmp_path, monkeypatch
):
    controller = FakeComfyController(tmp_path, outcome="needs_reconcile")
    pipeline, _store, _adapter, pid = _comfy_pipeline(tmp_path, controller, monkeypatch)
    attempt = pipeline.render_shot(pid, "shot-1", review=False)
    assert attempt["state"] == "review_pending" and len(controller.reconciled) == 1

    tamper = FakeComfyController(tmp_path / "t")
    (tmp_path / "t").mkdir()
    pipeline2, _store2, _adapter2, pid2 = _comfy_pipeline(
        tmp_path / "t", tamper, monkeypatch
    )
    original = tamper._finish

    def tampered(job):
        original(job)
        (tmp_path / "t" / f"{job['task_id']}.mp4").write_bytes(b"CORRUPT")

    tamper._finish = tampered
    attempt2 = pipeline2.render_shot(pid2, "shot-1", review=False)
    assert (
        attempt2["state"] == "failed"
        and "해시가 receipt와 다릅니다" in attempt2["error_details"][0]
    )


def test_comfy_cancel_calls_owned_task_cancel_and_does_not_succeed(
    tmp_path, monkeypatch
):
    controller = FakeComfyController(tmp_path, polls_before_done=10_000)
    pipeline, store, _adapter, pid = _comfy_pipeline(tmp_path, controller, monkeypatch)
    attempt = pipeline.submit_shot(pid, "shot-1")
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "a", pipeline.execute(pid, attempt["attempt_id"])
        )
    )
    worker.start()
    deadline = time.monotonic() + 5
    while not controller.started and time.monotonic() < deadline:
        time.sleep(0.01)
    store.cancel_attempt(pid, attempt["attempt_id"])
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result["a"]["state"] == "cancelled"
    assert controller.cancelled == [controller.prepared[0]["task_id"]]
    assert len(controller.prepared) == 1


def test_comfy_validate_rejects_bad_seed_prompt_and_unknown_profile(
    tmp_path, monkeypatch
):
    adapter = ComfyAdapter(
        controller=FakeComfyController(tmp_path), map_dir=tmp_path / "m"
    )
    base = {
        "profile": "wan22-5b-draft",
        "params": {"prompt": "x"},
        "seed": 1,
        "idempotency_key": "k",
    }
    assert adapter.validate(base) == []
    assert adapter.validate({**base, "seed": -1}) and adapter.validate(
        {**base, "seed": True}
    )
    assert adapter.validate({**base, "params": {}})
    assert adapter.validate({**base, "params": {"prompt": "x", "steps": 99}})
    assert adapter.validate({**base, "profile": "other"})
    assert adapter.validate({**base, "idempotency_key": ""})
    no_key = {k: v for k, v in base.items() if k != "idempotency_key"}
    assert (
        adapter.validate(no_key) == []
    )  # 계획 단계 검증은 key 없이 통과한다. 실행 단계가 key를 강제한다.
    with pytest.raises(EngineError, match="idempotency_key"):
        adapter.execute(ExecutionContext(spec=no_key, workdir=_mkdir(tmp_path / "nk")))


# ---------- TTS 어댑터(공급자 대역)


def test_tts_adapter_concatenates_with_exact_integer_sample_timings(tmp_path):
    adapter = TtsAdapter(provider=FakeTtsProvider(tmp_path), mock=True)
    spec = {
        "profile": "supertonic-speech",
        "params": {"lines": ["하나", "둘"], "gap_ms": 100, "lead_ms": 0},
    }
    run = adapter.execute(ExecutionContext(spec=spec, workdir=_mkdir(tmp_path / "w")))
    timing = json.loads(run.outputs[1].data)
    assert timing["lines"][0] == {
        "line": 0,
        "text": "하나",
        "start_sample": 0,
        "end_sample": 4410,
    }
    assert timing["lines"][1]["start_sample"] == 4410 + 4410  # 100ms 간격 = 4410샘플
    assert timing["total_samples"] == 4410 + 4410 + 8820
    from ttakkari.creative.engines.tts_engine import read_wav_header

    assert read_wav_header(run.outputs[0].data)["samples"] == timing["total_samples"]


def test_tts_adapter_rejects_silent_chunk_empty_lines_and_honors_cancel(tmp_path):
    from ttakkari.creative.engines.base import EngineEmptyOutput

    adapter = TtsAdapter(provider=FakeTtsProvider(tmp_path, empty_index=1), mock=True)
    spec = {"profile": "supertonic-speech", "params": {"lines": ["a", "b"]}}
    with pytest.raises(EngineEmptyOutput):
        adapter.execute(ExecutionContext(spec=spec, workdir=_mkdir(tmp_path / "w1")))
    assert adapter.validate(
        {"profile": "supertonic-speech", "params": {"lines": ["a", "  "]}}
    )
    cancelled = ExecutionContext(
        spec=spec, workdir=_mkdir(tmp_path / "w2"), cancelled=lambda: True
    )
    with pytest.raises(EngineError):
        TtsAdapter(provider=FakeTtsProvider(tmp_path), mock=True).execute(cancelled)


def _mkdir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_stale_ffmpeg_plans_helper_is_consistent():
    plans = ffmpeg_plans(2)
    assert [p["shot_id"] for p in plans] == ["shot-1", "shot-2"]
