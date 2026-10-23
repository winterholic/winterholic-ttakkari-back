"""Observation runner: candidate-side processes return raw observations, nothing else."""

from pathlib import Path

import pytest
from local_llm_eval.runner import run_check


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text("X = 3\n")
    return tmp_path


def test_observations_round_trip(workdir):
    run = run_check(
        workdir, "H1", "import m\nrec('x', m.X)\nrec('t', (1, 2))\nrec('s', {3, 1})"
    )
    assert run.status == "OBS" and run.obs == {"x": 3, "t": [1, 2], "s": [1, 3]}


def test_attempt_records_values_and_exception_types(workdir):
    probe = "import m\nrec('ok', attempt(lambda: m.X + 1))\nrec('err', attempt(lambda: 1 / 0))\n"
    run = run_check(workdir, "H1", probe)
    assert run.obs["ok"] == {"ok": 4}
    assert "ZeroDivisionError" in run.obs["err"]["raised"]


def test_probe_error_is_reported_as_error_not_as_pass(workdir):
    run = run_check(workdir, "H1", "import missing_module\n")
    assert run.status == "OBS" and "ModuleNotFoundError" in run.obs["__error__"]


def test_exit0_at_import_leaves_no_observation_line(workdir):
    (workdir / "e.py").write_text("import os\nos._exit(0)\n")
    assert run_check(workdir, "H1", "import e").status == "stub_exit0"


def test_sys_exit_zero_is_an_error_observation(workdir):
    (workdir / "s.py").write_text("import sys\nsys.exit(0)\n")
    run = run_check(workdir, "H1", "import s")
    assert run.status == "OBS" and run.obs["__error__"].startswith("SystemExit")


def test_timeout_kills_the_process_group(workdir):
    run = run_check(workdir, "H1", "while True: pass", timeout=1)
    assert run.status == "timeout" and run.wall_s < 5


def test_unittest_monkeypatch_is_recorded_as_tampering(workdir):
    (workdir / "t.py").write_text(
        "import unittest\nunittest.TestCase.run = lambda self, result=None: None\n"
    )
    run = run_check(workdir, "H1", "import t")
    assert run.obs["__tampered__"] == ["TestCase.run"]


def test_sitecustomize_in_workspace_is_not_imported(workdir):
    (workdir / "sitecustomize.py").write_text("import os\nos._exit(0)\n")
    assert run_check(workdir, "H1", "rec('x', 1)").status == "OBS"


def test_parent_provided_dirs_are_listed_after_exit(workdir):
    probe = "import os\nopen(os.path.join(DIRS['d'], 'a.txt'), 'w').write('hi')\nrec('x', 1)\n"
    run = run_check(workdir, "H1", probe, dir_names=("d",))
    assert (
        run.facts["dirs"]["d"] == ["a.txt"]
        and run.facts["dir_files"]["d"]["a.txt"] == "hi"
    )


def test_workspace_listing_before_and_after_is_a_parent_fact(workdir):
    run = run_check(workdir, "H1", "open('new.txt', 'w').write('x')\nrec('x', 1)")
    assert (
        "new.txt" in run.facts["cwd_after"] and "new.txt" not in run.facts["cwd_before"]
    )
