"""Adversarial controls for the admitted candidate execution boundary."""

import pytest
from local_llm_eval import codes
from local_llm_eval.evaluator import evaluate_attempt
from local_llm_eval.simulate import gold_ends, materialize, reports_for
from local_llm_eval.tasks import REGISTRY
from local_llm_eval.tasks.sgl import GOOD

# No answer constants: only copy list arguments passed through the probe's helper.
COPY_INPUT_ATTACK = """import sys
_frame = sys._getframe()
while _frame is not None:
    _ns = _frame.f_globals
    if _ns.get("__name__") == "__probe__":
        _original = _ns["attempt"]
        def _copied(fn, *args, **kwargs):
            return _original(fn, *(a[:] if isinstance(a, list) else a for a in args), **kwargs)
        _ns["attempt"] = _copied
        break
    _frame = _frame.f_back
"""
# Still accepts tuple inputs; its only defect is mutating list inputs.
MUTATING_MEDIAN = GOOD.replace(
    "    s = sorted(xs)\n",
    "    if isinstance(xs, list):\n        xs.sort()\n    s = sorted(xs)\n",
)


def evaluate_source(source):
    task = REGISTRY["B4-SGL-D"]()
    ends = gold_ends(task)
    ends[0]["stats.py"] = source
    with materialize(task, ends, reports_for(task, ends)) as attempt:
        return evaluate_attempt(task, attempt)


def test_input_mutation_cannot_be_hidden_by_copying_inside_the_probe():
    plain = evaluate_source(MUTATING_MEDIAN)
    attacked = evaluate_source(COPY_INPUT_ATTACK + MUTATING_MEDIAN)
    print(
        f"plain={plain.verdict}/{plain.fail_code} helper_attack={attacked.verdict}/{attacked.fail_code}"
    )
    assert plain.verdict == codes.FAIL and plain.fail_code == "FAIL_REGRESSION"
    assert attacked.verdict == codes.FAIL


@pytest.mark.parametrize("task_id", sorted(REGISTRY))
def test_generalized_attacks_rejected_on_correct_solutions(task_id):
    from local_llm_eval.negatives import INTEGRITY_ATTACKS

    task = REGISTRY[task_id]()
    controls = [n for n in task.negatives if n.id in INTEGRITY_ATTACKS]
    assert {n.id for n in controls} == set(INTEGRITY_ATTACKS)
    for control in controls:
        ends = control.end(task.base_files, gold_ends(task))
        extra = task.gold_reports(task.base_files, ends) if task.gold_reports else None
        with materialize(
            task, ends, reports_for(task, ends, extra), session=task.gold_session
        ) as attempt:
            ev = evaluate_attempt(task, attempt)
        assert ev.verdict == codes.FAIL and ev.fail_code in control.expect_codes, (
            task_id,
            control.id,
            ev,
        )


def test_ordinary_attribute_assignment_preserves_python_evaluation_and_aliases(
    tmp_path,
):
    from local_llm_eval.runner import run_check

    (tmp_path / "m.py").write_text("""class State:
    def __init__(self):
        self.a, self.b = [], 2
    def update(self):
        alias = self.a
        self.a += [1]
        for self.b in [3, 4]:
            pass
        with open('data.txt', 'w') as self.handle:
            self.handle.write('ok')
        return alias == self.a and alias is self.a, self.b
""")
    run = run_check(
        tmp_path, "semantics", "import m\nrec('v', attempt(m.State().update))"
    )
    assert run.obs.get("v") == {"ok": [True, 4]}, run


@pytest.mark.parametrize(
    "binding",
    [
        "def f(_llme_get=None): pass",
        "import math as _llme_slot",
        "from math import sqrt as _llme_get",
        "try:\n    pass\nexcept Exception as _llme_slot:\n    pass",
        "match {}:\n    case {**_llme_get}: pass",
    ],
)
def test_generated_guard_names_cannot_be_shadowed(binding):
    from local_llm_eval.integrity import violations

    assert violations(binding)


def test_sealed_import_ignores_source_rewritten_by_candidate(tmp_path):
    from local_llm_eval.runner import run_check

    (tmp_path / "m.py").write_text(
        "open('n.py', 'w').write('value = 999\\n')\nimport n\nvalue = n.value\n"
    )
    (tmp_path / "n.py").write_text("value = 4\n")
    run = run_check(tmp_path, "sealed", "import m\nrec('v', m.value)")
    assert run.status == "OBS" and run.obs["v"] == 4
    assert (tmp_path / "n.py").read_text() == "value = 999\n"


@pytest.mark.parametrize(
    "expression", ["sys._getframe()", "gc.get_referrers(object())"]
)
def test_audit_records_a_caught_attempt_independently_of_static_admission(
    tmp_path, expression
):
    from local_llm_eval.runner import run_check

    # The trusted test driver deliberately bypasses source admission to exercise
    # the audit layer alone; candidate code cannot call compile/exec.
    path = tmp_path / "m.py"
    path.write_text("X = 1\n")
    code = (
        "import sys, gc\ntry:\n    "
        + expression
        + "\nexcept BaseException:\n    pass\n"
    )
    probe = f"exec(compile({code!r}, {str(path)!r}, 'exec'))\nrec('v', 4)"
    run = run_check(tmp_path, "audit", probe)
    assert any("audit " in item for item in run.obs["__tampered__"])


@pytest.mark.parametrize("name", ["__main__.py", "integrity.py", "json.py"])
def test_reserved_source_module_cannot_expose_a_trusted_module(tmp_path, name):
    from local_llm_eval.runner import run_check

    (tmp_path / name).write_text("value = 1\n")
    assert run_check(tmp_path, "collision", "rec('v', 4)").status == "integrity"


def test_valid_looking_observations_with_nonzero_exit_are_never_accepted(tmp_path):
    from local_llm_eval.runner import run_check

    # Trusted driver simulates a broken transport; no candidate restrictions involved.
    run = run_check(tmp_path, "exit", "print('OBS\\t{}')\nimport os\nos._exit(3)")
    assert run.status == "crash" and run.facts["returncode"] == 3
