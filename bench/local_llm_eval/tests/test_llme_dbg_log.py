"""DBG rev2: the failure log is real, reproducible output; the original fixture's premise was false."""

from local_llm_eval import fixtures
from local_llm_eval.manifest import write_files
from local_llm_eval.runner import run_check
from local_llm_eval.tasks import dbg


def test_committed_log_equals_a_fresh_run_on_the_pristine_files():
    committed = (fixtures.DATA / "long_rules_rev2_ci_failure.log").read_text(
        encoding="utf-8"
    )
    assert dbg.generate_ci_log(dbg.base_files_without_log()) == committed
    assert (
        "test_rule_043_non_multiple" in committed
        and "27.333333333333332 != 27" in committed
    )
    assert "FAILED (failures=1)" in committed


def test_original_fixture_public_suite_was_green_at_base(tmp_path):
    """The reason for rev2: the old prompt claimed a failing test but nothing failed."""
    write_files(tmp_path, fixtures.files("long_rules"))
    run = run_check(tmp_path, "orig", "rec('u', run_unittest('tests', '.'))\n")
    assert (
        run.status == "OBS"
        and run.obs["u"]["failures"] == 0
        and run.obs["u"]["errors"] == 0
    )


def test_rev2_base_has_exactly_one_failing_public_test(tmp_path):
    files = dbg.base_files_without_log()
    write_files(tmp_path, files)
    run = run_check(tmp_path, "rev2", "rec('u', run_unittest('tests', '.'))\n")
    assert run.obs["u"]["failures"] == 1 and run.obs["u"]["errors"] == 0
