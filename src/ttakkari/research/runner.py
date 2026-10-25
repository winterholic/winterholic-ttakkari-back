"""Background runner and execution lifecycle for Research Lab v2 experiments."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update

from ttakkari import generation
from ttakkari.db import sessionmaker
from ttakkari.models import utcnow
from ttakkari.research.models import (
    Configuration,
    Experiment,
    Measurement,
    ResearchArtifact,
    Run,
    Study,
)

logger = logging.getLogger(__name__)

# In-memory cancel events and background task registry
_CANCEL_EVENTS: dict[uuid.UUID, asyncio.Event] = {}
_ACTIVE_TASKS: dict[uuid.UUID, asyncio.Task] = {}

def _get_registry():
    try:
        from ttakkari.research.adapters.registry import get_adapter_registry
        return get_adapter_registry()
    except ImportError:
        return None


async def cancel_run(run_id: uuid.UUID, reason: str = "user_cancelled") -> bool:
    """Signal cancellation to a running or queued run."""
    ev = _CANCEL_EVENTS.get(run_id)
    if ev:
        ev.set()

    # Update DB status
    sm = sessionmaker()
    async with sm() as db:
        run = await db.get(Run, run_id)
        if run and run.status in ("queued", "running"):
            run.status = "cancelled"
            run.error_code = "cancelled"
            run.error_message = reason
            run.finished_at = utcnow()
            await db.commit()
            return True
    return False


async def execute_run_instance(run_id: uuid.UUID, adapter_registry: Any = None) -> Run:
    """Execute a single run through its designated ExecutionAdapter."""
    reg = adapter_registry or _get_registry()
    cancel_event = asyncio.Event()
    _CANCEL_EVENTS[run_id] = cancel_event

    sm = sessionmaker()
    async with sm() as db:
        run = await db.get(Run, run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        config = await db.get(Configuration, run.configuration_id)
        study = await db.get(Study, run.study_id)
        if not config or not study:
            raise ValueError(f"Configuration or Study missing for run {run_id}")

        if cancel_event.is_set():
            run.status = "cancelled"
            run.error_code = "cancelled"
            run.error_message = "Cancelled before start"
            run.finished_at = utcnow()
            await db.commit()
            return run

        run.status = "running"
        run.started_at = utcnow()
        await db.commit()

        # Find adapter
        adapter = None
        if reg is not None and hasattr(reg, "get_adapter"):
            adapter = reg.get_adapter(run.adapter_id)

        if adapter is None:
            run.status = "failed"
            run.error_code = "adapter_not_found"
            run.error_message = f"실행 어댑터 '{run.adapter_id}'가 레지스트리에 등록되어 있지 않습니다."
            run.finished_at = utcnow()
            await db.commit()
            return run

        start_time = asyncio.get_event_loop().time()
        try:
            adapter_res = await adapter.execute_run(
                run_id=run.id,
                config=config.parameters,
                scope=study.scope,
                cancel_event=cancel_event,
            )
            elapsed = asyncio.get_event_loop().time() - start_time

            run.status = adapter_res.status
            run.raw_output = adapter_res.raw_output or {}
            run.environment_snapshot = adapter_res.environment_snapshot or {}
            run.error_code = adapter_res.error_code
            run.error_message = adapter_res.error_message
            run.wall_seconds = adapter_res.wall_seconds or elapsed
            run.finished_at = utcnow()

            if adapter_res.status == "succeeded":
                # Convert measurements and insert immutably
                measurements = adapter.to_measurements(adapter_res, config.parameters)
                for m in measurements:
                    m_row = Measurement(
                        id=uuid.uuid4(),
                        run_id=run.id,
                        experiment_id=run.experiment_id,
                        configuration_id=run.configuration_id,
                        metric_key=m.metric_key,
                        metric_label=m.metric_label,
                        value=float(m.value),
                        unit=m.unit,
                        direction=m.direction,
                        is_proxy=m.is_proxy,
                        aggregator=m.aggregator,
                        slice_key=m.slice_key,
                        meta=m.meta,
                        created_at=utcnow(),
                    )
                    db.add(m_row)

                # Connect any artifacts produced
                from pathlib import Path
                from ttakkari.artifacts.service import register_file
                from ttakkari.broker.policy import ResolvedPath
                from ttakkari.models import ArtifactArea, ArtifactOrigin

                raw_artifacts = adapter_res.raw_output.get("artifacts", [])
                result_artifacts = getattr(adapter_res, "artifacts", []) or []
                all_artifacts = list(result_artifacts)
                if isinstance(raw_artifacts, list):
                    all_artifacts.extend(raw_artifacts)

                registered_paths: dict[Path, uuid.UUID] = {}
                linked_artifact_ids: set[uuid.UUID] = set()

                for art_info in all_artifacts:
                    if not isinstance(art_info, dict):
                        continue
                    file_art_id = None
                    if "artifact_id" in art_info and art_info["artifact_id"]:
                        file_art_id = uuid.UUID(str(art_info["artifact_id"]))
                    elif "path" in art_info and art_info["path"]:
                        raw_p = Path(art_info["path"])
                        if not raw_p.is_file():
                            logger.error(
                                "Declared artifact file missing on disk for run %s: %s",
                                run.id,
                                raw_p,
                            )
                            await db.rollback()
                            run = await db.get(Run, run_id)
                            if run:
                                run.status = "failed"
                                run.error_code = "artifact_file_missing"
                                run.error_message = f"선언된 아티팩트 파일이 디스크에 존재하지 않습니다: {raw_p}"
                                run.wall_seconds = adapter_res.wall_seconds or elapsed
                                run.finished_at = utcnow()
                                await db.commit()
                            return run

                        norm_path = raw_p.resolve()
                        if norm_path in registered_paths:
                            file_art_id = registered_paths[norm_path]
                        else:
                            cat = art_info.get("category") or "research_tts_sample"
                            reg_file = await register_file(
                                db,
                                ResolvedPath(root=raw_p.parent, path=raw_p),
                                source="research_run",
                                area=ArtifactArea.agent,
                                origin=ArtifactOrigin.agent,
                                category=cat,
                                workspace_id=None,
                                session_id=None,
                                run_id=None,
                                force_copy=True,
                            )
                            file_art_id = reg_file.id
                            registered_paths[norm_path] = file_art_id
                        art_info["artifact_id"] = str(file_art_id)

                    if file_art_id is not None:
                        if file_art_id in linked_artifact_ids:
                            continue
                        linked_artifact_ids.add(file_art_id)

                        role = (
                            art_info.get("role")
                            or art_info.get("category")
                            or ("audio_sample" if art_info.get("kind") == "audio" else "run_artifact")
                        )
                        label = art_info.get("label") or art_info.get("filename") or f"Run {run.id} Artifact"
                        art_row = ResearchArtifact(
                            id=uuid.uuid4(),
                            study_id=run.study_id,
                            experiment_id=run.experiment_id,
                            run_id=run.id,
                            artifact_id=file_art_id,
                            role=role,
                            label=label,
                            promoted_to_library=False,
                            created_at=utcnow(),
                        )
                        db.add(art_row)

            await db.commit()
            return run

        except asyncio.CancelledError:
            run.status = "cancelled"
            run.error_code = "cancelled"
            run.error_message = "Task cancelled by system"
            run.finished_at = utcnow()
            await db.commit()
            return run
        except Exception as e:
            logger.exception("Error executing run %s", run_id)
            elapsed = asyncio.get_event_loop().time() - start_time
            run.status = "failed"
            run.error_code = type(e).__name__
            run.error_message = str(e)
            run.wall_seconds = elapsed
            run.finished_at = utcnow()
            await db.commit()
            return run
        finally:
            _CANCEL_EVENTS.pop(run_id, None)


async def run_experiment_batch(
    experiment_id: uuid.UUID,
    run_ids: list[uuid.UUID],
    adapter_registry: Any = None,
) -> None:
    """Execute a batch of runs under generation work context and auto-analyze."""
    async with generation.enter_accepted_work("research") as acquired:
        sm = sessionmaker()
        if not acquired:
            logger.warning(
                "Generation rejected accepted work gate for experiment %s", experiment_id
            )
            async with sm() as db:
                exp = await db.get(Experiment, experiment_id)
                if exp:
                    exp.state = "cancelled"
                    exp.finished_at = utcnow()
                    exp.receipt = {
                        "error_code": "GENERATION_REJECTED",
                        "error_message": "Generation accepted-work gate rejected (not active or draining)",
                    }
                for rid in run_ids:
                    r = await db.get(Run, rid)
                    if r and r.status == "queued":
                        r.status = "cancelled"
                        r.error_code = "GENERATION_REJECTED"
                        r.error_message = "Generation accepted-work gate rejected (not active or draining)"
                        r.finished_at = utcnow()
                await db.commit()
            return
        # Mark experiment as running
        async with sm() as db:
            exp = await db.get(Experiment, experiment_id)
            if exp:
                exp.state = "running"
                await db.commit()

        for rid in run_ids:
            try:
                run = await execute_run_instance(rid, adapter_registry=adapter_registry)
                if run.status == "cancelled":
                    logger.info("Batch stopped due to run %s cancellation", rid)
                    break
            except Exception as e:
                logger.exception("Failed to execute run instance %s", rid)

        # Update experiment state to completed or needs_verification
        async with sm() as db:
            exp = await db.get(Experiment, experiment_id)
            if exp:
                runs_res = (
                    await db.execute(
                        select(Run.status).where(Run.experiment_id == experiment_id)
                    )
                ).scalars().all()
                if all(s == "succeeded" for s in runs_res):
                    exp.state = "completed"
                elif any(s == "failed" for s in runs_res):
                    exp.state = "failed"
                elif any(s == "cancelled" for s in runs_res):
                    exp.state = "cancelled"
                else:
                    exp.state = "completed"
                exp.finished_at = utcnow()
                await db.commit()

        # Trigger auto-analysis
        try:
            from ttakkari.research.analyzer import analyze_experiment

            async with sm() as db:
                await analyze_experiment(db, experiment_id=experiment_id)
                await db.commit()
        except Exception as e:
            logger.warning("Auto-analysis failed for experiment %s: %s", experiment_id, e)


async def start_experiment_runs(
    db,
    experiment_id: uuid.UUID,
    configuration_ids: list[uuid.UUID] | None = None,
    background: bool = True,
    adapter_registry: Any = None,
) -> list[Run]:
    """Create run records for configurations and dispatch execution."""
    exp = await db.get(Experiment, experiment_id)
    if not exp:
        raise ValueError(f"Experiment {experiment_id} not found")

    # External reports are recorded through the external-report API with an
    # external origin; they must never be dispatched as locally measured runs.
    if exp.adapter_id == "external_report":
        raise HTTPException(
            422,
            "external_report_experiments_are_not_locally_executable: 외부 결과는 /external-report API로 등록하세요.",
        )

    # Persisted definitions and direct /run requests must pass the same local
    # execution checks as the planner preview before any Run rows are created.
    from ttakkari.research.planner import validate_experiment_plan

    study = await db.get(Study, exp.study_id)
    plan = await validate_experiment_plan(
        db,
        study_id=exp.study_id,
        method_id=exp.method_id,
        compared_method_ids=[uuid.UUID(str(value)) for value in (exp.compared_method_ids or [])],
        adapter_id=exp.adapter_id,
        strategy=exp.strategy,
        search_space=exp.search_space or {},
        fixed_inputs=exp.fixed_inputs or {},
        budget=exp.budget or {},
        scope=study.scope if study else "personal",
    )
    if not plan.valid:
        raise HTTPException(
            422,
            "experiment_plan_not_executable: " + "; ".join(plan.errors),
        )

    q = select(Configuration).where(Configuration.experiment_id == experiment_id)
    if configuration_ids:
        q = q.where(Configuration.id.in_(configuration_ids))
    q = q.order_index(Configuration.order_index) if hasattr(q, "order_index") else q.order_by(Configuration.order_index)
    configs = (await db.execute(q)).scalars().all()

    if not configs:
        raise ValueError(f"No configurations found for experiment {experiment_id}")

    created_runs: list[Run] = []
    repetitions = 1
    if exp.strategy == "repeat":
        budget = exp.budget or {}
        repetitions = max(1, min(10, int(budget.get("repetitions", 2))))

    for cfg in configs:
        for r_idx in range(1, repetitions + 1):
            run_row = Run(
                id=uuid.uuid4(),
                experiment_id=exp.id,
                configuration_id=cfg.id,
                study_id=exp.study_id,
                adapter_id=exp.adapter_id,
                execution_origin="local_measured",
                repeat_index=r_idx,
                seed=42 + (r_idx - 1),
                status="queued",
                progress=None,
                environment_snapshot={},
                raw_output={},
                created_at=utcnow(),
            )
            db.add(run_row)
            created_runs.append(run_row)

    await db.commit()

    run_ids = [r.id for r in created_runs]
    if background:
        task = asyncio.create_task(
            run_experiment_batch(
                experiment_id, run_ids, adapter_registry=adapter_registry
            )
        )
        _ACTIVE_TASKS[experiment_id] = task
    else:
        # Synchronous execution for tests / quick evaluations
        await run_experiment_batch(
            experiment_id, run_ids, adapter_registry=adapter_registry
        )

    return created_runs
