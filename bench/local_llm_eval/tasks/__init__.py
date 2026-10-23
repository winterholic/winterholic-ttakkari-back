"""Dev task registry (holdout tasks are intentionally absent: they live in the sealed bundle)."""

from collections.abc import Callable

from ..model import Task


def _lazy(module: str) -> Callable[[], Task]:
    def build() -> Task:
        import importlib

        from ..negatives import integrity_negatives

        task = importlib.import_module(f"{__name__}.{module}").build()
        task.negatives.extend(integrity_negatives(task))
        return task

    return build


REGISTRY: dict[str, Callable[[], Task]] = {
    "B4-EXP-D": _lazy("exp"),
    "B4-SGL-D": _lazy("sgl"),
    "B4-MUL-D": _lazy("mul"),
    "B4-API-D": _lazy("api"),
    "B4-DBG-D": _lazy("dbg"),
    "B4-REF-D": _lazy("ref"),
    "B4-TST-D": _lazy("tst"),
    "B4-REC-D": _lazy("rec"),
    "B4-LNG-D": _lazy("lng"),
}
