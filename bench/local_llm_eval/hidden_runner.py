"""Child process for one hidden check. Run as: python -I -S -B hidden_runner.py <spec.json>.

The spec file (check id, nonce, code, cwd) is deleted right after it is read. The result is reported
only by a handshake line `HIDDEN_DONE\t<nonce>\t<check_id>\t<STATUS>\t<json>` so that an exit code of 0
without the handshake (import-time `os._exit(0)`, forged output) is never mistaken for success.

Known limit: candidate code is imported into this same process, so a determined attacker could read the
nonce from interpreter state. The CPU stage accepts that; Seatbelt/process isolation belongs to 4.3.
"""

import io
import json
import os
import sys
import traceback
import unittest


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
        "ok": result.wasSuccessful(),
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
        "os._exit": os._exit,
    }


def main():
    spec_path = sys.argv[1]
    with open(spec_path) as fh:
        spec = json.load(fh)
    os.unlink(spec_path)
    sys.argv[:] = ["hidden"]
    nonce, check_id, cwd = spec["nonce"], spec["check_id"], spec["cwd"]
    os.chdir(cwd)
    sys.path.insert(0, cwd)
    before = _tamper_probe()
    namespace = {
        "__name__": "__hidden__",
        "W": cwd,
        "run_unittest": run_unittest,
        "RESULT": {},
    }
    status, detail = "OK", ""
    try:
        exec(compile(spec["code"], f"<check {check_id}>", "exec"), namespace)  # noqa: S102
    except SystemExit as exc:
        status, detail = "EXIT", f"SystemExit({exc.code!r})"
    except BaseException as exc:  # noqa: BLE001 - every candidate failure must be reported
        status = "FAIL"
        tb = traceback.format_exception_only(type(exc), exc)
        detail = "".join(tb).strip()[-400:]
    after = _tamper_probe()
    changed = sorted(k for k in before if before[k] is not after[k])
    if status == "OK" and changed:
        status, detail = "TAMPER", "patched: " + ",".join(changed)
    payload = {"detail": detail, "result": namespace.get("RESULT") or {}}
    sys.stdout.write(
        f"\nHIDDEN_DONE\t{nonce}\t{check_id}\t{status}\t{json.dumps(payload)}\n"
    )
    sys.stdout.flush()


main()
