"""DB-free tests reproducing REGNA friction improvements:
1. Fault isolation & accurate error classification (preventing budget_exceeded override when adapter failed or raised).
2. Configuration preservation on study resume/snapshot.
3. Full prompt and output trajectory persistence for LLM inference.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from ttakkari.models import utcnow
from ttakkari.research import runner, service
from ttakkari.research.adapters.base import AdapterRunResult
from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter
from ttakkari.research.models import (
    BestResult,
    Configuration,
    Event,
    Experiment,
    Node,
    Relation,
    Run,
    Study,
)


class _MockQueryDB:
    def __init__(self, data: dict):
        self.data = data
        self.added = []

    async def get(self, model, ident, **kwargs):
        table = self.data.get(model, {})
        return table.get(ident)

    async def scalar(self, query):
        str_q = str(query)
        for cls, name in [
            (Run, "research_runs"),
            (BestResult, "research_best_results"),
            (Node, "research_nodes"),
            (Relation, "research_relations"),
            (Experiment, "research_experiments"),
            (Event, "research_events"),
            (Configuration, "research_configurations"),
        ]:
            if name in str_q and "count" in str_q:
                return len(self.data.get(cls, {}))
        if "research_runs" in str_q:
            runs = list(self.data.get(Run, {}).values())
            for r in runs:
                if r.status == "queued":
                    return r.id
            return runs[0].id if runs else None
        return None

    class _ScalarsWrapper:
        def __init__(self, items):
            self.items = items

        def all(self):
            return self.items

        def one_or_none(self):
            return self.items[0] if self.items else None

    async def scalars(self, query):
        str_q = str(query)
        for cls, name in [
            (Configuration, "research_configurations"),
            (Run, "research_runs"),
            (BestResult, "research_best_results"),
            (Node, "research_nodes"),
            (Relation, "research_relations"),
            (Experiment, "research_experiments"),
            (Event, "research_events"),
        ]:
            if name in str_q:
                items = list(self.data.get(cls, {}).values())
                offset = getattr(query, "_offset_clause", None)
                limit = getattr(query, "_limit_clause", None)
                if offset is not None:
                    items = items[int(offset.value):]
                if limit is not None:
                    items = items[:int(limit.value)]
                return self._ScalarsWrapper(items)
        return self._ScalarsWrapper([])

    async def execute(self, query):
        return self._ScalarsWrapper([])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, obj, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_snapshot_includes_configurations():
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg1_id = uuid.uuid4()
    cfg2_id = uuid.uuid4()

    study = Study(
        id=study_id,
        title="REGNA Friction Verification Study",
        goal="Verify fault isolation, config preservation and trajectory persistence",
        scope="personal",
        version=1,
        created_at=utcnow(),
    )
    exp = Experiment(
        id=exp_id,
        study_id=study_id,
        title="Fault Isolation Experiment",
        hypothesis="Errors should be isolated and configurations preserved",
        adapter_id="llm_inference",
        budget={"seconds": 30, "max_total_seconds": 60, "max_runs": 10},
        state="queued",
        created_at=utcnow(),
    )
    cfg1 = Configuration(
        id=cfg1_id,
        experiment_id=exp_id,
        label="Config-1",
        role="candidate",
        order_index=0,
        parameters={"model": "qwen3.5-4b-q4_k_m", "prompt": "테스트 프롬프트 1", "ctx_size": 2048},
        config_hash="hash1",
        created_at=utcnow(),
    )
    cfg2 = Configuration(
        id=cfg2_id,
        experiment_id=exp_id,
        label="Config-2",
        role="candidate",
        order_index=1,
        parameters={"model": "qwen3.5-4b-q4_k_m", "prompt": "테스트 프롬프트 2", "ctx_size": 4096},
        config_hash="hash2",
        created_at=utcnow(),
    )

    db = _MockQueryDB({
        Study: {study_id: study},
        Experiment: {exp_id: exp},
        Configuration: {cfg1_id: cfg1, cfg2_id: cfg2},
        Node: {},
        Relation: {},
        Run: {},
        Event: {},
        BestResult: {},
    })

    snap = await service.snapshot(db, study)

    assert "configurations" in snap, "snapshot must include configurations key for resuming study"
    cfg_ids = {c["id"] for c in snap["configurations"]}
    assert str(cfg1_id) in cfg_ids
    assert str(cfg2_id) in cfg_ids
    cfg1_data = next(c for c in snap["configurations"] if c["id"] == str(cfg1_id))
    assert cfg1_data["parameters"]["prompt"] == "테스트 프롬프트 1"
    assert cfg1_data["parameters"]["ctx_size"] == 2048


@pytest.mark.asyncio
async def test_llm_inference_preserves_prompt_and_trajectories():
    adapter = LlmInferenceAdapter()

    fake_bench_result = {
        "execution_path": "host_gpu",
        "measured_on": "host GPU path outside sandbox",
        "prompt": "한국어 AI 모델의 성능을 측정하는 표준 벤치마크 테스트 문장입니다.",
        "runs": [
            {
                "run": 1,
                "wall_seconds": 1.2,
                "prompt_tokens": 15,
                "prompt_tokens_per_second": 120.0,
                "generated_tokens": 30,
                "generation_tokens_per_second": 25.0,
                "content": "안녕하세요! 이것은 모델의 전체 생성 텍스트 응답입니다.",
                "output_preview": "안녕하세요! 이것은 모델의...",
            }
        ],
        "median_generation_tokens_per_second": 25.0,
        "median_prompt_tokens_per_second": 120.0,
    }

    mock_host_gpu = MagicMock()
    mock_host_gpu.start = MagicMock()
    def get_current():
        lid = mock_host_gpu.start.call_args.kwargs.get("launch_id") if mock_host_gpu.start.call_args else "test_launch_id"
        return {"launch_id": lid, "phase": "ready"}
    mock_host_gpu._current.side_effect = get_current
    mock_host_gpu.status.return_value = {"ready": True}
    mock_host_gpu.bench.return_value = fake_bench_result
    mock_host_gpu.stop.return_value = {"stopped_now": True, "running": False, "phase": "stopped"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ttakkari.host_gpu.HostGpu", lambda s: mock_host_gpu)
        mp.setattr("ttakkari.research.adapters.llm_inference.checkpoint_resource", AsyncMock())
        mp.setattr(adapter, "validate_config", lambda cfg, scope: [])
        mp.setattr(adapter, "_cleanup_owned_launch", lambda hg, dd, launch: {"success": True, "stopped": True})

        res = await adapter.execute_run(
            run_id=uuid.uuid4(),
            config={"model": "qwen3.5-4b-q4_k_m", "prompt": "테스트 프롬프트"},
            scope="personal",
        )

    assert res.status == "succeeded"
    assert "prompt" in res.raw_output, "raw_output must preserve prompt"
    assert res.raw_output["prompt"] == "테스트 프롬프트"
    assert "sample_trajectories" in res.raw_output, "raw_output must contain sample_trajectories"
    trajectories = res.raw_output["sample_trajectories"]
    assert len(trajectories) == 1
    assert trajectories[0]["prompt"] == "테스트 프롬프트"
    assert trajectories[0]["response"] == "안녕하세요! 이것은 모델의 전체 생성 텍스트 응답입니다."
    assert trajectories[0]["response_completeness"] == "full"
    assert trajectories[0]["response_source"] == "content"
    assert trajectories[0]["generated_tokens"] == 30


@pytest.mark.asyncio
async def test_llm_preview_fallback_is_marked_incomplete_and_preserves_raw_run():
    adapter = LlmInferenceAdapter()
    bench = {"prompt": "original", "runs": [{"run": 1, "output_preview": "미리보기"}]}
    host = MagicMock()
    def get_current():
        launch_id = host.start.call_args.kwargs.get("launch_id") if host.start.call_args else "launch"
        return {"launch_id": launch_id, "phase": "ready"}
    host._current.side_effect = get_current
    host.status.return_value = {"ready": True}
    host.bench.return_value = bench
    host.stop.return_value = {"stopped_now": True, "running": False}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ttakkari.host_gpu.HostGpu", lambda _: host)
        mp.setattr("ttakkari.research.adapters.llm_inference.checkpoint_resource", AsyncMock())
        mp.setattr(adapter, "validate_config", lambda cfg, scope: [])
        mp.setattr(adapter, "_read_launch", lambda gpu: gpu._current())
        mp.setattr(adapter, "_cleanup_owned_launch", lambda *args: {"success": True})
        result = await adapter.execute_run(uuid.uuid4(), {"model": "qwen3.5-4b-q4_k_m"}, scope="personal")
    item = result.raw_output["sample_trajectories"][0]
    assert item["response"] == "미리보기"
    assert item["response_completeness"] == "preview_fallback"
    assert item["response_source"] == "output_preview"
    assert item["prompt"] == result.raw_output["prompt"]
    assert result.raw_output["runs"][0]["output_preview"] == "미리보기"


class _MockSessionManager:
    def __init__(self, db):
        self._db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_failed_run_preserves_error_code_not_overwritten_by_budget_exceeded():
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run_id = uuid.uuid4()

    study = Study(id=study_id, scope="personal", title="Study", created_at=utcnow())
    exp = Experiment(id=exp_id, study_id=study_id, title="Exp", budget={}, created_at=utcnow())
    cfg = Configuration(id=cfg_id, experiment_id=exp_id, label="C", parameters={}, config_hash="h", created_at=utcnow())
    run = Run(id=run_id, experiment_id=exp_id, configuration_id=cfg_id, study_id=study_id,
              adapter_id="mock_adapter", status="queued", created_at=utcnow())

    db = _MockQueryDB({
        Study: {study_id: study},
        Experiment: {exp_id: exp},
        Configuration: {cfg_id: cfg},
        Run: {run_id: run},
    })

    mock_adapter = MagicMock()
    mock_adapter.execute_run = AsyncMock(
        return_value=AdapterRunResult(
            status="failed",
            raw_output={"error": "server_not_ready"},
            environment_snapshot={"scope": "personal"},
            error_code="server_not_ready",
            error_message="llama-server did not become ready in 45s",
            wall_seconds=45.0,
        )
    )
    reg = MagicMock()
    reg.get_adapter.return_value = mock_adapter

    sm = _MockSessionManager(db)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "sessionmaker", lambda: sm)
        mp.setattr(runner, "_fence_owner", lambda run, owner: None)
        mp.setattr(runner, "ExecutionOwner", MagicMock(acquire=MagicMock(return_value=MagicMock(
            metadata={"generation_id": None, "token": "test"}, snapshot=lambda x, **kw: x, close=lambda: None
        ))))
        future_deadline = asyncio.get_running_loop().time() + 60
        result_run = await runner.execute_run_instance(run_id, adapter_registry=reg, deadline=future_deadline)

    # Must preserve adapter's original failure cause, NOT overwritten with budget_exceeded/cancelled
    assert result_run.status == "failed"
    assert result_run.error_code == "server_not_ready"
    assert "llama-server" in (result_run.error_message or "")


@pytest.mark.asyncio
async def test_adapter_runtime_exception_classified_as_failed_not_budget_exceeded():
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run_id = uuid.uuid4()

    study = Study(id=study_id, scope="personal", title="Study", created_at=utcnow())
    exp = Experiment(id=exp_id, study_id=study_id, title="Exp", budget={}, created_at=utcnow())
    cfg = Configuration(id=cfg_id, experiment_id=exp_id, label="C", parameters={}, config_hash="h", created_at=utcnow())
    run = Run(id=run_id, experiment_id=exp_id, configuration_id=cfg_id, study_id=study_id,
              adapter_id="mock_adapter", status="queued", created_at=utcnow())

    db = _MockQueryDB({
        Study: {study_id: study},
        Experiment: {exp_id: exp},
        Configuration: {cfg_id: cfg},
        Run: {run_id: run},
    })

    mock_adapter = MagicMock()
    mock_adapter.execute_run = AsyncMock(side_effect=RuntimeError("gpu_memory_segmentation_fault"))
    reg = MagicMock()
    reg.get_adapter.return_value = mock_adapter

    sm = _MockSessionManager(db)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "sessionmaker", lambda: sm)
        mp.setattr(runner, "_fence_owner", lambda run, owner: None)
        mp.setattr(runner, "ExecutionOwner", MagicMock(acquire=MagicMock(return_value=MagicMock(
            metadata={"generation_id": None, "token": "test"}, snapshot=lambda x, **kw: x, close=lambda: None
        ))))
        future_deadline = asyncio.get_running_loop().time() + 60
        result_run = await runner.execute_run_instance(run_id, adapter_registry=reg, deadline=future_deadline)

    assert result_run.status == "failed"
    assert result_run.error_code == "RuntimeError"
    assert "gpu_memory_segmentation_fault" in (result_run.error_message or "")


@pytest.mark.asyncio
async def test_adapter_timeout_error_is_not_misclassified_as_experiment_deadline():
    study_id, exp_id, cfg_id, run_id = [uuid.uuid4() for _ in range(4)]
    study = Study(id=study_id, scope="personal", title="Study", created_at=utcnow())
    exp = Experiment(id=exp_id, study_id=study_id, title="Exp", budget={}, created_at=utcnow())
    cfg = Configuration(id=cfg_id, experiment_id=exp_id, label="C", parameters={}, config_hash="h", created_at=utcnow())
    run = Run(id=run_id, experiment_id=exp_id, configuration_id=cfg_id, study_id=study_id,
              adapter_id="mock", status="queued", created_at=utcnow())
    db = _MockQueryDB({Study: {study_id: study}, Experiment: {exp_id: exp}, Configuration: {cfg_id: cfg}, Run: {run_id: run}})
    reg = MagicMock()
    reg.get_adapter.return_value.execute_run = AsyncMock(side_effect=TimeoutError("adapter internal timeout"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "sessionmaker", lambda: _MockSessionManager(db))
        mp.setattr(runner, "_fence_owner", lambda run, owner: None)
        mp.setattr(runner, "ExecutionOwner", MagicMock(acquire=MagicMock(return_value=MagicMock(
            metadata={"generation_id": None, "token": "test"}, snapshot=lambda x, **kw: x, close=lambda: None
        ))))
        result = await runner.execute_run_instance(run_id, adapter_registry=reg)
    assert result.status == "failed"
    assert result.error_code == "TimeoutError"
    assert "adapter internal timeout" in result.error_message
    assert result.environment_snapshot["research_adapter_receipt"]["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_memory_guard_stops_failed_adapter_and_preserves_adapter_receipt(monkeypatch):
    from contextlib import nullcontext

    study_id, exp_id, cfg_id, run_id = [uuid.uuid4() for _ in range(4)]
    study = Study(id=study_id, scope="personal", title="Study", created_at=utcnow())
    exp = Experiment(id=exp_id, study_id=study_id, title="Exp", budget={}, created_at=utcnow())
    cfg = Configuration(id=cfg_id, experiment_id=exp_id, label="C", parameters={}, config_hash="h", created_at=utcnow())
    run = Run(id=run_id, experiment_id=exp_id, configuration_id=cfg_id, study_id=study_id,
              adapter_id="mock", status="queued", created_at=utcnow())
    db = _MockQueryDB({Study: {study_id: study}, Experiment: {exp_id: exp}, Configuration: {cfg_id: cfg}, Run: {run_id: run}})

    class Guard:
        def __init__(self):
            self.samples = [100]
            self.error_code = "memory_guard_stopped"

        def snapshot(self):
            return {"samples": self.samples, "error_code": self.error_code}

    reg = MagicMock()
    reg.get_adapter.return_value.execute_run = AsyncMock(return_value=AdapterRunResult(
        status="failed", error_code="adapter_failed", error_message="synthetic adapter error",
        raw_output={"sentinel": "synthetic only"}, environment_snapshot={},
    ))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "sessionmaker", lambda: _MockSessionManager(db))
        mp.setattr(runner, "_fence_owner", lambda run, owner: None)
        mp.setattr(runner, "ExecutionOwner", MagicMock(acquire=MagicMock(return_value=MagicMock(
            metadata={"generation_id": None, "token": "test"}, snapshot=lambda x, **kw: x, close=lambda: None
        ))))
        mp.setattr(runner, "worker_memory_limit", lambda memory_mb: nullcontext(Guard()))
        mp.setattr("ttakkari.research.resources.resource_checkpoints", lambda callback: nullcontext())
        result = await runner.execute_run_instance(run_id, adapter_registry=reg)

    assert result.status == "cancelled"
    assert result.error_code == "memory_guard_stopped"
    assert result.environment_snapshot["research_memory_guard"]["error_code"] == "memory_guard_stopped"
    receipt = result.environment_snapshot["research_adapter_receipt"]
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "adapter_failed"
    assert receipt["error_message"] == "synthetic adapter error"
    assert receipt["raw_output"] == {"sentinel": "synthetic only"}


@pytest.mark.asyncio
async def test_snapshot_paginates_runs_and_best_results_and_bounds_configurations():
    study_id, exp_id, cfg_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    study = Study(id=study_id, title="page", goal="", scope="personal", created_at=utcnow())
    exp = Experiment(id=exp_id, study_id=study_id, title="e", created_at=utcnow())
    configs = {
        item.id: item
        for item in [
            Configuration(id=uuid.uuid4(), experiment_id=exp_id, label=f"c-{i}", parameters={},
                          config_hash=f"h-{i}", created_at=utcnow())
            for i in range(501)
        ]
    }
    runs = {uuid.uuid4(): Run(id=uuid.uuid4(), study_id=study_id, experiment_id=exp_id, configuration_id=cfg_id,
           status="succeeded", created_at=utcnow()) for _ in range(501)}
    # Keep dictionary keys and ORM ids aligned.
    runs = {r.id: r for r in runs.values()}
    results = {
        result.id: result
        for result in [
            BestResult(id=uuid.uuid4(), study_id=study_id, experiment_id=exp_id,
                       configuration_id=next(iter(configs)), run_id=next(iter(runs)),
                       objective="maximize", primary_metric="score", rationale="fixture", status="proposed")
            for _ in range(501)
        ]
    }
    db = _MockQueryDB({Study: {study_id: study}, Experiment: {exp_id: exp}, Configuration: configs,
                       Run: runs, BestResult: results, Node: {}, Relation: {}, Event: {}})
    first = await service.snapshot(db, study, offset=0, limit=500)
    tail = await service.snapshot(db, study, offset=500, limit=500)
    assert len(first["runs"]) == 500
    assert len(first["best_results"]) == 500
    assert len(first["configurations"]) == 500
    assert first["pagination"]["has_more"] is True
    assert first["pagination"]["has_more_by_collection"]["best_results"] is True
    assert first["pagination"]["has_more_by_collection"]["configurations"] is True
    assert first["pagination"]["totals"]["configurations"] == 501
    assert len(tail["runs"]) == 1
    assert len(tail["best_results"]) == 1
    assert tail["pagination"]["has_more"] is False
    assert tail["pagination"]["totals"]["runs"] == 501
    assert tail["pagination"]["totals"]["best_results"] == 501
    assert tail["pagination"]["totals"]["configurations"] == 0


@pytest.mark.asyncio
async def test_batch_continues_when_individual_run_fails():
    """An individual run failure must not cancel unstarted batch siblings."""
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg1_id = uuid.uuid4()
    cfg2_id = uuid.uuid4()
    run1_id = uuid.uuid4()
    run2_id = uuid.uuid4()

    study = Study(id=study_id, scope="personal", title="Study", created_at=utcnow())
    exp = Experiment(id=exp_id, study_id=study_id, title="Exp", budget={"max_total_seconds": 100}, created_at=utcnow())
    cfg1 = Configuration(id=cfg1_id, experiment_id=exp_id, label="C1", parameters={}, config_hash="h1", created_at=utcnow())
    cfg2 = Configuration(id=cfg2_id, experiment_id=exp_id, label="C2", parameters={}, config_hash="h2", created_at=utcnow())
    run1 = Run(id=run1_id, experiment_id=exp_id, configuration_id=cfg1_id, study_id=study_id,
               adapter_id="mock", status="queued", created_at=utcnow())
    run2 = Run(id=run2_id, experiment_id=exp_id, configuration_id=cfg2_id, study_id=study_id,
               adapter_id="mock", status="queued", created_at=utcnow())

    executed = []
    async def mock_execute(rid, **kwargs):
        executed.append(rid)
        if rid == run1_id:
            run1.status = "failed"
            run1.error_code = "adapter_error"
            return run1
        else:
            run2.status = "succeeded"
            return run2

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "execute_run_instance", mock_execute)
        mp.setattr(runner, "_finish_experiment_if_terminal", AsyncMock(return_value=True))

        class MockEnterAcceptedWork:
            async def __aenter__(self):
                return True
            async def __aexit__(self, *args):
                pass

        mp.setattr("ttakkari.generation.enter_accepted_work", lambda *args, **kwargs: MockEnterAcceptedWork())

        owner = MagicMock()
        owner.metadata = {"generation_id": str(uuid.uuid4()), "token": "tok"}

        db = _MockQueryDB({
            Study: {study_id: study},
            Experiment: {exp_id: exp},
            Configuration: {cfg1_id: cfg1, cfg2_id: cfg2},
            Run: {run1_id: run1, run2_id: run2},
        })
        sm = _MockSessionManager(db)
        mp.setattr(runner, "sessionmaker", lambda: sm)

        await runner._run_owned_batch(exp_id, [run1_id, run2_id], None, owner)

    assert executed == [run1_id, run2_id], "Both runs must execute; run1 failure must not cancel run2"
    assert run1.status == "failed"
    assert run2.status == "succeeded"
