"""Ledger: nothing is dropped, identity gaps become UNKNOWN, retries add cost not completions."""

import pytest

from local_llm_eval.ledger import REQUIRED_NON_MISSING, Ledger, summarize

IDENT = {name: "x" for name in REQUIRED_NON_MISSING}


def row(
    attempt_id, profile="p", task="t", repeat=1, verdict="PASS", wall=10.0, **extra
):
    return {
        "attempt_id": attempt_id,
        "profile_id": profile,
        "task_id": task,
        "repeat": repeat,
        "verdict": verdict,
        "wall_s": wall,
        **IDENT,
        **extra,
    }


def test_rows_are_immutable_and_ordered(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    ledger.append(row("a"))
    with pytest.raises(ValueError, match="duplicate"):
        ledger.append(row("a", verdict="FAIL"))
    ledger.append(row("b"))
    assert [r["attempt_id"] for r in ledger.rows()] == ["a", "b"]


def test_missing_identity_turns_a_verdict_into_unknown(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl")
    rec = ledger.append({**row("a", verdict="PASS"), "server_run_id": None})
    assert rec["verdict"] == "UNKNOWN" and rec["evaluator_verdict"] == "PASS"
    assert (
        rec["comparable"] is False and "missing:server_run_id" in rec["unknown_reasons"]
    )


def test_zero_wall_rows_are_kept_and_flagged(tmp_path):
    rec = Ledger(tmp_path / "l.jsonl").append(row("a", verdict="UNKNOWN", wall=0))
    assert "zero_wall_no_response" in rec["flags"]


def test_retries_add_cost_but_not_completions():
    rows = [
        row("a1", repeat=1, verdict="UNKNOWN", wall=40.0),
        row("a2", repeat=1, verdict="PASS", wall=60.0),
        row("b1", repeat=2, verdict="FAIL", wall=100.0),
    ]
    s = summarize(rows)["p"]
    assert s["unique_task_repeat_keys"] == 2 and s["unique_successes"] == 1
    assert s["extra_retry_rows"] == 1 and s["wall_all_attempts_s"] == 200.0
    assert s["cost_all_attempt_wall_per_success_s"] == 200.0
    assert s["success_row_mean_wall_s_not_a_cost"] == 60.0


def test_profiles_are_never_pooled():
    rows = [row("a", profile="p1"), row("b", profile="p2", verdict="FAIL")]
    s = summarize(rows)
    assert s["p1"]["unique_successes"] == 1 and s["p2"]["unique_successes"] == 0


def test_no_success_keeps_total_cost_and_no_completion_cost():
    s = summarize([row("a", verdict="FAIL", wall=33.0)])["p"]
    assert (
        s["cost_all_attempt_wall_per_success_s"] is None
        and s["wall_all_attempts_s"] == 33.0
    )
    assert "N/A" in s["total_cost_note"]


def test_batch_elapsed_is_separate_from_summed_task_wall():
    rows = [
        row("a", wall=300.0, batch_id="b"),
        row("b", repeat=2, wall=289.0, batch_id="b"),
    ]
    s = summarize(rows, batch_elapsed={"b": 310.0})["p"]
    assert s["wall_all_attempts_s"] == 589.0 and s["batch_elapsed_s"] == 310.0
    assert s["cost_batch_elapsed_per_success_s"] == 155.0


def test_non_comparable_rows_do_not_count_as_successes():
    rec = {**row("a"), "server_run_id": None}
    s = summarize([rec | {"comparable": False, "verdict": "PASS"}])["p"]
    assert s["unique_successes"] == 0 and s["non_comparable_rows"] == 1
