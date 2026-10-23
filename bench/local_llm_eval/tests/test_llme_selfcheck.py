"""Full self-check per dev task: classification, references, and every negative control."""

import pytest

from local_llm_eval.selfcheck import selfcheck_task
from local_llm_eval.tasks import REGISTRY


@pytest.mark.parametrize("task_id", sorted(REGISTRY))
def test_selfcheck_is_clean(task_id):
    report = selfcheck_task(REGISTRY[task_id]())
    summary = report["summary"]
    problems = [
        row
        for section in ("classification", "references", "negatives")
        for row in report[section]
        if not row["ok"]
    ]
    assert not problems, problems[:3]
    assert summary["false_positives"] == 0
    assert summary["negatives"] >= 10 and summary["reference_fail"] == 0
    assert {r["reference"] for r in report["references"]} == {"gold", "alt_gold"}
