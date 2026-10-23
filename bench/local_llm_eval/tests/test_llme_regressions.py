"""Replays of the 4.1 probes against the old verifiers: each must be rejected by the new evaluator."""

from local_llm_eval import codes
from local_llm_eval.evaluator import evaluate_attempt
from local_llm_eval.simulate import gold_ends, materialize, reports_for
from local_llm_eval.tasks import REGISTRY


def verdict(task_id, ends):
    t = REGISTRY[task_id]()
    with materialize(t, ends, reports_for(t, ends)) as attempt:
        return evaluate_attempt(t, attempt)


def test_p2_exit0_in_package_init_alone_is_rejected():
    """Old verifier: hidden_ok and unittest_ok both true for this no-op (probe P2)."""
    t = REGISTRY["B4-MUL-D"]()
    end = dict(t.base_files)
    end["inv/__init__.py"] = "import os\nos._exit(0)\n"
    ev = verdict("B4-MUL-D", [end])
    assert ev.verdict == codes.FAIL and ev.fail_code == "FAIL_MISSING_TARGET"


def test_p2_exit0_cannot_ride_along_with_a_real_fix():
    t = REGISTRY["B4-MUL-D"]()
    end = dict(gold_ends(t)[0])
    end["inv/__init__.py"] = "import os\nos._exit(0)\n" + end["inv/__init__.py"]
    assert verdict("B4-MUL-D", [end]).fail_code == "FAIL_STUB_EXIT0"


def test_p6_inline_copy_with_a_comment_is_rejected():
    t = REGISTRY["B4-REF-D"]()
    names = {n.name for n in t.negatives}
    assert any("probe P6" in n for n in names)
    ev = verdict(
        "B4-REF-D",
        next(
            n.end(t.base_files, gold_ends(t))
            for n in t.negatives
            if "probe P6" in n.name
        ),
    )
    assert ev.verdict == codes.FAIL


def test_p4_always_failing_tests_are_rejected_although_they_kill_every_mutant():
    t = REGISTRY["B4-TST-D"]()
    neg = next(n for n in t.negatives if "probe P4" in n.name)
    assert (
        verdict("B4-TST-D", neg.end(t.base_files, gold_ends(t))).verdict == codes.FAIL
    )


def test_pre_solved_workspace_with_valid_tests_is_not_a_pass():
    """Audit P1-3: the old final-state checkers pass this; the start-hash gate does not."""
    t = REGISTRY["B4-REC-D"]()
    solved = gold_ends(t)[0]
    with materialize(
        t, [solved], reports_for(t, [solved]), start_files=solved
    ) as attempt:
        ev = evaluate_attempt(t, attempt)
    assert ev.verdict == codes.INFRA and ev.fail_code == codes.INFRA_BASE_MISMATCH


def test_audit_p1_2_quantity_separator_output_is_rejected():
    t = REGISTRY["B4-MUL-D"]()
    neg = next(n for n in t.negatives if n.id == "N14")
    ev = verdict("B4-MUL-D", neg.end(t.base_files, gold_ends(t)))
    assert ev.verdict == codes.FAIL and ev.fail_code in (
        "FAIL_HIDDEN_ONLY",
        "FAIL_HIDDEN",
        "FAIL_PUBLIC",
    )
