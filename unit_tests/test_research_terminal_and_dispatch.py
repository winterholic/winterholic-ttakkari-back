"""DB-free tests reproducing Research Lab execution, retry completion, and agent dispatch defects."""
from __future__ import annotations

import uuid
from typing import get_args, get_type_hints

import pytest

from ttakkari.api.research import AgentIn
from ttakkari.research import runner, service
from ttakkari.research.models import (
    BestResult,
    Event,
    Experiment,
    Node,
    Relation,
    Run,
    Study,
)


class _MockQueryDB:
    """In-memory mock supporting get, scalar, scalars, execute, add, commit."""

    def __init__(self, data: dict):
        self.data = data
        self.committed = []

    async def get(self, model, ident, **kwargs):
        table = self.data.get(model, {})
        return table.get(ident)

    async def scalar(self, query):
        str_q = str(query)
        # Handle select(func.count()).select_from(cls)
        for cls, name in [
            (Run, "research_runs"),
            (BestResult, "research_best_results"),
            (Node, "research_nodes"),
            (Relation, "research_relations"),
            (Experiment, "research_experiments"),
            (Event, "research_events"),
        ]:
            if name in str_q and "count" in str_q:
                return len(self.data.get(cls, {}))
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
            (Run, "research_runs"),
            (BestResult, "research_best_results"),
            (Node, "research_nodes"),
            (Relation, "research_relations"),
            (Experiment, "research_experiments"),
            (Event, "research_events"),
        ]:
            if name in str_q:
                items = list(self.data.get(cls, {}).values())
                return self._ScalarsWrapper(items)
        return self._ScalarsWrapper([])

    async def execute(self, query):
        return self._ScalarsWrapper([])

    def add(self, obj):
        pass

    async def commit(self):
        pass

    async def flush(self):
        pass


@pytest.mark.asyncio
async def test_finish_experiment_recovers_to_completed_when_retry_succeeds():
    """Defect reproduction: An experiment with a failed run whose retry succeeded

    must become 'completed', not stay 'failed'.
    """
    exp_id = uuid.uuid4()
    study_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run1_id = uuid.uuid4()
    run2_id = uuid.uuid4()

    exp = Experiment(
        id=exp_id,
        study_id=study_id,
        title="Retry recovery test",
        strategy="single",
        adapter="retrieval_eval",
        state="running",
    )

    run1 = Run(
        id=run1_id,
        experiment_id=exp_id,
        configuration_id=cfg_id,
        study_id=study_id,
        adapter_id="retrieval_eval",
        execution_origin="local_measured",
        repeat_index=1,
        seed=42,
        status="failed",
        error_code="connection_failed",
        error_message="timeout",
        environment_snapshot={},
    )

    # Run 2 is a successful retry of Run 1
    run2 = Run(
        id=run2_id,
        experiment_id=exp_id,
        configuration_id=cfg_id,
        study_id=study_id,
        adapter_id="retrieval_eval",
        execution_origin="local_measured",
        repeat_index=1,
        seed=42,
        status="succeeded",
        environment_snapshot={
            "research_retry": {
                "contract": 1,
                "retry_of_run_id": str(run1_id),
            }
        },
    )

    db = _MockQueryDB({
        Experiment: {exp_id: exp},
        Run: {run1_id: run1, run2_id: run2},
    })

    # Call _finish_experiment_if_terminal
    finished = await runner._finish_experiment_if_terminal(db, exp_id)
    assert finished is True
    # If the bug exists, exp.state will be "failed" because run1.status == "failed" is in statuses!
    # Expected correct behavior: superseded run1 is ignored, so exp.state is "completed".
    assert exp.state == "completed", f"Expected experiment to be completed, but got {exp.state}"


def test_agent_in_allows_run_retry_and_run_start_and_external_report():
    """Defect reproduction: AgentIn schema must accept run_retry, run_start, and external_report."""
    run_id = uuid.uuid4()
    exp_id = uuid.uuid4()

    # If the bug exists, these will raise ValidationError!
    p_retry = AgentIn(operation="run_retry", run_id=run_id)
    assert p_retry.operation == "run_retry"

    p_run = AgentIn(operation="run_start", experiment_id=exp_id)
    assert p_run.operation == "run_start"

    p_ext = AgentIn(operation="external_report", experiment_id=exp_id, data={"provenance": "external"})
    assert p_ext.operation == "external_report"


def test_mcp_manage_research_operation_names_match_agent_api():
    from ttakkari.orchestrator.mcp_server import manage_research

    api_operations = get_args(AgentIn.model_fields["operation"].annotation)
    mcp_operations = get_args(get_type_hints(manage_research)["operation"])
    for operation in ("run", "run_start", "run_retry", "external_report"):
        assert operation in api_operations
        assert operation in mcp_operations


@pytest.mark.asyncio
async def test_snapshot_includes_best_result_and_terminal_runs():
    """Defect reproduction: snapshot must include best_result and runs for resume."""
    study_id = uuid.uuid4()
    exp_id = uuid.uuid4()
    cfg_id = uuid.uuid4()
    run_id = uuid.uuid4()
    best_id = uuid.uuid4()

    study = Study(
        id=study_id,
        title="Resume snapshot test",
        goal="Verify snapshot contains runs and best result",
        scope="personal",
        best_result_id=best_id,
    )

    run = Run(
        id=run_id,
        experiment_id=exp_id,
        configuration_id=cfg_id,
        study_id=study_id,
        adapter_id="retrieval_eval",
        execution_origin="local_measured",
        repeat_index=1,
        seed=42,
        status="succeeded",
        environment_snapshot={},
    )

    best = BestResult(
        id=best_id,
        study_id=study_id,
        experiment_id=exp_id,
        configuration_id=cfg_id,
        run_id=run_id,
        status="adopted",
        objective="max_accuracy",
        primary_metric="recall_at_k",
        rationale="Superior performance",
    )

    db = _MockQueryDB({
        Study: {study_id: study},
        Run: {run_id: run},
        BestResult: {best_id: best},
    })

    snap = await service.snapshot(db, study)
    # Expected: snapshot should include runs and best_result
    assert "best_result" in snap or "best_results" in snap, "snapshot missing best_result"
    assert "runs" in snap, "snapshot missing runs"
