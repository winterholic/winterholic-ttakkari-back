"""Static leak scan: no unreviewed hit in any dev task, and the scan does fire on a planted leak."""

import pytest
from local_llm_eval.leakscan import scan
from local_llm_eval.model import Check
from local_llm_eval.tasks import REGISTRY


def _planted_judge(obs, facts):
    assert obs["x"] != "ONLY_IN_HIDDEN_9731"


def _planted_judge(obs, facts):
    assert obs["x"] != "ONLY_IN_HIDDEN_9731"


@pytest.mark.parametrize("task_id", sorted(REGISTRY))
def test_no_unreviewed_leak(task_id):
    result = scan(REGISTRY[task_id]())
    assert not result.findings, result.findings[:5]


def test_scan_fires_on_a_hidden_literal_planted_in_workspace_source():
    task = REGISTRY["B4-SGL-D"]()
    task.stages[0].checks.append(Check("HX", probe="rec('x', 1)", judge=_planted_judge))
    task.base_files["stats.py"] += "\n# ONLY_IN_HIDDEN_9731\n"
    findings = scan(task).findings
    assert any(
        f["token"] == "ONLY_IN_HIDDEN_9731" and f["direction"] == "hidden_literal"
        for f in findings
    )


def test_scan_fires_on_a_gold_only_token_already_present_in_the_workspace():
    task = REGISTRY["B4-SGL-D"]()
    gold = task.gold
    task.gold = lambda base: [
        {
            **gold(base)[0],
            "stats.py": gold(base)[0]["stats.py"] + "\n# statistics_median_unique\n",
        }
    ]
    task.base_files["hint.py"] = "# statistics_median_unique\n"
    assert any(f["token"] == "statistics_median_unique" for f in scan(task).findings)


def test_exploration_names_are_allowlisted_with_a_reason():
    result = scan(REGISTRY["B4-EXP-D"]())
    assert result.allowed and all("reason" in row for row in result.allowed)
