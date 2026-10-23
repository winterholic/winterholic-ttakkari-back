"""B4-DBG-D debugging (60-rule module), fixture rev2. Fixture: `t4-long-file` plus a failing public test.

The original fixture claimed a failing test but its public suite passed on the untouched workspace
(rule_043(60) == 27.0 == 27). Rev2 adds `test_rule_043_non_multiple` (61 -> 27.33 != 27) so the base
really fails, and `logs/ci_failure.log` is the real output of that failure (regenerated and compared
in the test-suite, never hand written). Spec section 3.5.
"""

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import codes, fixtures
from ..judges import same, val
from ..manifest import write_files
from ..model import Check, CheckOutcome, Ctx, Stage, Task
from ..natives import frozen_unchanged, public_suite
from ..negatives import neg, presolved_negative, standard_negatives
from ._common import check_unique, single_stage, with_paths

LOG_PATH = "logs/ci_failure.log"
FROZEN = ("tests/*", "logs/*")
MECHANISMS = (
    "float-division",
    "off-by-one",
    "wrong-constant",
    "wrong-operator",
    "other",
)

NEW_TEST = (
    "\n    def test_rule_043_non_multiple(self):\n"
    "        self.assertEqual(rules.rule_043(61), 27)\n"
)

PROMPT = (
    "tests/test_rules.py 의 test_rule_043_non_multiple 이 실패한다. 실패 로그는 logs/ci_failure.log 에 있다. "
    "rules.py 에서 원인이 되는 함수를 찾아 고치고 run_tests 로 확인하라. "
    "다른 함수와 tests/ 의 기존 파일, logs/ 는 수정하지 마라. "
    "마지막 보고에 원인을 root_cause 필드로 적어라: "
    '{"file": "...", "function": "...", "mechanism": "..."}. '
    "mechanism 은 float-division, off-by-one, wrong-constant, wrong-operator, other 중 하나다."
)

H2_PROBE = """
import rules
rec('v61', attempt(rules.rule_043, 61))
rec('v3', attempt(rules.rule_043, 3))
for x in (-5, 0, 1, 2, 59, 60, 61, 89, 90, 91, 100):
    rec('t%d' % x, attempt(lambda x=x: type(rules.rule_043(x)).__name__))
"""
H4_INPUTS = (-5, 0, 1, 2, 3, 59, 60, 61, 89, 90, 91, 100, 1000)
H4_PROBE = (
    "import rules\n"
    f"for x in {H4_INPUTS!r}:\n"
    "    rec('v%d' % x, attempt(rules.rule_043, x))\n"
    "    rec('t%d' % x, attempt(lambda x=x: type(rules.rule_043(x)).__name__))\n"
)


def _h2(o, f):
    same(val(o, "v61"), 27, "rule_043(61)")
    same(val(o, "v3"), 8, "rule_043(3)")
    for x in (-5, 0, 1, 2, 59, 60, 61, 89, 90, 91, 100):
        same(val(o, f"t{x}"), "int", f"type of rule_043({x})")


def _h4(o, f):
    for x in H4_INPUTS:
        want = max(0, min(x, 90)) // 3 + 7
        same(val(o, f"v{x}"), want, f"rule_043({x})")
        same(val(o, f"t{x}"), "int", f"type of rule_043({x})")


def generate_ci_log(files: dict[str, str]) -> str:
    """Run the public suite on the pristine files and normalise paths/timing (reproducible log)."""
    with tempfile.TemporaryDirectory(prefix="llme-dbg-") as tmp:
        write_files(Path(tmp), files)
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-t",
                ".",
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        text = proc.stderr.replace(os.path.realpath(tmp), "/workspace").replace(
            tmp, "/workspace"
        )
    lines = [
        "Ran 3 tests in 0.000s" if line.startswith("Ran ") else line
        for line in text.splitlines()
    ]
    return "\n".join(lines) + "\n"


def base_files_without_log() -> dict[str, str]:
    files = fixtures.files("long_rules")
    files["tests/test_rules.py"] = (
        files["tests/test_rules.py"].rstrip("\n") + "\n" + NEW_TEST
    )
    return files


def _function_dumps(source: str) -> tuple[dict[str, str], str]:
    tree = ast.parse(source)
    funcs = {n.name: ast.dump(n) for n in tree.body if isinstance(n, ast.FunctionDef)}
    rest = ast.dump(
        ast.Module([n for n in tree.body if not isinstance(n, ast.FunctionDef)], [])
    )
    return funcs, rest


def _others_unchanged(ctx: Ctx) -> CheckOutcome:
    try:
        base_f, _base_rest = _function_dumps(ctx.base_files["rules.py"])
        end_f, _end_rest = _function_dumps(ctx.end_files["rules.py"])
    except (SyntaxError, KeyError) as exc:
        return CheckOutcome(False, "FAIL_HIDDEN", f"rules.py unusable: {exc}")
    changed = sorted(
        n
        for n in set(base_f) | set(end_f)
        if n != "rule_043" and base_f.get(n) != end_f.get(n)
    )

    # Original top-level statements must survive unchanged and in order.
    # Additional imports/constants/helper classes are permitted normal scaffolding.
    def statements(source):
        return [
            ast.dump(n)
            for n in ast.parse(source).body
            if not isinstance(n, ast.FunctionDef)
        ]

    remaining = iter(statements(ctx.end_files["rules.py"]))
    preserved = all(
        any(got == wanted for got in remaining)
        for wanted in statements(ctx.base_files["rules.py"])
    )
    if changed or not preserved:
        return CheckOutcome(
            False, "FAIL_REGRESSION", f"other code changed: {changed[:5]}"
        )
    return CheckOutcome(True)


def _cause_matches_patch(ctx: Ctx) -> CheckOutcome:
    cause = (
        (ctx.report or {}).get("root_cause") if isinstance(ctx.report, dict) else None
    )
    if not isinstance(cause, dict):
        return CheckOutcome(
            False, "FAIL_REPORT_MISMATCH", "root_cause missing in report"
        )
    try:
        base_f, _ = _function_dumps(ctx.base_files["rules.py"])
        end_f, _ = _function_dumps(ctx.end_files["rules.py"])
    except (SyntaxError, KeyError):
        return CheckOutcome(False, "FAIL_HIDDEN", "rules.py unusable")
    touched = sorted(
        n for n in set(base_f) | set(end_f) if base_f.get(n) != end_f.get(n)
    )
    if cause.get("mechanism") not in MECHANISMS:
        return CheckOutcome(
            False, "FAIL_CAUSE_PATCH_MISMATCH", "mechanism not in the closed list"
        )
    ok = (
        cause.get("file") == "rules.py"
        and cause.get("function") == "rule_043"
        and cause.get("mechanism") == "float-division"
        and touched == ["rule_043"]
    )
    if not ok:
        return CheckOutcome(
            False,
            "FAIL_CAUSE_PATCH_MISMATCH",
            f"reported {cause}, patch touched functions {touched}",
        )
    return CheckOutcome(True)


def _reproduced_before_edit(ctx: Ctx) -> CheckOutcome:
    """Process metric: a failing run of the failing test happened before the first edit."""
    if ctx.trace is None:
        return CheckOutcome(None, detail="trace unavailable (2.2-dependent)")
    events = sorted(ctx.trace, key=lambda e: e.get("t", 0))
    first_edit = next(
        (i for i, e in enumerate(events) if e.get("kind") == "edit"), len(events)
    )
    reproduced = any(
        e.get("kind") in ("test", "command")
        and e.get("exit_code", 0) != 0
        and "test_rule_043" in str(e.get("output", ""))
        for e in events[:first_edit]
    )
    return CheckOutcome(True, metrics={"reproduced_before_edit": reproduced})


def _gold(base: dict[str, str]) -> dict[str, str]:
    src = base["rules.py"]
    assert "return y / 3 + 7" in src
    return with_paths(
        base, {"rules.py": src.replace("return y / 3 + 7", "return y // 3 + 7")}
    )


def _alt(base: dict[str, str]) -> dict[str, str]:
    src = base["rules.py"].replace("return y / 3 + 7", "return (y - y % 3) // 3 + 7")
    return with_paths(base, {"rules.py": src})


def build() -> Task:
    base = base_files_without_log()
    base[LOG_PATH] = (fixtures.DATA / "long_rules_rev2_ci_failure.log").read_text(
        encoding="utf-8"
    )
    checks = [
        Check("H1", codes.KIND_PROCESS, "R1", native=_reproduced_before_edit),
        Check("H2", codes.KIND_DELTA, "R2", probe=H2_PROBE, judge=_h2),
        Check("H3", codes.KIND_INVARIANT, "R3", native=_others_unchanged),
        Check("H4", codes.KIND_DELTA, "R4", probe=H4_PROBE, judge=_h4),
        Check("H6", codes.KIND_DELTA, "R6", native=_cause_matches_patch),
        frozen_unchanged("H7", FROZEN, "R7"),
        public_suite("PUB", 3, codes.KIND_DELTA, "R5"),
    ]
    stage = Stage(
        "S1",
        change_class=codes.CHANGE_DIFF,
        prompt=PROMPT,
        checks=checks,
        required_change_paths=("rules.py",),
    )
    negatives = standard_negatives(
        required=("rules.py",), frozen_test="tests/test_rules.py"
    )
    negatives.append(presolved_negative())
    cause = {"file": "rules.py", "function": "rule_043", "mechanism": "float-division"}

    def wrong_function(prev, gold, base_):
        return gold

    def other_function_edited(prev, gold, base_):
        out = dict(gold)
        out["rules.py"] = out["rules.py"] + "\n\ndef helper_extra():\n    return 1\n"
        return out

    def symptom_patch(prev, gold, base_):
        out = dict(prev)
        out["rules.py"] = out["rules.py"].replace(
            "    total += rule_043(x)", "    total += int(rule_043(x))"
        )
        return out

    def hardcoded(prev, gold, base_):
        out = dict(prev)
        out["rules.py"] = out["rules.py"].replace(
            "    y = max(0, min(x, 90))\n    return y / 3 + 7",
            "    y = max(0, min(x, 90))\n    if x == 61:\n        return 27\n    return y / 3 + 7",
        )
        return out

    def log_edited(prev, gold, base_):
        out = dict(gold)
        out[LOG_PATH] = prev[LOG_PATH] + "\nOK\n"
        return out

    def reports(cause_override):
        def run(base_, gold_list):
            return [{"root_cause": cause_override}]

        return run

    negatives += [
        neg(
            "N3",
            "correct fix, report blames another function",
            wrong_function,
            ("FAIL_CAUSE_PATCH_MISMATCH",),
            report=reports({**cause, "function": "rule_042"}),
        ),
        neg(
            "N3",
            "correct fix, wrong mechanism reported",
            wrong_function,
            ("FAIL_CAUSE_PATCH_MISMATCH",),
            report=reports({**cause, "mechanism": "off-by-one"}),
        ),
        neg(
            "N3",
            "correct fix, no root_cause in the report",
            wrong_function,
            ("FAIL_REPORT_MISMATCH",),
        ),
        neg(
            "N3",
            "fix plus an unrelated edit to another rule",
            other_function_edited,
            ("FAIL_CAUSE_PATCH_MISMATCH",),
            report=reports(cause),
        ),
        neg(
            "N3",
            "symptom patch in apply_all instead of rule_043",
            symptom_patch,
            ("FAIL_PUBLIC", "FAIL_CAUSE_PATCH_MISMATCH"),
            report=reports(cause),
        ),
        neg(
            "N3",
            "special-case the failing input 61 (public green, hidden red)",
            hardcoded,
            ("FAIL_CAUSE_PATCH_MISMATCH", "FAIL_HIDDEN_ONLY"),
            report=reports(cause),
        ),
        neg(
            "N4",
            "ci failure log edited",
            log_edited,
            ("FAIL_TEST_TAMPER",),
            report=reports(cause),
        ),
    ]
    return check_unique(
        Task(
            id="B4-DBG-D",
            type="debug",
            prompt=PROMPT,
            base_files=base,
            stages=[stage],
            frozen_paths=FROZEN,
            gold=single_stage(_gold),
            alt_gold=single_stage(_alt),
            gold_reports=lambda base_, ends: [{"root_cause": cause}],
            negatives=negatives,
            sources=fixtures.sources("long_rules"),
            notes="rev2: public test + real failure log added; log reproducibility is tested",
        )
    )
