"""Isolated regression reproduction for planner adapter validation failures.

Runs the real validate_experiment_plan function with an in-memory DB stub and
adapter registry doubles. It does not connect to a database or dispatch runs.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
import uuid

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

import ttakkari.research.planner as planner  # noqa: E402
import ttakkari.research.adapters as adapters  # noqa: E402

print(
    "runtime:",
    {"python": sys.executable, "planner_module": planner.__file__, "database_target": None},
)


class EmptyDb:
    async def get(self, model, key):
        return None


class Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def get_adapter(self, adapter_id):
        return self.adapter


class RejectMissingRequired:
    def validate_config(self, params, *, scope):
        return ["required input missing: model"] if "model" not in params else []


class RejectInvalidSweep:
    def validate_config(self, params, *, scope):
        return ["n_ctx must be positive"] if params.get("n_ctx", 1) <= 0 else []


class ExplodingValidator:
    def validate_config(self, params, *, scope):
        raise RuntimeError("synthetic validator failure")


async def call(adapter, adapter_id="llm_inference", *, search_space=None, fixed_inputs=None):
    adapters.get_adapter_registry = lambda: Registry(adapter)
    return await planner.validate_experiment_plan(
        EmptyDb(),
        study_id=uuid.uuid4(),
        method_id=None,
        compared_method_ids=[],
        adapter_id=adapter_id,
        strategy="grid" if search_space else "single",
        search_space=search_space or {},
        fixed_inputs=fixed_inputs or {},
        budget={"max_runs": 2},
    )


async def main():
    missing = await call(RejectMissingRequired())
    print(
        "missing_required:",
        {"valid": missing.valid, "errors": missing.errors},
    )

    sweep = await call(
        RejectInvalidSweep(),
        search_space={"n_ctx": [16, 0]},
    )
    print(
        "invalid_sweep_member:",
        {
            "expanded": [c["parameters"] for c in sweep.expanded_configurations],
            "valid": sweep.valid,
            "errors": sweep.errors,
        },
    )

    raised = await call(ExplodingValidator())
    print("validator_exception:", {"valid": raised.valid, "errors": raised.errors})

    unknown = await call(None, adapter_id="unknown_adapter")
    print("unknown_adapter:", {"valid": unknown.valid, "errors": unknown.errors})


if __name__ == "__main__":
    asyncio.run(main())
