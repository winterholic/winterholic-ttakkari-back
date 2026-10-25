"""Unit and isolated integration regression tests for Stage 2 defect fixes.

Covers:
1. planner.validate_experiment_plan fail-closed behavior on unknown adapter or validator exceptions,
   while preserving valid configs, sweeps, and external_report draft designs.
2. ExternalReportAdapter validation of required source_title and source_url for external_benchmark,
   while permitting manual_evaluation and imported_log without external source requirements,
   and keeping source_year optional.
3. record_external_report endpoint integration with Adapter validation and roundtrip persistence
   of source metadata to raw_output, environment_snapshot, measurements, and top-level run output.
4. Read backward compatibility for historical and migrated v1 records.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
import pytest
from fastapi import HTTPException

import ttakkari.research.adapters as adapters
import ttakkari.research.planner as planner
from ttakkari.api.research import ExternalReportIn, record_external_report
from ttakkari.research.adapters.external_report import ExternalReportAdapter
from ttakkari.research.models import Experiment, Study
import ttakkari.research.service as svc


@pytest.fixture(autouse=True)
def restore_adapter_registry_lookup():
    """These probes replace a module lookup; do not leak the stub into later E2E tests."""
    original = adapters.get_adapter_registry
    yield
    adapters.get_adapter_registry = original


class StubDb:
    """In-memory database stub simulating async SQLAlchemy session for research entities."""

    def __init__(self, entities: dict[type, dict[Any, Any]] | None = None):
        self.entities: dict[type, dict[Any, Any]] = entities or {}
        self.added: list[Any] = []
        self.flushed: list[Any] = []
        self.committed = False

    async def get(self, model: type, ident: Any):
        return self.entities.get(model, {}).get(ident)

    def add(self, obj: Any):
        self.added.append(obj)
        model = type(obj)
        if model not in self.entities:
            self.entities[model] = {}
        if hasattr(obj, "id"):
            self.entities[model][obj.id] = obj

    async def flush(self):
        self.flushed.extend(self.added)

    async def commit(self):
        self.committed = True

    async def execute(self, query: Any):
        class Result:
            def scalars(self):
                class Scalars:
                    def all(self):
                        return []
                return Scalars()
        return Result()


class ExplodingValidator:
    def validate_config(self, params: dict[str, Any], *, scope: str) -> list[str]:
        raise RuntimeError("synthetic validator failure")


class MockRegistry:
    def __init__(self, adapter_map: dict[str, Any]):
        self.adapter_map = adapter_map

    def get_adapter(self, adapter_id: str):
        return self.adapter_map.get(adapter_id)


# ---------------------------------------------------------------------------
# Defect 1: Planner fail-closed on adapter errors / exceptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_rejects_unknown_adapter():
    """Unknown adapter must fail-closed with valid=False and meaningful error message."""
    adapters.get_adapter_registry = lambda: MockRegistry({})
    result = await planner.validate_experiment_plan(
        StubDb(),
        study_id=uuid.uuid4(),
        method_id=None,
        compared_method_ids=[],
        adapter_id="nonexistent_adapter_xyz",
        strategy="single",
        search_space={},
        fixed_inputs={},
        budget={"max_runs": 2},
    )
    assert result.valid is False
    assert any("알 수 없는 실행 어댑터입니다: 'nonexistent_adapter_xyz'" in err for err in result.errors)


@pytest.mark.asyncio
async def test_planner_rejects_validator_exception():
    """Validator exception in adapter.validate_config must fail-closed with valid=False."""
    adapters.get_adapter_registry = lambda: MockRegistry({"llm_inference": ExplodingValidator()})
    result = await planner.validate_experiment_plan(
        StubDb(),
        study_id=uuid.uuid4(),
        method_id=None,
        compared_method_ids=[],
        adapter_id="llm_inference",
        strategy="single",
        search_space={},
        fixed_inputs={},
        budget={"max_runs": 2},
    )
    assert result.valid is False
    assert any("어댑터 'llm_inference' 설정 검증 중 오류가 발생했습니다: RuntimeError" in err for err in result.errors)


@pytest.mark.asyncio
async def test_planner_preserves_valid_config_and_sweeps():
    """Valid config and valid parameter sweeps must remain valid=True."""
    class ValidAdapter:
        def validate_config(self, params: dict[str, Any], *, scope: str) -> list[str]:
            if params.get("n_ctx", 1) <= 0:
                return ["n_ctx must be positive"]
            return []

    adapters.get_adapter_registry = lambda: MockRegistry({"llm_inference": ValidAdapter()})
    # Valid sweep
    valid_res = await planner.validate_experiment_plan(
        StubDb(),
        study_id=uuid.uuid4(),
        method_id=None,
        compared_method_ids=[],
        adapter_id="llm_inference",
        strategy="grid",
        search_space={"n_ctx": [16, 32]},
        fixed_inputs={},
        budget={"max_runs": 5},
    )
    assert valid_res.valid is True
    assert len(valid_res.errors) == 0

    # Invalid sweep member
    invalid_res = await planner.validate_experiment_plan(
        StubDb(),
        study_id=uuid.uuid4(),
        method_id=None,
        compared_method_ids=[],
        adapter_id="llm_inference",
        strategy="grid",
        search_space={"n_ctx": [16, 0]},
        fixed_inputs={},
        budget={"max_runs": 5},
    )
    assert invalid_res.valid is False
    assert "n_ctx must be positive" in invalid_res.errors


@pytest.mark.asyncio
async def test_planner_allows_external_report_draft_design_without_sources():
    """External report draft designs are hypothesis proposals, not local runs; they stay valid without sources."""
    result = await planner.validate_experiment_plan(
        StubDb(),
        study_id=uuid.uuid4(),
        method_id=None,
        compared_method_ids=[],
        adapter_id="external_report",
        strategy="single",
        search_space={},
        fixed_inputs={},
        budget={"max_runs": 2},
    )
    assert result.valid is True
    assert len(result.errors) == 0


# ---------------------------------------------------------------------------
# Defect 2: ExternalReportAdapter source validation for external_benchmark
# ---------------------------------------------------------------------------


def test_external_report_adapter_rejects_missing_or_blank_sources():
    """external_benchmark requires nonempty title and locator (URL), but year is optional."""
    adapter = ExternalReportAdapter()
    base_measurements = [{"metric_key": "decode_tps", "value": 185.4}]

    # 1. Missing both title and url
    err1 = adapter.validate_config(
        {"provenance": "external_benchmark", "measurements": base_measurements},
        scope="personal",
    )
    assert any("출처 제목(source_title)" in e for e in err1)
    assert any("출처 위치(source_url 또는 locator)" in e for e in err1)

    # 2. Blank title and url
    err2 = adapter.validate_config(
        {
            "provenance": "external_benchmark",
            "source_title": "   ",
            "source_url": "\t\n",
            "measurements": base_measurements,
        },
        scope="personal",
    )
    assert any("출처 제목(source_title)" in e for e in err2)
    assert any("출처 위치(source_url 또는 locator)" in e for e in err2)

    # 3. Only title present, url missing
    err3 = adapter.validate_config(
        {
            "provenance": "external_benchmark",
            "source_title": "FlashInfer Benchmark Table 2",
            "measurements": base_measurements,
        },
        scope="personal",
    )
    assert not any("출처 제목" in e for e in err3)
    assert any("출처 위치(source_url 또는 locator)" in e for e in err3)

    # 4. Only url present, title missing
    err4 = adapter.validate_config(
        {
            "provenance": "external_benchmark",
            "source_url": "https://arxiv.org/abs/2402.16329",
            "measurements": base_measurements,
        },
        scope="personal",
    )
    assert any("출처 제목(source_title)" in e for e in err4)
    assert not any("출처 위치" in e for e in err4)

    # 5. Both title and url present -> valid (year optional)
    err5 = adapter.validate_config(
        {
            "provenance": "external_benchmark",
            "source_title": "FlashInfer Benchmark Table 2",
            "source_url": "https://arxiv.org/abs/2402.16329",
            "measurements": base_measurements,
        },
        scope="personal",
    )
    assert err5 == []

    # 6. Both present via locator alias and nested raw_output -> valid
    err6 = adapter.validate_config(
        {
            "provenance": "external_benchmark",
            "raw_output": {
                "source_title": "FlashInfer Benchmark Table 2",
                "source_locator": "https://arxiv.org/abs/2402.16329",
            },
            "measurements": base_measurements,
        },
        scope="personal",
    )
    assert err6 == []


def test_external_report_adapter_permits_manual_and_imported_without_sources():
    """manual_evaluation and imported_log do not require external citation sources."""
    adapter = ExternalReportAdapter()
    base_measurements = [{"metric_key": "listening_quality", "value": 4.5}]

    err_manual = adapter.validate_config(
        {"provenance": "manual_evaluation", "measurements": base_measurements},
        scope="personal",
    )
    assert err_manual == []

    err_import = adapter.validate_config(
        {"provenance": "imported_log", "measurements": base_measurements},
        scope="personal",
    )
    assert err_import == []


# ---------------------------------------------------------------------------
# Defect 2 Integration: record_external_report endpoint validation & roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_external_report_endpoint_validates_and_roundtrips():
    """record_external_report must apply adapter validation and persist sources to run output."""
    study_id = uuid.uuid4()
    study = Study(
        id=study_id,
        title="Test Study",
        goal="Benchmark comparison",
        scope="personal",
    )
    exp_id = uuid.uuid4()
    exp = Experiment(
        id=exp_id,
        study_id=study_id,
        title="Test Exp",
        hypothesis="A100 claimed throughput",
        rationale="Comparison against paper",
        adapter="external_report",
        adapter_id="external_report",
        strategy="single",
        primary_metric="decode_tps",
        baseline_configuration_id=None,
    )
    db = StubDb({Study: {study_id: study}, Experiment: {exp_id: exp}})

    # 1. external_benchmark without source must be rejected with 422
    body_missing = ExternalReportIn(
        provenance="external_benchmark",
        metrics=[{"metric_key": "decode_tps", "value": 185.4}],
    )
    with pytest.raises(HTTPException) as exc_info:
        await record_external_report(exp_id, body_missing, None, db)
    assert exc_info.value.status_code == 422
    assert "external_report_validation_failed" in exc_info.value.detail
    assert "출처 제목(source_title)" in exc_info.value.detail

    # 2. external_benchmark with title and url must succeed (201) and roundtrip source fields
    body_valid = ExternalReportIn(
        provenance="external_benchmark",
        source_title="FlashInfer Paper Table 2",
        source_url="https://arxiv.org/abs/2402.16329",
        source_year=2024,
        metrics=[{"metric_key": "decode_tps", "value": 185.4, "unit": "tok/s"}],
        raw_output={"note": "A100 claimed throughput"},
        environment_snapshot={"claimed_gpu": "A100-SXM4-80GB"},
    )
    res = await record_external_report(exp_id, body_valid, None, db)
    assert res["status"] == "succeeded"
    assert res["execution_origin"] == "external_report"
    assert res["provenance"] == "external_benchmark"
    assert res["source_title"] == "FlashInfer Paper Table 2"
    assert res["source_url"] == "https://arxiv.org/abs/2402.16329"
    assert res["source_year"] == 2024
    assert res["raw_output"]["source_title"] == "FlashInfer Paper Table 2"
    assert res["raw_output"]["source_url"] == "https://arxiv.org/abs/2402.16329"
    assert res["environment_snapshot"]["source_title"] == "FlashInfer Paper Table 2"
    assert len(res["measurements"]) == 1
    assert res["measurements"][0]["value"] == 185.4
    assert res["measurements"][0]["meta"]["source_title"] == "FlashInfer Paper Table 2"

    # 3. manual_evaluation without sources succeeds
    body_manual = ExternalReportIn(
        provenance="manual_evaluation",
        metrics=[{"metric_key": "mos_score", "value": 4.2}],
    )
    res_manual = await record_external_report(exp_id, body_manual, None, db)
    assert res_manual["status"] == "succeeded"
    assert res_manual["provenance"] == "manual_evaluation"


def test_service_run_out_read_backward_compatibility():
    """run_out must preserve read compatibility for existing records and migrated_v1."""
    class FakeRow:
        __table__ = type("Table", (), {"columns": [
            type("Col", (), {"key": "id"})(),
            type("Col", (), {"key": "execution_origin"})(),
            type("Col", (), {"key": "raw_output"})(),
        ]})
        id = uuid.uuid4()
        execution_origin = "migrated_v1"
        raw_output = {"note": "historical v1 receipt without source"}

    out = svc.run_out(FakeRow())
    assert out["execution_origin"] == "migrated_v1"
    assert "source_title" not in out
    assert out["raw_output"]["note"] == "historical v1 receipt without source"


if __name__ == "__main__":
    import asyncio
    print("Running Stage 2 defects regression test suite...")

    # Planner defect tests
    asyncio.run(test_planner_rejects_unknown_adapter())
    print("  [PASS] test_planner_rejects_unknown_adapter")
    asyncio.run(test_planner_rejects_validator_exception())
    print("  [PASS] test_planner_rejects_validator_exception")
    asyncio.run(test_planner_preserves_valid_config_and_sweeps())
    print("  [PASS] test_planner_preserves_valid_config_and_sweeps")
    asyncio.run(test_planner_allows_external_report_draft_design_without_sources())
    print("  [PASS] test_planner_allows_external_report_draft_design_without_sources")

    # Adapter defect tests
    test_external_report_adapter_rejects_missing_or_blank_sources()
    print("  [PASS] test_external_report_adapter_rejects_missing_or_blank_sources")
    test_external_report_adapter_permits_manual_and_imported_without_sources()
    print("  [PASS] test_external_report_adapter_permits_manual_and_imported_without_sources")

    # Endpoint integration tests
    asyncio.run(test_record_external_report_endpoint_validates_and_roundtrips())
    print("  [PASS] test_record_external_report_endpoint_validates_and_roundtrips")

    # Read backward compatibility
    test_service_run_out_read_backward_compatibility()
    print("  [PASS] test_service_run_out_read_backward_compatibility")

    print("All 8 regression tests passed successfully!")

