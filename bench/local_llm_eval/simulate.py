"""Build attempts from file dictionaries (no model): used by negatives, gold runs and tests."""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .manifest import changed_paths, diff_manifests, manifest_of_files, write_files
from .model import Attempt, StageAttempt, Task


def auto_report(stage_base: dict[str, str], end: dict[str, str]) -> dict:
    """An honest structured report: claims exactly the diff and says done."""
    diff = diff_manifests(manifest_of_files(stage_base), manifest_of_files(end))
    return {"status": "done", "changed_files": changed_paths(diff), "commands_run": []}


def stage_bases(task: Task, ends: list[dict[str, str]]) -> list[dict[str, str]]:
    return [task.base_files] + list(ends[:-1])


def gold_ends(task: Task) -> list[dict[str, str]]:
    assert task.gold is not None
    return task.gold(task.base_files)


def reports_for(
    task: Task, ends: list[dict[str, str]], extra: list[dict | None] | None = None
) -> list[dict | None]:
    out: list[dict | None] = []
    for index, (base, end) in enumerate(
        zip(stage_bases(task, ends), ends, strict=True)
    ):
        report = auto_report(base, end)
        if extra and extra[index]:
            report.update(extra[index])
        out.append(report)
    return out


@contextmanager
def materialize(
    task: Task,
    ends: list[dict[str, str]],
    reports: list[dict | None] | None = None,
    start_files: dict[str, str] | None = None,
    traces: list[list[dict] | None] | None = None,
    session: dict | None = None,
    **meta,
) -> Iterator[Attempt]:
    """Write each stage end into its own directory and yield an Attempt with honest start hashes."""
    with tempfile.TemporaryDirectory(prefix="llme-attempt-") as tmp:
        stages = []
        previous = start_files if start_files is not None else task.base_files
        for index, end in enumerate(ends):
            end_dir = Path(tmp) / f"stage{index}"
            end_dir.mkdir()
            write_files(end_dir, end)
            stages.append(
                StageAttempt(
                    start_manifest=manifest_of_files(previous),
                    end_dir=end_dir,
                    report=reports[index] if reports else None,
                    trace=traces[index] if traces else None,
                )
            )
            previous = end
        yield Attempt(
            attempt_id=meta.pop("attempt_id", "sim"),
            task_id=task.id,
            profile_id=meta.pop("profile_id", "simulated"),
            repeat=meta.pop("repeat", 1),
            stages=stages,
            session=session,
            meta=meta,
        )
