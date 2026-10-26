"""Background runner and execution lifecycle for Research Lab v2 experiments."""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update

from ttakkari import generation
from ttakkari.db import sessionmaker
from ttakkari.model_process import worker_memory_limit
from ttakkari.models import utcnow
from ttakkari.research.models import (
    Configuration,
    Event,
    Experiment,
    Measurement,
    ResearchArtifact,
    Run,
    Study,
)
from ttakkari.research.ownership import OWNER_KEY, ExecutionOwner, owner_token, probe

logger = logging.getLogger(__name__)

# In-memory cancel events and background task registry
_CANCEL_EVENTS: dict[uuid.UUID, asyncio.Event] = {}
_ACTIVE_TASKS: dict[uuid.UUID, asyncio.Task] = {}


def _remove_run_copies(paths: list[Path]) -> list[str]:
    """Remove only directories allocated by this artifact transaction."""
    failures = []
    for path in reversed(paths):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Failed to remove rolled-back research artifact copy: %s", path)
            failures.append(str(path))
    return failures


def _get_registry():
    try:
        from ttakkari.research.adapters.registry import get_adapter_registry
        return get_adapter_registry()
    except ImportError:
        return None


def _fence_owner(run: Run, owner: ExecutionOwner) -> None:
    if (run.environment_snapshot or {}).get(OWNER_KEY) != owner.metadata:
        raise generation.OwnershipLost(str(run.id))


async def cancel_run(run_id: uuid.UUID, reason: str = "user_cancelled") -> bool:
    """Signal cancellation to a running or queued run."""
    # Update DB status
    sm = sessionmaker()
    async with sm() as db:
        run = await db.get(Run, run_id, with_for_update=True)
        if run and run.status in ("queued", "running"):
            ev = _CANCEL_EVENTS.get(run_id)
            if ev:
                ev.set()
            run.status = "cancelled"
            run.error_code = "cancelled"
            run.error_message = reason
            run.finished_at = utcnow()
            await db.commit()
            return True
    return False


async def execute_run_instance(
    run_id: uuid.UUID, adapter_registry: Any = None, *, deadline: float | None = None,
    _owner: ExecutionOwner | None = None,
) -> Run:
    """Execute a single run through its designated ExecutionAdapter."""
    reg = adapter_registry or _get_registry()
    sm = sessionmaker()
    async with sm() as db:
        # Claim the queued row once, including across concurrent API requests.
        # Terminal evidence and a live owner's cancellation signal must survive replay.
        run = await db.get(Run, run_id, with_for_update=True)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        if run.status != "queued":
            return run

        # A reserved queued Run belongs to its accepting batch even before its
        # first adapter call. Direct/replayed dispatch must not replace that owner.
        reserved = (run.environment_snapshot or {}).get(OWNER_KEY)
        if reserved is not None and (_owner is None or reserved != _owner.metadata):
            return run

        config = await db.get(Configuration, run.configuration_id)
        study = await db.get(Study, run.study_id)
        if not config or not study:
            raise ValueError(f"Configuration or Study missing for run {run_id}")

        from ttakkari.research.planner import execution_memory_budget

        experiment = await db.get(Experiment, run.experiment_id)
        try:
            memory_mb = execution_memory_budget(experiment.budget or {}, run.adapter_id, config.parameters)
        except ValueError as exc:
            # Admission still holds the queued Run's row lock: a concurrent user
            # cancellation cannot be overwritten after we release that lock.
            run.status = "cancelled"
            run.error_code = "budget_invalid"
            run.error_message = str(exc)
            run.finished_at = utcnow()
            await db.commit()
            return run

        cancel_event = asyncio.Event()
        own_lock = _owner is None
        owner = _owner or ExecutionOwner.acquire()
        run.environment_snapshot = owner.snapshot(run.environment_snapshot)
        _CANCEL_EVENTS[run_id] = cancel_event
        run.status = "running"
        run.started_at = utcnow()
        try:
            await db.commit()
        except BaseException:
            _CANCEL_EVENTS.pop(run_id, None)
            if own_lock:
                owner.close()
            raise

        start_time = asyncio.get_event_loop().time()
        adapter_res = None
        memory_guard = None
        created_copies: list[Path] = []
        execution_timeout = asyncio.timeout_at(deadline)
        from ttakkari.research.resources import (
            RESOURCE_KEY,
            WORKERS_KEY,
            merge_worker_checkpoint,
            resource_checkpoints,
        )

        async def write_resource(resource: dict) -> None:
            async with sm() as checkpoint_db:
                checkpoint_run = await checkpoint_db.get(Run, run_id, with_for_update=True)
                _fence_owner(checkpoint_run, owner)
                snapshot = checkpoint_run.environment_snapshot or {}
                key = WORKERS_KEY if resource.get("kind") == "model_worker" else RESOURCE_KEY
                value = (merge_worker_checkpoint(snapshot, resource, owner.metadata["token"])
                         if key == WORKERS_KEY else {
                             **resource, "contract": 1, "owner_token": owner.metadata["token"],
                         })
                checkpoint_run.environment_snapshot = {**snapshot, key: {**value, "recorded_at": utcnow().isoformat()}}
                await checkpoint_db.commit()

        try:
            adapter = reg.get_adapter(run.adapter_id) if reg is not None and hasattr(reg, "get_adapter") else None
            if adapter is None:
                run.status = "failed"
                run.error_code = "adapter_not_found"
                run.error_message = f"실행 어댑터 '{run.adapter_id}'가 레지스트리에 등록되어 있지 않습니다."
                run.finished_at = utcnow()
                await db.commit()
                return run
            with resource_checkpoints(write_resource), worker_memory_limit(memory_mb) as memory_guard:
                async with execution_timeout:
                    adapter_res = await adapter.execute_run(
                        run_id=run.id,
                        config=config.parameters,
                        scope=study.scope,
                        cancel_event=cancel_event,
                    )
            elapsed = asyncio.get_event_loop().time() - start_time
            if memory_guard is not None and not memory_guard.samples and adapter_res.status == "succeeded":
                memory_guard.error_code = "memory_probe_unavailable"
            budget_exhausted = execution_timeout.expired() or (
                deadline is not None and asyncio.get_running_loop().time() >= deadline
            )

            # Cancellation may come from another request/process while the adapter
            # finishes. Re-read the persisted status before publishing its result.
            await db.refresh(run, with_for_update=True)
            _fence_owner(run, owner)
            cancelled = run.status == "cancelled"
            run.status = "cancelled" if cancelled else adapter_res.status
            run.raw_output = adapter_res.raw_output or {}
            run.environment_snapshot = owner.snapshot(adapter_res.environment_snapshot, previous=run.environment_snapshot)
            if memory_guard is not None:
                run.environment_snapshot = {**run.environment_snapshot,
                    "research_memory_guard": {**memory_guard.snapshot(), "owner_token": owner.metadata["token"]}}
            if not cancelled:
                run.error_code = adapter_res.error_code
                run.error_message = adapter_res.error_message
                if budget_exhausted:
                    # Some adapters return their cleanup evidence after handling
                    # CancelledError. Expiry must still prevent a success result.
                    cancel_event.set()
                    run.status = "cancelled"
                    run.error_code = "budget_exceeded"
                    run.error_message = "Experiment max_total_seconds exhausted"
                elif memory_guard is not None and memory_guard.error_code:
                    run.status = "cancelled"
                    run.error_code = memory_guard.error_code
                    run.error_message = "Model worker RSS guard stopped this run: " + memory_guard.error_code
            run.wall_seconds = elapsed if budget_exhausted else adapter_res.wall_seconds or elapsed
            run.finished_at = utcnow()

            if run.status == "succeeded":
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
                from ttakkari.artifacts.service import register_file
                from ttakkari.broker.policy import ResolvedPath
                from ttakkari.models import Artifact, ArtifactArea, ArtifactOrigin

                raw_artifacts = adapter_res.raw_output.get("artifacts", [])
                result_artifacts = getattr(adapter_res, "artifacts", []) or []
                all_artifacts = list(result_artifacts)
                if isinstance(raw_artifacts, list):
                    all_artifacts.extend(raw_artifacts)

                # Validate every declaration before copying anything: a late failure must not
                # leave orphan copies, and the adapter's output and measurements stay as evidence.
                declared: list[tuple[dict, uuid.UUID | None, Path | None]] = []
                failure: tuple[str, str] | None = None
                for art_info in all_artifacts:
                    if not isinstance(art_info, dict):
                        continue
                    if art_info.get("artifact_id"):
                        try:
                            legacy_id = uuid.UUID(str(art_info["artifact_id"]))
                        except ValueError:
                            failure = ("artifact_invalid", f"아티팩트 ID 형식이 올바르지 않습니다: {art_info['artifact_id']}")
                            break
                        if await db.get(Artifact, legacy_id) is None:
                            failure = ("artifact_not_found", f"선언된 아티팩트가 파일 보관함에 없습니다: {legacy_id}")
                            break
                        declared.append((art_info, legacy_id, None))
                    elif art_info.get("path"):
                        raw_p = Path(art_info["path"])
                        if not raw_p.is_file():
                            logger.error("Declared artifact file missing on disk for run %s: %s", run.id, raw_p)
                            failure = ("artifact_file_missing", f"선언된 아티팩트 파일이 디스크에 존재하지 않습니다: {raw_p}")
                            break
                        declared.append((art_info, None, raw_p))

                if failure is not None:
                    run.status = "failed"
                    run.error_code, run.error_message = failure
                    await db.commit()
                    return run

                # File I/O and DB artifact registration can fail independently of inference.
                # Preserve completed measurements first, without announcing success until
                # every artifact has been linked and the artifact transaction commits.
                run.status = "running"
                run.finished_at = None
                await db.commit()

                registered_paths: dict[Path, uuid.UUID] = {}
                linked_artifact_ids: set[uuid.UUID] = set()

                for art_info, file_art_id, raw_p in declared:
                    if raw_p is not None:
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
                                created_copies=created_copies,
                            )
                            file_art_id = reg_file.id
                            registered_paths[norm_path] = file_art_id

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

                # The measurement checkpoint released our row lock. Honour a
                # cancellation accepted during artifact copying before final commit.
                with db.no_autoflush:
                    await db.refresh(run, ["status", "error_code", "error_message", "environment_snapshot"], with_for_update=True)
                _fence_owner(run, owner)
                if run.status == "cancelled":
                    raise asyncio.CancelledError
                run.status = "succeeded"
                run.finished_at = utcnow()

            await db.commit()
            created_copies.clear()
            return run

        except generation.OwnershipLost:
            await db.rollback()
            _remove_run_copies(created_copies)
            logger.warning("Discarded stale research owner result for Run %s", run_id)
            return await db.get(Run, run_id, populate_existing=True)
        except asyncio.CancelledError:
            await db.rollback()
            cleanup_failures = _remove_run_copies(created_copies)
            run = await db.get(Run, run_id, with_for_update=True, populate_existing=True)
            _fence_owner(run, owner)
            if adapter_res is not None:
                run.raw_output = adapter_res.raw_output or {}
                run.environment_snapshot = owner.snapshot(adapter_res.environment_snapshot, previous=run.environment_snapshot)
            if memory_guard is not None:
                run.environment_snapshot = {**run.environment_snapshot,
                    "research_memory_guard": {**memory_guard.snapshot(), "owner_token": owner.metadata["token"]}}
            run.status = "cancelled"
            run.error_code = "cancelled"
            run.error_message = run.error_message or "Task cancelled by system"
            if cleanup_failures:
                run.error_message += "; artifact cleanup failed: " + ", ".join(cleanup_failures)
            run.finished_at = utcnow()
            await db.commit()
            return run
        except Exception as e:
            if execution_timeout.expired():
                logger.info("Execution time budget exhausted for run %s", run_id)
            else:
                logger.exception("Error executing run %s", run_id)
            elapsed = asyncio.get_event_loop().time() - start_time
            # A failed flush poisons the session; reset it so the run still reaches a terminal state.
            await db.rollback()
            cleanup_failures = _remove_run_copies(created_copies)
            run = await db.get(Run, run_id, with_for_update=True, populate_existing=True)
            _fence_owner(run, owner)
            if adapter_res is not None:
                run.raw_output = adapter_res.raw_output or {}
                run.environment_snapshot = owner.snapshot(adapter_res.environment_snapshot, previous=run.environment_snapshot)
            if memory_guard is not None:
                run.environment_snapshot = {**run.environment_snapshot,
                    "research_memory_guard": {**memory_guard.snapshot(), "owner_token": owner.metadata["token"]}}
            if run.status == "cancelled":
                run.error_message = (
                    f"{run.error_message or 'Run cancelled'}; "
                    f"adapter error after cancellation: {type(e).__name__}: {e}"
                )
            elif execution_timeout.expired():
                cancel_event.set()
                run.status = "cancelled"
                run.error_code = "budget_exceeded"
                run.error_message = "Experiment max_total_seconds exhausted"
            elif memory_guard is not None and memory_guard.error_code:
                run.status = "cancelled"
                run.error_code = memory_guard.error_code
                run.error_message = "Model worker RSS guard stopped this run: " + memory_guard.error_code
            else:
                run.status = "failed"
                run.error_code = type(e).__name__
                run.error_message = str(e)
            if cleanup_failures:
                run.error_message += "; artifact cleanup failed: " + ", ".join(cleanup_failures)
            run.wall_seconds = elapsed
            run.finished_at = utcnow()
            await db.commit()
            return run
        finally:
            _CANCEL_EVENTS.pop(run_id, None)
            if own_lock:
                owner.close()


async def _finish_experiment_if_terminal(db, experiment_id: uuid.UUID) -> bool:
    """Publish a terminal batch result only after every child Run is terminal."""
    exp = await db.get(Experiment, experiment_id, with_for_update=True)
    if exp is None:
        return False
    statuses = (await db.scalars(select(Run.status).where(Run.experiment_id == experiment_id))).all()
    if not statuses:
        return False
    if any(status not in ("succeeded", "failed", "cancelled") for status in statuses):
        exp.state = "queued" if all(status == "queued" for status in statuses) else "running"
        exp.finished_at = None
        return False
    state = "failed" if "failed" in statuses else "cancelled" if "cancelled" in statuses else "completed"
    if exp.state == state and exp.finished_at is not None:
        return False
    exp.state = state
    exp.finished_at = utcnow()
    return True


async def _cancel_unstarted_batch(sm, experiment_id, run_ids, code, message) -> None:
    async with sm() as db:
        await db.execute(update(Run).where(
            Run.id.in_(run_ids), Run.experiment_id == experiment_id, Run.status == "queued",
        ).values(status="cancelled", error_code=code, error_message=message, finished_at=utcnow()))
        await db.commit()


async def run_experiment_batch(
    experiment_id: uuid.UUID,
    run_ids: list[uuid.UUID],
    adapter_registry: Any = None,
    *, _owner: ExecutionOwner | None = None,
) -> None:
    """Execute a batch of runs under generation work context and auto-analyze."""
    if not run_ids:
        if _owner is not None:
            _owner.close()
        return
    owner = _owner
    try:
        if owner is None:
            # Reserve every queued sibling before dispatch. One DB transaction
            # protects this batch against a replay taking its later Run first.
            async with sessionmaker()() as db:
                rows = list(await db.scalars(select(Run).where(
                    Run.id.in_(run_ids), Run.experiment_id == experiment_id,
                ).order_by(Run.id).with_for_update()))
                if any(row.status == "running" for row in rows):
                    return
                queued = [row for row in rows if row.status == "queued"]
                if not queued or any((row.environment_snapshot or {}).get(OWNER_KEY) is not None for row in queued):
                    return
                owner = ExecutionOwner.acquire()
                for row in queued:
                    row.environment_snapshot = owner.snapshot(row.environment_snapshot)
                await db.commit()
        await _run_owned_batch(experiment_id, run_ids, adapter_registry, owner)
    finally:
        if owner is not None:
            owner.close()


async def _run_owned_batch(experiment_id, run_ids, adapter_registry, owner) -> None:
    accepted_id = owner.metadata["generation_id"]
    async with generation.enter_accepted_work(
        "research", accepted_generation_id=uuid.UUID(accepted_id) if accepted_id else None,
    ) as acquired:
        sm = sessionmaker()
        if not acquired:
            logger.warning(
                "Generation rejected accepted work gate for experiment %s", experiment_id
            )
            async with sm() as db:
                cancelled = await db.execute(
                    update(Run).where(
                        Run.id.in_(run_ids), Run.experiment_id == experiment_id, Run.status == "queued",
                    ).values(status="cancelled", error_code="GENERATION_REJECTED",
                             error_message="Generation accepted-work gate rejected (not active or draining)",
                             finished_at=utcnow())
                )
                exp = await db.get(Experiment, experiment_id, with_for_update=True)
                if cancelled.rowcount and exp:
                    exp.receipt = {**(exp.receipt or {}),
                        "error_code": "GENERATION_REJECTED",
                        "error_message": "Generation accepted-work gate rejected (not active or draining)",
                    }
                    await _finish_experiment_if_terminal(db, experiment_id)
                await db.commit()
            return
        # Mark experiment as running
        async with sm() as db:
            exp = await db.get(Experiment, experiment_id, with_for_update=True)
            pending = await db.scalar(select(Run.id).where(
                Run.id.in_(run_ids), Run.experiment_id == experiment_id, Run.status == "queued",
            ).limit(1))
            if exp is None or pending is None:
                return
            from ttakkari.research.planner import execution_budget_seconds

            try:
                seconds = execution_budget_seconds(exp.budget or {})
            except ValueError as exc:
                await db.execute(update(Run).where(
                    Run.id.in_(run_ids), Run.experiment_id == experiment_id, Run.status == "queued",
                ).values(status="cancelled", error_code="budget_invalid",
                         error_message=str(exc), finished_at=utcnow()))
                await _finish_experiment_if_terminal(db, experiment_id)
                await db.commit()
                return
            deadline = asyncio.get_running_loop().time() + seconds
            exp.state = "running"
            exp.finished_at = None
            await db.commit()

        for rid in run_ids:
            if asyncio.get_running_loop().time() >= deadline:
                await _cancel_unstarted_batch(
                    sm, experiment_id, run_ids, "budget_exceeded", "Experiment max_total_seconds exhausted",
                )
                break
            try:
                run = await execute_run_instance(rid, adapter_registry=adapter_registry, deadline=deadline, _owner=owner)
                if run.status == "running":
                    # This is a replay while another batch owns the Run. Do not
                    # steal its remaining configurations or publish partial analysis.
                    return
                if run.status == "cancelled":
                    logger.info("Batch stopped due to run %s cancellation", rid)
                    budget_expired = run.error_code == "budget_exceeded"
                    await _cancel_unstarted_batch(
                        sm, experiment_id, run_ids,
                        "budget_exceeded" if budget_expired else "batch_cancelled",
                        "Experiment max_total_seconds exhausted" if budget_expired
                        else "Batch stopped after a run was cancelled",
                    )
                    break
            except Exception:
                logger.exception("Failed to execute run instance %s", rid)

        # Another batch may still own a Run, or configurations may remain queued.
        async with sm() as db:
            finished = await _finish_experiment_if_terminal(db, experiment_id)
            await db.commit()
        if not finished:
            return

        # Trigger auto-analysis
        try:
            from ttakkari.research.analyzer import analyze_experiment

            async with sm() as db:
                await analyze_experiment(db, experiment_id=experiment_id)
                await db.commit()
        except Exception as e:  # noqa: BLE001 - analysis failure must preserve completed execution
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

    # Reservation is committed with the new Runs, before create_task. A crash
    # between commit and scheduling is therefore recoverable as an interruption.
    owner = ExecutionOwner.acquire()
    for row in created_runs:
        row.environment_snapshot = owner.snapshot(row.environment_snapshot)
    try:
        await db.commit()
    except BaseException:
        owner.close()
        raise

    run_ids = [r.id for r in created_runs]
    if background:
        batch = run_experiment_batch(
            experiment_id, run_ids, adapter_registry=adapter_registry, _owner=owner,
        )
        try:
            task = asyncio.create_task(batch)
        except BaseException:
            batch.close()
            owner.close()
            raise
        _ACTIVE_TASKS[experiment_id] = task

        def finished(done):
            # A task cancelled before its first instruction never enters the
            # coroutine's finally block. Release its reservation here as well.
            owner.close()
            if _ACTIVE_TASKS.get(experiment_id) is done:
                _ACTIVE_TASKS.pop(experiment_id, None)
            if not done.cancelled() and done.exception() is not None:
                logger.error("Research batch %s ended with an error", experiment_id,
                             exc_info=done.exception())

        task.add_done_callback(finished)
    else:
        # Synchronous execution for tests / quick evaluations
        await run_experiment_batch(
            experiment_id, run_ids, adapter_registry=adapter_registry, _owner=owner,
        )

    return created_runs


async def recover_orphaned_runs() -> int:
    async with generation.enter_accepted_work("research-recovery") as acquired:
        return await _recover_orphaned_runs() if acquired else 0


async def _recover_orphaned_runs() -> int:
    """Record owner death without replaying adapters or discarding checkpoints.

    Unknown legacy owners remain untouched. A released OS lock proves only that
    the Python execution owner ended, not that external child resources are gone.
    """
    sm = sessionmaker()
    from ttakkari.research.resources import (
        RECOVERY_KEY,
        RESOURCE_KEY,
        WORKERS_KEY,
        recover_model_workers,
        recover_resource,
    )
    async with sm() as db:
        candidates = list(await db.scalars(select(Run).where(
            Run.status.in_(("queued", "running")) | (
                Run.status.in_(("succeeded", "failed", "cancelled"))
                & (Run.environment_snapshot[RESOURCE_KEY]["state"].astext.in_(("starting", "owned", "cleanup_unverified"))
                   | (Run.environment_snapshot[WORKERS_KEY]["state"].astext == "owned"))
                & ~Run.environment_snapshot.has_key(RECOVERY_KEY)
            ),
            Run.execution_origin == "local_measured",
            Run.environment_snapshot.has_key(OWNER_KEY),
        )))
    groups: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in candidates:
        token = owner_token(row.environment_snapshot or {})
        if token is not None:
            groups.setdefault(token, []).append(row.id)
    recovered = 0
    for token, ids in groups.items():
        handle = probe(token)
        if handle is None:
            continue
        try:
            async with sm() as db:
                rows = list(await db.scalars(select(Run).where(Run.id.in_(ids)).order_by(Run.id).with_for_update()))
                experiments = set()
                for row in rows:
                    snapshot = row.environment_snapshot or {}
                    resource, workers = snapshot.get(RESOURCE_KEY), snapshot.get(WORKERS_KEY)
                    interrupted = row.status in ("queued", "running")
                    cleanup_pending = (
                        ((isinstance(resource, dict) and resource.get("state") in ("starting", "owned", "cleanup_unverified"))
                         or (isinstance(workers, dict) and workers.get("state") == "owned"))
                        and RECOVERY_KEY not in snapshot
                    )
                    if (not (interrupted or cleanup_pending) or owner_token(snapshot) != token):
                        continue
                    study = await db.get(Study, row.study_id)
                    cleanup = (
                        await recover_resource(row.environment_snapshot or {})
                        if row.adapter_id == "llm_inference" and study and study.scope == "personal"
                        else {"status": "unverified", "reason": "unsupported_adapter_or_scope"}
                    )
                    if row.adapter_id == "tts_synthesis" and study:
                        cleanup = await recover_model_workers(snapshot)
                    row.environment_snapshot = {**snapshot, RECOVERY_KEY: {
                        **cleanup, "contract": 1, "owner_token": str(token), "checked_at": utcnow().isoformat(),
                    }}
                    if interrupted:
                        row.status = "failed" if row.status == "running" else "cancelled"
                        row.error_code = "owner_lost"
                        row.error_message = (
                            "실행을 맡은 프로세스가 종료되었습니다. 저장된 측정은 보존했습니다. "
                            + ("기록된 외부 자원의 정리를 확인했습니다." if cleanup["status"] == "verified"
                               else "외부 자원 정리는 미확인입니다.")
                        )
                        row.finished_at = utcnow()
                        experiments.add(row.experiment_id)
                    db.add(Event(study_id=row.study_id,
                                 kind="execution_finished" if interrupted else "resource_recovery", actor="system", payload={
                        "id": str(row.experiment_id), "run_id": str(row.id), "state": row.status,
                        "error_code": row.error_code, "error_message": row.error_message,
                        "owner": row.environment_snapshot[OWNER_KEY],
                        "checkpoint_preserved": True, "external_resource_cleanup": cleanup["status"],
                    }))
                    recovered += 1
                for experiment_id in sorted(experiments):
                    await _finish_experiment_if_terminal(db, experiment_id)
                await db.commit()
        finally:
            handle.close()
    if recovered:
        logger.warning("Recorded %d orphaned research Runs; no adapters replayed", recovered)
    return recovered
