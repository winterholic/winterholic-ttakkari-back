"""H13/H14 (resume, cancel recovery): interface only until WBS 2.2 settles the harness."""

from local_llm_eval import codes
from local_llm_eval.evaluator import evaluate_attempt
from local_llm_eval.model import Attempt
from local_llm_eval.simulate import gold_ends, materialize, reports_for
from local_llm_eval.tasks import REGISTRY
from local_llm_eval.tasks.lng import lng_session_check, turn_script_digest


def outcomes(session):
    return {
        cid: out
        for cid, out in lng_session_check(
            Attempt("a", "B4-LNG-D", "p", 1, session=session)
        )
    }


def test_without_session_info_both_gates_are_pending():
    out = outcomes(None)
    assert out["H13"].passed is None and out["H14"].passed is None


def test_resume_without_lineage_fails():
    out = outcomes({"resume": {"resumed": True, "lineage_preserved": False}})
    assert out["H13"].passed is False


def test_cancel_that_never_completed_fails():
    assert outcomes({"cancel": {"completed": False}})["H14"].passed is False


def test_cancel_snapshot_must_compile_and_hold_no_partial_files(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "a.py").write_text("x = 1\n")
    assert (
        outcomes({"cancel": {"completed": True, "snapshot_dir": str(good)}})[
            "H14"
        ].passed
        is True
    )
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "a.py").write_text("def broken(:\n")
    (bad / "b.py.tmp").write_text("partial")
    out = outcomes({"cancel": {"completed": True, "snapshot_dir": str(bad)}})["H14"]
    assert (
        out.passed is False
        and "does not compile" in out.detail
        and "partial file" in out.detail
    )


def test_completed_cancel_without_a_snapshot_stays_pending():
    assert outcomes({"cancel": {"completed": True}})["H14"].passed is None


def test_evaluation_marks_session_pending_and_failures_fail_the_attempt():
    t = REGISTRY["B4-LNG-D"]()
    ends = gold_ends(t)
    reports = reports_for(t, ends, t.gold_reports(t.base_files, ends))
    with materialize(t, ends, reports) as attempt:
        ev = evaluate_attempt(t, attempt)
    assert ev.verdict == codes.PASS and "session" in ev.gates_pending
    lost = {"resume": {"resumed": True, "lineage_preserved": False}}
    with materialize(t, ends, reports, session=lost) as attempt:
        assert evaluate_attempt(t, attempt).verdict == codes.FAIL


def test_turn_script_digest_is_stable_text_hash():
    assert (
        len(turn_script_digest()) == 64 and turn_script_digest() == turn_script_digest()
    )
