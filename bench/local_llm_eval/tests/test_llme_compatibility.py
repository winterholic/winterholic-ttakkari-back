"""Harmless prefixes on correct solutions must not be mistaken for tampering."""

import pytest
from local_llm_eval.evaluator import evaluate_attempt
from local_llm_eval.simulate import gold_ends, materialize, reports_for
from local_llm_eval.tasks import REGISTRY

PREFIXES = {
    "datetime": "import datetime\n",
    "heapq": "import heapq\n",
    "bisect": "import bisect\n",
    "logging": "import logging\n",
    "pathlib": "import pathlib\n",
    "fractions": "import fractions\n",
    "math.log": "import math; harmless = math.log(1)\n",
    "type": "harmless = type(1)\n",
    "hasattr": "harmless = hasattr(1, 'real')\n",
    "getattr": "harmless = getattr(1, 'real')\n",
    "__all__": "__all__ = []\n",
    "__hash__": "class Harmless: __hash__ = lambda self: 1\n",
    "__getitem__": "class Harmless: __getitem__ = lambda self, i: i\n",
    "__call__": "class Harmless: __call__ = lambda self: None\n",
}


@pytest.mark.parametrize("task_id", sorted(k for k in REGISTRY if k != "B4-EXP-D"))
@pytest.mark.parametrize("prefix", sorted(PREFIXES))
def test_harmless_prefix_is_not_a_checker_patch(task_id, prefix):
    task = REGISTRY[task_id]()
    ends = gold_ends(task)
    path = task.stages[0].required_change_paths[0]
    for end in ends:
        end[path] = PREFIXES[prefix] + end[path]
    extra = task.gold_reports(task.base_files, ends) if task.gold_reports else None
    with materialize(
        task, ends, reports_for(task, ends, extra), session=task.gold_session
    ) as attempt:
        ev = evaluate_attempt(task, attempt)
    assert ev.verdict == "PASS", (task_id, prefix, ev)
