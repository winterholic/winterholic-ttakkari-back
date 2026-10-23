"""Helpers shared by the task modules."""

from ..model import Task


def with_paths(base: dict[str, str], updates: dict[str, str | None]) -> dict[str, str]:
    """Copy of base with path updates; a None value deletes the path."""
    out = dict(base)
    for path, value in updates.items():
        if value is None:
            out.pop(path, None)
        else:
            out[path] = value
    return out


def single_stage(fn):
    """Lift fn(base_files) -> end_files into the list-of-stage-ends form."""

    def run(base: dict[str, str]) -> list[dict[str, str]]:
        return [fn(base)]

    return run


def check_unique(task: Task) -> Task:
    ids = [c.id for s in task.stages for c in s.checks]
    assert len(ids) == len(set(ids)), f"duplicate check ids in {task.id}"
    return task
