"""Candidate-side process for one probe. Run as: python -I -S -B hidden_runner.py <spec.json>.

This process executes candidate code and returns RAW OBSERVATIONS only (one `OBS\t<json>` line written to
fd 1). It holds no secret, no expected value and no verdict: the spec file holds the probe program and the
parent-provided scratch dirs, nothing that would let candidate code decide a check. All comparison with
expected values happens in the evaluator process (judges), which never imports candidate code.

What candidate code in this process can still do: print or exit early. An early exit leaves no OBS line
(stub), several OBS lines are tampering, and a forged OBS line must carry values equal to the judge's
expectations, which are not in this process. See docs: evaluator-implementation-4-2.md section 9.
"""

import builtins
import io
import json
import os
import sys
import traceback
import unittest

# References captured before any candidate import so later monkeypatching of the originals is harmless here.
_dumps = json.dumps
_write = os.write
_isinstance = isinstance
_str = str
_repr = repr
_sorted = sorted
_type = type
_BASE = BaseException
_list, _tuple, _set, _frozenset, _dict, _bytes = (
    list,
    tuple,
    set,
    frozenset,
    dict,
    bytes,
)
_int, _float, _bool = int, float, bool


def jsonable(value):
    if value is None or _isinstance(value, (_bool, _int, _float, _str)):
        return value
    if _isinstance(value, (_list, _tuple)):
        return [jsonable(v) for v in value]
    if _isinstance(value, (_set, _frozenset)):
        return [jsonable(v) for v in _sorted(value, key=_repr)]
    if _isinstance(value, _dict):
        return {_str(k): jsonable(v) for k, v in value.items()}
    if _isinstance(value, _bytes):
        return value.decode("utf-8", "replace")
    return {"__repr__": _repr(value)[:300]}


def run_unittest(start_dir="tests", top=".", pattern="test*.py"):
    suite = unittest.TestLoader().discover(start_dir, pattern, top_level_dir=top)
    stream = io.StringIO()
    result = unittest.TextTestResult(stream, False, 0)
    suite.run(result)
    return {
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "tail": "\n".join(
            text.strip().splitlines()[-1] if text.strip() else ""
            for _, text in (result.failures + result.errors)[:3]
        ),
    }


def _tamper_probe():
    case = unittest.TestCase
    result = unittest.TestResult
    return {
        "TestCase.run": case.run,
        "TestCase.assertEqual": case.assertEqual,
        "TestCase.assertRaises": case.assertRaises,
        "TestCase.assertTrue": case.assertTrue,
        "TestResult.addFailure": result.addFailure,
        "TestResult.addError": result.addError,
        "TestResult.wasSuccessful": result.wasSuccessful,
        "unittest.main": unittest.main,
        "json.dumps": json.dumps,
        "os.write": os.write,
    }


def main():
    spec_path = sys.argv[1]
    with open(spec_path) as fh:
        spec = json.load(fh)
    os.unlink(spec_path)
    sys.argv[:] = ["probe"]
    cwd = spec["cwd"]
    os.chdir(cwd)
    sys.path.insert(0, cwd)
    before = _tamper_probe()
    saved_builtins = dict(vars(builtins))
    observations = {}

    def rec(name, value):
        observations[_str(name)] = jsonable(value)

    def attempt(fn, *args, **kwargs):
        try:
            return {"ok": jsonable(fn(*args, **kwargs))}
        except _BASE as exc:
            return {
                "raised": [c.__name__ for c in _type(exc).__mro__],
                "msg": _str(exc)[:200],
            }

    # the probe sees a private copy of the builtins so patching `builtins.list` etc. cannot reach it
    private_builtins = dict(vars(builtins))
    namespace = {
        "__name__": "__probe__",
        "__builtins__": private_builtins,
        "W": cwd,
        "DIRS": spec.get("dirs", {}),
        "rec": rec,
        "attempt": attempt,
        "jsonable": jsonable,
        "run_unittest": run_unittest,
    }
    failure = None
    try:
        exec(compile(spec["code"], "<probe>", "exec"), namespace)  # noqa: S102
    except _BASE as exc:
        failure = exc  # formatted only after the builtins are restored (candidate code may patch them)
    after = _tamper_probe()
    changed = _sorted(k for k in before if before[k] is not after[k])
    live = vars(builtins)
    changed += _sorted(
        "builtins." + k
        for k in saved_builtins
        if live.get(k, None) is not saved_builtins[k]
    )
    vars(builtins).update(saved_builtins)
    if failure is not None:
        if _isinstance(failure, SystemExit):
            observations["__error__"] = f"SystemExit({failure.code!r})"
        else:
            observations["__error__"] = "".join(
                traceback.format_exception_only(_type(failure), failure)
            ).strip()[-400:]
    if changed:
        observations["__tampered__"] = changed
    _write(1, ("\nOBS\t" + _dumps(observations) + "\n").encode("utf-8", "replace"))


main()
