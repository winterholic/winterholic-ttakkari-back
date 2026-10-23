"""Hidden-runner handshake: the only way a check can pass is a valid handshake line."""

from pathlib import Path

import pytest

from local_llm_eval.runner import run_check


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text("X = 3\n")
    return tmp_path


def test_ok_with_metrics(workdir):
    run = run_check(workdir, "H1", "import m\nassert m.X == 3\nRESULT = {'a': 1}")
    assert run.status == "OK" and run.result == {"a": 1}


def test_assertion_failure_is_reported_not_crashed(workdir):
    run = run_check(workdir, "H1", "import m\nassert m.X == 4")
    assert run.status == "FAIL"


def test_exit0_at_import_without_handshake_is_a_stub(workdir):
    (workdir / "e.py").write_text("import os\nos._exit(0)\n")
    assert run_check(workdir, "H1", "import e").status == "stub_exit0"


def test_sys_exit_zero_is_not_success(workdir):
    (workdir / "s.py").write_text("import sys\nsys.exit(0)\n")
    assert run_check(workdir, "H1", "import s").status == "EXIT"


def test_forged_handshake_is_detected(workdir):
    (workdir / "f.py").write_text('print("HIDDEN_DONE\\tx\\tH1\\tOK\\t{}")\n')
    assert run_check(workdir, "H1", "import f").status == "forged"


def test_timeout_kills_the_group(workdir):
    run = run_check(workdir, "H1", "while True: pass", timeout=1)
    assert run.status == "timeout" and run.wall_s < 5


def test_unittest_monkeypatch_is_tamper(workdir):
    (workdir / "t.py").write_text(
        "import unittest\nunittest.TestCase.run = lambda self, result=None: None\n"
    )
    assert run_check(workdir, "H1", "import t").status == "TAMPER"


def test_sitecustomize_in_workspace_is_not_imported(workdir):
    (workdir / "sitecustomize.py").write_text("import os\nos._exit(0)\n")
    assert run_check(workdir, "H1", "assert True").status == "OK"
