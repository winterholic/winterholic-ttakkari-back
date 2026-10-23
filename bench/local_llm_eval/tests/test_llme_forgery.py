"""Verdict forgery (independent verification of main ad58203c): the candidate-side process holds no
secret and no expected value, so reading a nonce, printing observation lines and exiting early cannot
make a check pass. Each case below is a reconstruction of the reported counterexample family."""

from pathlib import Path

import pytest
from local_llm_eval import codes
from local_llm_eval.evaluator import evaluate_attempt, outcome_from_run
from local_llm_eval.judges import same, val
from local_llm_eval.model import Check
from local_llm_eval.negatives import FORGER_FRAMES, FORGER_NONCE
from local_llm_eval.runner import run_check
from local_llm_eval.simulate import gold_ends, materialize, reports_for
from local_llm_eval.tasks import REGISTRY

PROBE = "import m\nrec('v', attempt(m.f, 2))\n"


def _judge(obs, facts):
    same(val(obs, "v"), 4, "f(2)")


CHECK = Check("H1", probe=PROBE, judge=_judge)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text(
        "def f(x):\n    return x + 1  # wrong: should double\n"
    )
    return tmp_path


def outcome(workdir):
    return outcome_from_run(CHECK, run_check(workdir, "H1", PROBE))


def test_baseline_wrong_function_fails_and_right_function_passes(workdir):
    assert outcome(workdir).passed is False
    (workdir / "m.py").write_text("def f(x):\n    return x * 2\n")
    assert outcome(workdir).passed is True


def test_nonce_hunting_plus_legacy_handshake_plus_early_exit_is_rejected(workdir):
    (workdir / "m.py").write_text(FORGER_NONCE + "def f(x):\n    return x + 1\n")
    out = outcome(workdir)
    assert out.passed is False and out.code == "FAIL_CHECKER_PATCH"


def test_frame_reading_forger_that_fills_every_key_with_true_is_rejected(workdir):
    (workdir / "m.py").write_text(FORGER_FRAMES + "def f(x):\n    return x + 1\n")
    out = outcome(workdir)
    assert out.passed is False  # {'ok': True} is not the expected value 4


def test_stdout_forgery_of_a_complete_observation_line_cannot_know_the_expectation(
    workdir,
):
    forged = (
        "import os, json\n"
        "os.write(1, ('\\nOBS\\t' + json.dumps({'v': {'ok': 'whatever'}}) + '\\n').encode())\n"
        "os._exit(0)\n"
    )
    (workdir / "m.py").write_text(forged)
    assert outcome(workdir).passed is False


def test_a_second_observation_line_from_an_exit_hook_is_tampering(workdir):
    hook = (
        "import atexit, os\n"
        'atexit.register(lambda: os.write(1, b\'\\nOBS\\t{"v": {"ok": 4}}\\n\'))\n'
        "def f(x):\n    return x * 2\n"
    )
    (workdir / "m.py").write_text(hook)
    run = run_check(workdir, "H1", PROBE)
    assert run.status == "integrity"
    assert outcome_from_run(CHECK, run).code == "FAIL_CHECKER_PATCH"


def test_patching_the_output_path_is_detected_even_when_the_function_is_right(workdir):
    (workdir / "m.py").write_text(
        "import json, os\njson.dumps = lambda *a, **k: '{}'\ndef f(x):\n    return x * 2\n"
    )
    out = outcome(workdir)
    assert out.passed is False and out.code == "FAIL_CHECKER_PATCH"


def test_swallowing_exceptions_in_the_candidate_cannot_change_the_judge(workdir):
    swallow = (
        "import sys\nsys.excepthook = lambda *a: None\n"
        "def f(x):\n    try:\n        raise AssertionError('wrong')\n    except BaseException:\n        return 4 - 1\n"
    )
    (workdir / "m.py").write_text(swallow)
    assert outcome(workdir).passed is False


def test_patching_builtins_does_not_reach_the_probe_and_is_flagged(workdir):
    (workdir / "m.py").write_text(
        "import builtins\nbuiltins.list = lambda *a: [3, 1, 2]\ndef f(x):\n    return x * 2\n"
    )
    probe = "import m\nxs = [9, 8]\nrec('n', len(xs))\nrec('l', list(xs))\n"
    run = run_check(workdir, "H1", probe)
    assert run.status == "integrity" and "builtins" in run.detail
    assert outcome_from_run(CHECK, run).code == "FAIL_CHECKER_PATCH"


def test_breaking_the_import_system_through_builtins_is_flagged_not_trusted(workdir):
    (workdir / "m.py").write_text(
        "import builtins\nbuiltins.len = lambda *a: 0\ndef f(x):\n    return x * 2\n"
    )
    run = run_check(workdir, "H1", PROBE)
    assert run.status == "integrity" and "builtins" in run.detail
    assert outcome_from_run(CHECK, run).passed is False


def test_incomplete_observation_stream_without_a_probe_error_is_tampering(workdir):
    (workdir / "m.py").write_text(
        "import os\nos.write(1, b'\\nOBS\\t{}\\n')\nos._exit(0)\ndef f(x):\n    return x\n"
    )
    out = outcome(workdir)
    assert out.code == "FAIL_CHECKER_PATCH" and "capability" in out.detail


@pytest.mark.parametrize("task_id", sorted(REGISTRY))
def test_every_dev_task_has_n15_controls_and_none_passes(task_id):
    task = REGISTRY[task_id]()
    controls = [n for n in task.negatives if n.id == "N15"]
    assert controls, task_id
    gold = gold_ends(task)
    for neg in controls:
        ends = neg.end(task.base_files, gold)
        with materialize(task, ends, reports_for(task, ends)) as attempt:
            ev = evaluate_attempt(task, attempt)
        assert ev.verdict == codes.FAIL, (task_id, neg.name, ev.verdict)


def test_forging_with_a_correct_report_in_the_read_only_task_is_a_forbidden_write():
    task = REGISTRY["B4-EXP-D"]()
    neg = next(n for n in task.negatives if n.id == "N15")
    ends = neg.end(task.base_files, gold_ends(task))
    with materialize(task, ends, reports_for(task, ends)) as attempt:
        assert evaluate_attempt(task, attempt).fail_code == "FAIL_FORBIDDEN_WRITE"


FORBIDDEN_TEST_SOURCES = {
    "reads the SUT source": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(open('shop/discount.py').read(), '')\n",
    "imports os": "import os\nimport unittest\n",
    "imports sys": "import sys\n",
    "relative import": "from . import helpers\n",
    "takes over run": "import unittest\nclass T(unittest.TestCase):\n    def run(self, result=None):\n        return None\n",
    "load_tests hook": "import unittest\ndef load_tests(loader, tests, pattern):\n    return tests\n",
    "custom result class": "import unittest\nclass R(unittest.TestResult):\n    pass\n",
    "code object access": "from shop.discount import apply_discount\nX = apply_discount.__code__\n",
    "getattr": "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        getattr(self, 'x', None)\n",
    "print": "print('OBS')\n",
    "exec": "exec('x = 1')\n",
}


@pytest.mark.parametrize("name", sorted(FORBIDDEN_TEST_SOURCES))
def test_forbidden_constructs_in_candidate_written_tests_are_flagged(name):
    from local_llm_eval.natives import forbidden_constructs

    result = forbidden_constructs(FORBIDDEN_TEST_SOURCES[name], ("shop",))
    if name == "getattr":
        assert result == []  # protected ordinary lookup is now permitted
    else:
        assert result, name


def test_ordinary_tests_are_not_flagged():
    from local_llm_eval.natives import forbidden_constructs
    from local_llm_eval.tasks.rec import ALT_TESTS
    from local_llm_eval.tasks.rec import GOLD_TESTS as REC_GOLD
    from local_llm_eval.tasks.tst import ALT_TESTS as TST_ALT
    from local_llm_eval.tasks.tst import GOLD_TESTS as TST_GOLD

    for source in (REC_GOLD, ALT_TESTS, TST_GOLD, TST_ALT):
        assert forbidden_constructs(source, ("shop",)) == []
