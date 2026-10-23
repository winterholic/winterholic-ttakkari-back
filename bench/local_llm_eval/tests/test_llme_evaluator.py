"""Evaluator core: start-state hash, read-only vs diff-required, structural gates, reports, infra rows."""

import os
from pathlib import Path

import pytest
from local_llm_eval import codes
from local_llm_eval.evaluator import ConfigError, evaluate_attempt
from local_llm_eval.model import Attempt
from local_llm_eval.simulate import gold_ends, materialize, reports_for
from local_llm_eval.tasks import REGISTRY


def task(task_id):
    return REGISTRY[task_id]()


def run(t, ends, reports="auto", **kw):
    if reports == "auto":
        reports = reports_for(
            t, ends, t.gold_reports(t.base_files, ends) if t.gold_reports else None
        )
    with materialize(t, ends, reports, **kw) as attempt:
        return evaluate_attempt(t, attempt)


def test_gold_passes_and_pending_gates_are_listed_not_hidden():
    t = task("B4-SGL-D")
    ev = run(t, gold_ends(t))
    assert ev.verdict == codes.PASS
    assert "trace_reconcile" in ev.gates_pending and "trace_unavailable" in ev.flags


def test_start_state_must_equal_the_pristine_manifest():
    t = task("B4-SGL-D")
    solved = dict(gold_ends(t)[0])
    ev = run(t, gold_ends(t), start_files=solved)
    assert (ev.verdict, ev.fail_code) == (codes.INFRA, codes.INFRA_BASE_MISMATCH)
    assert "differs from the sealed baseline" in ev.stages[0].detail


def test_one_changed_byte_in_the_start_state_is_a_mismatch():
    t = task("B4-SGL-D")
    start = dict(t.base_files)
    start["stats.py"] += " "
    ev = run(t, gold_ends(t), start_files=start)
    assert ev.fail_code == codes.INFRA_BASE_MISMATCH


def test_missing_start_tree_is_unknown_not_a_model_result():
    t = task("B4-SGL-D")
    with materialize(t, gold_ends(t), reports_for(t, gold_ends(t))) as attempt:
        attempt.stages[0].start_dir = None
        ev = evaluate_attempt(t, attempt)
    assert ev.verdict == codes.UNKNOWN


def test_evaluator_hashes_the_real_start_tree_not_a_caller_manifest():
    """The caller can no longer vouch for the start state: only files on disk count."""
    t = task("B4-SGL-D")
    ends = gold_ends(t)
    with materialize(t, ends, reports_for(t, ends)) as attempt:
        start = attempt.stages[0].start_dir
        assert evaluate_attempt(t, attempt).verdict == codes.PASS
        (start / "stats.py").write_text(
            t.base_files["stats.py"] + "\n# tampered before the run\n"
        )
        ev = evaluate_attempt(t, attempt)
    assert (ev.verdict, ev.fail_code) == (codes.INFRA, codes.INFRA_BASE_MISMATCH)


def test_extra_missing_or_symlinked_start_files_are_a_mismatch():
    t = task("B4-SGL-D")
    ends = gold_ends(t)
    for mutate in ("extra", "missing", "symlink"):
        with materialize(t, ends, reports_for(t, ends)) as attempt:
            start = attempt.stages[0].start_dir
            if mutate == "extra":
                (start / "leftover.txt").write_text("x")
            elif mutate == "missing":
                (start / "stats.py").unlink()
            else:
                os.symlink("/etc/hosts", start / "link.txt")
            ev = evaluate_attempt(t, attempt)
        assert ev.fail_code == codes.INFRA_BASE_MISMATCH, mutate


def test_pycache_in_the_start_tree_does_not_count():
    t = task("B4-SGL-D")
    ends = gold_ends(t)
    with materialize(t, ends, reports_for(t, ends)) as attempt:
        cache = attempt.stages[0].start_dir / "__pycache__"
        cache.mkdir()
        (cache / "stats.cpython-313.pyc").write_bytes(b"\0")
        assert evaluate_attempt(t, attempt).verdict == codes.PASS


def test_noop_differs_by_change_class():
    sgl, exp = task("B4-SGL-D"), task("B4-EXP-D")
    assert run(sgl, [dict(sgl.base_files)]).fail_code == "FAIL_NOOP"
    ev = run(exp, [dict(exp.base_files)])
    assert ev.fail_code == "FAIL_NOOP" and "no result" in ev.stages[0].detail


def test_read_only_task_passes_with_no_source_change_and_diff_task_would_fail():
    exp = task("B4-EXP-D")
    ends = gold_ends(exp)
    changed = set(ends[0]) ^ set(exp.base_files)
    assert changed == {"report.json"}
    assert run(exp, ends).verdict == codes.PASS


def test_read_only_task_rejects_a_source_edit_even_with_a_correct_report():
    exp = task("B4-EXP-D")
    end = dict(gold_ends(exp)[0])
    end["ledger/money.py"] += "\n# touched\n"
    assert run(exp, [end]).fail_code == "FAIL_FORBIDDEN_WRITE"


def test_symlink_in_the_workspace_is_forbidden(tmp_path):
    t = task("B4-SGL-D")
    with materialize(t, gold_ends(t), reports_for(t, gold_ends(t))) as attempt:
        os.symlink("/etc/hosts", Path(attempt.stages[0].end_dir) / "link.txt")
        ev = evaluate_attempt(t, attempt)
    assert ev.fail_code == "FAIL_FORBIDDEN_WRITE"


def test_pycache_and_pyc_are_ignored(tmp_path):
    t = task("B4-SGL-D")
    with materialize(t, gold_ends(t), reports_for(t, gold_ends(t))) as attempt:
        cache = Path(attempt.stages[0].end_dir) / "__pycache__"
        cache.mkdir()
        (cache / "stats.cpython-313.pyc").write_bytes(b"\0")
        assert evaluate_attempt(t, attempt).verdict == codes.PASS


def test_report_must_match_the_diff():
    t = task("B4-SGL-D")
    ends = gold_ends(t)
    reports = reports_for(t, ends)
    reports[0]["changed_files"] = ["stats.py", "tests/test_stats.py"]
    ev = run(t, ends, reports)
    assert ev.fail_code == "FAIL_REPORT_MISMATCH"
    assert "code_pass_report_mismatch" in ev.flags


def test_missing_report_is_not_a_success():
    t = task("B4-SGL-D")
    assert run(t, gold_ends(t), reports=[None]).fail_code == "FAIL_REPORT_MISMATCH"


def test_false_done_flag_when_a_failing_attempt_claims_done():
    t = task("B4-SGL-D")
    ev = run(
        t, [dict(t.base_files, **{"stats.py": t.base_files["stats.py"] + "\n# c\n"})]
    )
    assert ev.verdict == codes.FAIL and "false_done" in ev.stages[0].flags


def test_claim_of_a_command_absent_from_the_trace_is_a_mismatch():
    t = task("B4-SGL-D")
    ends = gold_ends(t)
    reports = reports_for(t, ends)
    reports[0]["commands_run"] = ["python -m unittest"]
    trace = [{"t": 1, "kind": "command", "command": "ls"}]
    with materialize(t, ends, reports, traces=[trace]) as attempt:
        assert evaluate_attempt(t, attempt).fail_code == "FAIL_REPORT_MISMATCH"
    trace = [{"t": 1, "kind": "test", "command": "python -m unittest discover"}]
    with materialize(t, ends, reports, traces=[trace]) as attempt:
        ev = evaluate_attempt(t, attempt)
    assert ev.verdict == codes.PASS and "trace_reconcile" not in ev.gates_pending


def test_infra_rows_are_not_evaluated_and_unknown_is_not_excluded():
    t = task("B4-SGL-D")
    proven = Attempt(
        "a", t.id, "p", 1, infra={"code": "INFRA_HTTP_503", "proven": True}
    )
    unproven = Attempt(
        "b", t.id, "p", 1, infra={"code": None, "proven": False}, meta={"wall_s": 0}
    )
    ev_a, ev_b = evaluate_attempt(t, proven), evaluate_attempt(t, unproven)
    assert (ev_a.verdict, ev_a.evaluated) == (codes.INFRA, False)
    assert ev_b.verdict == codes.UNKNOWN and "zero_wall_no_response" in ev_b.flags


def test_every_stage_needs_a_delta_check():
    t = task("B4-SGL-D")
    t.stages[0].checks = [c for c in t.stages[0].checks if c.kind != codes.KIND_DELTA]
    with pytest.raises(ConfigError, match="no delta check"):
        run(t, gold_ends(t))


def test_long_task_start_must_continue_from_the_previous_end():
    t = task("B4-LNG-D")
    ends = gold_ends(t)
    with materialize(
        t, ends, reports_for(t, ends, t.gold_reports(t.base_files, ends))
    ) as attempt:
        (attempt.stages[2].start_dir / "stats.py").write_text(t.base_files["stats.py"])
        ev = evaluate_attempt(t, attempt)
    assert (
        ev.verdict == codes.INFRA
        and ev.stages[2].fail_code == codes.INFRA_BASE_MISMATCH
    )


def test_tamper_is_judged_against_the_original_not_the_previous_stage():
    t = task("B4-LNG-D")
    ends = [dict(e) for e in gold_ends(t)]
    for end in ends[2:]:
        end.pop("tests/test_stats.py")
    ev = run(t, ends, reports_for(t, ends, t.gold_reports(t.base_files, ends)))
    assert ev.fail_code == "FAIL_TEST_TAMPER"


def test_stage_result_carries_the_start_hash_the_evaluator_computed():
    from local_llm_eval.manifest import manifest_of_files, tree_hash

    t = task("B4-SGL-D")
    ev = run(t, gold_ends(t))
    assert ev.stages[0].start_tree_sha256 == tree_hash(manifest_of_files(t.base_files))
    mismatch = run(t, gold_ends(t), start_files=dict(gold_ends(t)[0]))
    assert mismatch.stages[0].start_tree_sha256 != ev.stages[0].start_tree_sha256
