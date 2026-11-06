"""Selected-run linkage in exported Research Lab reports."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from ttakkari.db import sessionmaker
from ttakkari.models import Artifact
from ttakkari.research import service
from ttakkari.research.models import (
    BestResult,
    Configuration,
    Experiment,
    Measurement,
    Run,
    Study,
)


async def _report_with_adopted_run(
    monkeypatch, *, selected_measurement: bool, stale_selected_measurement: bool = False
):
    study = Study(title="Report linkage", goal="Synthetic report probe", scope="personal")
    async with sessionmaker()() as db:
        db.add(study)
        await db.flush()
        experiment = Experiment(
            study_id=study.id,
            title="Same configuration repeats",
            hypothesis="Highest measured value is adopted",
            rationale="Fixture",
            adapter="synthetic",
            adapter_id="synthetic",
            primary_metric="score",
            state="completed",
        )
        db.add(experiment)
        await db.flush()
        config = Configuration(
            experiment_id=experiment.id,
            label="same-config",
            config_hash=uuid.uuid4().hex,
            parameters={},
        )
        db.add(config)
        await db.flush()
        low_run = Run(
            experiment_id=experiment.id,
            study_id=study.id,
            configuration_id=config.id,
            adapter_id="synthetic",
            status="succeeded",
            wall_seconds=1.0,
        )
        best_run = Run(
            experiment_id=experiment.id,
            study_id=study.id,
            configuration_id=config.id,
            adapter_id="synthetic",
            status="succeeded",
            wall_seconds=1.0,
        )
        db.add_all([low_run, best_run])
        await db.flush()
        measurements = [
            Measurement(
                run_id=low_run.id,
                experiment_id=experiment.id,
                configuration_id=config.id,
                metric_key="score",
                metric_label="Score",
                value=0.3,
                unit="points",
                direction="higher_is_better",
                is_proxy=False,
                aggregator="single",
                slice_key="overall",
                meta={},
            )
        ]
        if selected_measurement:
            measurements.append(
                Measurement(
                    run_id=best_run.id,
                    experiment_id=experiment.id,
                    configuration_id=config.id,
                    metric_key="score",
                    metric_label="Score",
                    value=0.9,
                    unit="points",
                    direction="higher_is_better",
                    is_proxy=False,
                    aggregator="single",
                    slice_key="overall",
                    meta={},
                )
            )
        if stale_selected_measurement:
            stale_config = Configuration(
                experiment_id=experiment.id,
                label="stale-config",
                config_hash=uuid.uuid4().hex,
                parameters={},
            )
            db.add(stale_config)
            await db.flush()
            measurements.append(
                Measurement(
                    run_id=best_run.id,
                    experiment_id=experiment.id,
                    configuration_id=stale_config.id,
                    metric_key="score",
                    metric_label="Score",
                    value=0.7,
                    unit="points",
                    direction="higher_is_better",
                    is_proxy=False,
                    aggregator="single",
                    slice_key="overall",
                    meta={},
                )
            )
        db.add_all(measurements)
        best = BestResult(
            study_id=study.id,
            experiment_id=experiment.id,
            configuration_id=config.id,
            run_id=best_run.id,
            status="adopted",
            objective="maximize score",
            primary_metric="score",
            rationale="Measured 0.9",
        )
        db.add(best)
        await db.flush()
        study.best_result_id = best.id
        await db.commit()
        study_id = study.id
        ids = (str(experiment.id), str(config.id), str(best_run.id), str(low_run.id))

    captured = {}

    async def capture_report(db, resolved_path, **kwargs):
        body = resolved_path.path.read_text(encoding="utf-8")
        captured["body"] = body
        artifact_id = uuid.uuid4()
        db.add(
            Artifact(
                id=artifact_id,
                filename=resolved_path.path.name,
                mime_type="text/markdown",
                kind="file",
                source="research_report",
                original_path=str(resolved_path.path),
                rel_path=None,
                storage_mode="inline",
                stored_path="synthetic-report",
                size_bytes=len(body),
                sha256="0" * 64,
            )
        )
        return SimpleNamespace(id=artifact_id)

    monkeypatch.setattr("ttakkari.artifacts.service.register_file", capture_report)
    async with sessionmaker()() as db:
        result = await service.export_study_report(db, study_id)
    return captured["body"], ids, result


@pytest.mark.asyncio
async def test_exported_adoption_identifies_selected_run_and_its_measurement(monkeypatch):
    body, (experiment_id, configuration_id, selected_run_id, other_run_id), result = (
        await _report_with_adopted_run(monkeypatch, selected_measurement=True)
    )
    adopted = body.split("## 채택된 최적 결과물", 1)[1].split("## 실험 및 측정 요약", 1)[0]

    assert experiment_id in adopted
    assert configuration_id in adopted
    assert selected_run_id in adopted
    assert "0.9000 points" in adopted
    assert "0.3000 points" not in adopted
    assert "| Run ID |" in body
    assert f"| {selected_run_id} |" in body
    assert f"| {other_run_id} |" in body
    assert result["artifact"]["role"] == "report_markdown"


@pytest.mark.asyncio
async def test_exported_adoption_does_not_fall_back_to_another_run_measurement(monkeypatch):
    body, (_, _, selected_run_id, _), _ = await _report_with_adopted_run(
        monkeypatch, selected_measurement=False
    )
    adopted = body.split("## 채택된 최적 결과물", 1)[1].split("## 실험 및 측정 요약", 1)[0]

    assert selected_run_id in adopted
    assert "미확인" in adopted
    assert "0.3000 points" not in adopted


@pytest.mark.asyncio
async def test_exported_adoption_ignores_stale_measurement_configuration(monkeypatch):
    body, (_, _, selected_run_id, _), _ = await _report_with_adopted_run(
        monkeypatch, selected_measurement=False, stale_selected_measurement=True
    )
    adopted = body.split("## 채택된 최적 결과물", 1)[1].split("## 실험 및 측정 요약", 1)[0]
    selected_row = next(line for line in body.splitlines() if line.startswith(f"| {selected_run_id} |"))

    assert "미확인" in adopted
    assert "0.7000 points" not in adopted
    assert "0.7000 points" not in selected_row
