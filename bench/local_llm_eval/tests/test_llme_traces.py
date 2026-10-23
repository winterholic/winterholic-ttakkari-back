"""Trace-dependent checks: gating for exploration, non-gating metrics for debug and recovery."""

from local_llm_eval import codes
from local_llm_eval.evaluator import evaluate_attempt
from local_llm_eval.simulate import gold_ends, materialize, reports_for
from local_llm_eval.tasks import REGISTRY


def evaluate(task_id, trace):
    t = REGISTRY[task_id]()
    ends = gold_ends(t)
    reports = reports_for(
        t, ends, t.gold_reports(t.base_files, ends) if t.gold_reports else None
    )
    with materialize(t, ends, reports, traces=[trace]) as attempt:
        return evaluate_attempt(t, attempt)


def check_row(ev, check_id):
    return next(c for c in ev.stages[0].checks if c["id"] == check_id)


EXPLORED = [
    {
        "t": 1,
        "kind": "search",
        "output": "ledger/money.py ledger/legacy/fees.py ledger/tax.py ledger/invoice.py",
    },
    {"t": 2, "kind": "read", "path": "ledger/money.py"},
    {"t": 3, "kind": "read", "path": "ledger/legacy/fees.py"},
    {"t": 9, "kind": "edit", "path": "report.json"},
]


def test_exploration_with_reads_before_the_report_passes_and_the_gate_closes():
    ev = evaluate("B4-EXP-D", EXPLORED)
    assert ev.verdict == codes.PASS
    assert check_row(ev, "H8")["passed"] is True and "H8" not in ev.gates_pending


def test_exploration_without_any_reading_is_rejected_once_a_trace_exists():
    ev = evaluate("B4-EXP-D", [{"t": 9, "kind": "edit", "path": "report.json"}])
    assert ev.fail_code == "FAIL_MISSING_TARGET"
    assert "cited without reading" in check_row(ev, "H8")["detail"]


def test_reading_only_after_writing_the_report_does_not_count():
    late = [{"t": 1, "kind": "edit", "path": "report.json"}] + [
        {"t": 5, "kind": "read", "path": p}
        for p in ("ledger/money.py", "ledger/legacy/fees.py")
    ]
    assert evaluate("B4-EXP-D", late).fail_code == "FAIL_MISSING_TARGET"


def test_without_a_trace_the_exploration_gate_is_pending_not_passed():
    t = REGISTRY["B4-EXP-D"]()
    ends = gold_ends(t)
    with materialize(t, ends, reports_for(t, ends)) as attempt:
        ev = evaluate_attempt(t, attempt)
    assert ev.verdict == codes.PASS and "H8" in ev.gates_pending


def test_debug_reproduction_is_a_metric_and_never_changes_the_verdict():
    yes = [
        {
            "t": 1,
            "kind": "test",
            "exit_code": 1,
            "output": "FAIL: test_rule_043_non_multiple",
        },
        {"t": 2, "kind": "edit", "path": "rules.py"},
    ]
    no = [{"t": 1, "kind": "edit", "path": "rules.py"}]
    ev_yes, ev_no = evaluate("B4-DBG-D", yes), evaluate("B4-DBG-D", no)
    assert ev_yes.verdict == ev_no.verdict == codes.PASS
    assert check_row(ev_yes, "H1")["metrics"]["reproduced_before_edit"] is True
    assert check_row(ev_no, "H1")["metrics"]["reproduced_before_edit"] is False


def test_recovery_metric_sees_fail_then_pass_after_the_first_edit():
    recovered = [
        {"t": 1, "kind": "edit", "path": "shop/pricing.py"},
        {"t": 2, "kind": "test", "exit_code": 1},
        {"t": 3, "kind": "edit", "path": "shop/pricing.py"},
        {"t": 4, "kind": "test", "exit_code": 0},
    ]
    first_try = [
        {"t": 1, "kind": "edit", "path": "shop/pricing.py"},
        {"t": 2, "kind": "test", "exit_code": 0},
    ]
    stuck = [
        {"t": 1, "kind": "edit", "path": "x"},
        {"t": 2, "kind": "test", "exit_code": 1},
    ]
    assert check_row(evaluate("B4-REC-D", recovered), "H7")["metrics"] == {
        "trap_activated": True,
        "recovered": True,
    }
    assert check_row(evaluate("B4-REC-D", first_try), "H7")["metrics"] == {
        "trap_activated": False,
        "recovered": False,
    }
    assert check_row(evaluate("B4-REC-D", stuck), "H7")["metrics"] == {
        "trap_activated": True,
        "recovered": False,
    }
