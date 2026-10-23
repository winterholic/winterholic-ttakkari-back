"""Reusable checks: public unittest suite, frozen files, candidate-written test suites (mutants)."""

import ast
import fnmatch
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import codes
from .manifest import write_files
from .model import Check, CheckOutcome, Ctx
from .runner import CheckRun, run_check

SKIP_NAMES = {"skip", "skipIf", "skipUnless", "expectedFailure", "skipTest", "xfail"}


def unittest_snippet(
    min_tests: int, start_dir: str = "tests", pattern: str = "test*.py"
) -> str:
    """Hidden-runner snippet: the unittest suite must be green, non-empty and skip-free."""
    return f"""
r = run_unittest({start_dir!r}, ".", {pattern!r})
RESULT = dict(r)
if r["skipped"] or r["expected_failures"] or r["unexpected_successes"]:
    RESULT["code"] = "FAIL_SKIP_XFAIL"
    raise AssertionError("skip/xfail present: %s" % r)
if r["tests"] < {min_tests}:
    RESULT["code"] = "FAIL_EMPTY_RESULT" if r["tests"] == 0 else "FAIL_PUBLIC"
    raise AssertionError("collected %d tests, need {min_tests}" % r["tests"])
assert r["ok"], r["tail"] or "unittest failed"
"""


def public_suite(
    check_id: str, min_tests: int, kind: str, requirement: str = ""
) -> Check:
    return Check(
        id=check_id,
        kind=kind,
        requirement=requirement,
        snippet=unittest_snippet(min_tests),
        fail_code="FAIL_PUBLIC",
        public=True,
    )


def frozen_unchanged(
    check_id: str, patterns: tuple[str, ...], requirement: str = ""
) -> Check:
    def run(ctx: Ctx) -> CheckOutcome:
        bad = [
            p
            for p, content in ctx.base_files.items()
            if any(fnmatch.fnmatchcase(p, pat) for pat in patterns)
            and ctx.end_files.get(p) != content
        ]
        if bad:
            return CheckOutcome(False, "FAIL_TEST_TAMPER", f"frozen changed: {bad[:5]}")
        return CheckOutcome(True)

    return Check(check_id, codes.KIND_INVARIANT, requirement, native=run)


# ---- candidate-written test suites -------------------------------------------------------


@dataclass
class SuiteSpec:
    test_path: str
    sut_names: tuple[str, ...]
    gold_overrides: dict[
        str, str
    ]  # canonical SUT files used instead of the candidate's
    mutants: dict[str, dict[str, str]]  # name -> overriding SUT files
    min_tests: int
    groups: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )  # check id -> mutant names


def analyze_tests(source: str, sut_names: tuple[str, ...]) -> dict:
    """Static facts about a candidate test file (no execution)."""
    info = {
        "parse_error": False,
        "tests": 0,
        "weak": [],
        "skip_markers": False,
        "has_comments": False,
    }
    info["has_comments"] = any(
        line.lstrip().startswith("#") for line in source.splitlines()
    )
    try:
        tree = ast.parse(source)
    except SyntaxError:
        info["parse_error"] = True
        return info
    for node in ast.walk(tree):
        names = set()
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        if names & SKIP_NAMES:
            info["skip_markers"] = True
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test"):
            continue
        info["tests"] += 1
        has_assert = False
        refs_sut = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assert) or (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr.startswith("assert")
            ):
                has_assert = True
            if isinstance(sub, ast.Name) and sub.id in sut_names:
                refs_sut = True
            if isinstance(sub, ast.Attribute) and sub.attr in sut_names:
                refs_sut = True
        if not (has_assert and refs_sut):
            info["weak"].append(node.name)
    return info


def _suite_run(
    ctx: Ctx, spec: SuiteSpec, label: str, overrides: dict[str, str]
) -> CheckRun:
    source = ctx.end_files.get(spec.test_path)
    files = dict(ctx.base_files)
    files.update(overrides)
    for path, content in ctx.end_files.items():
        if path.startswith("tests/"):
            files[path] = content
    if source is not None:
        files[spec.test_path] = source
    pattern = spec.test_path.rsplit("/", 1)[-1]
    with tempfile.TemporaryDirectory(prefix=f"llme-suite-{label}-") as tmp:
        write_files(Path(tmp), files)
        code = f'RESULT = dict(run_unittest("tests", ".", {pattern!r}))\n'
        return run_check(Path(tmp), f"suite-{label}", code, 30.0)


def _suite_cache(ctx: Ctx, spec: SuiteSpec) -> dict:
    key = ("suite", spec.test_path)
    if key in ctx.cache:
        return ctx.cache[key]
    names = list(spec.mutants)
    with ThreadPoolExecutor(max_workers=4) as pool:
        gold = pool.submit(_suite_run, ctx, spec, "gold", spec.gold_overrides)
        mut = {
            n: pool.submit(
                _suite_run, ctx, spec, n, {**spec.gold_overrides, **spec.mutants[n]}
            )
            for n in names
        }
        cache = {
            "gold": gold.result(),
            "mutants": {n: f.result() for n, f in mut.items()},
        }
    cache["info"] = analyze_tests(ctx.end_files.get(spec.test_path, ""), spec.sut_names)
    ctx.cache[key] = cache
    return cache


def _structural_run_failure(run: CheckRun) -> CheckOutcome | None:
    if run.status in ("stub_exit0", "EXIT"):
        return CheckOutcome(False, "FAIL_STUB_EXIT0", run.detail)
    if run.status in ("forged", "TAMPER"):
        return CheckOutcome(False, "FAIL_CHECKER_PATCH", run.detail)
    return None


def _empty_code(info: dict) -> str:
    return "FAIL_COMMENT_ONLY" if info["has_comments"] else "FAIL_EMPTY_RESULT"


def suite_collected(
    check_id: str,
    spec: SuiteSpec,
    requirement: str = "",
    kind: str = codes.KIND_DELTA,
) -> Check:
    def run(ctx: Ctx) -> CheckOutcome:
        data = _suite_cache(ctx, spec)
        structural = _structural_run_failure(data["gold"])
        if structural:
            return structural
        count = data["gold"].result.get("tests", 0)
        if count == 0:
            return CheckOutcome(False, _empty_code(data["info"]), "0 tests collected")
        if count < spec.min_tests:
            return CheckOutcome(
                False, "FAIL_HIDDEN", f"{count} tests < {spec.min_tests}"
            )
        return CheckOutcome(True, metrics={"tests": count})

    return Check(check_id, kind, requirement, native=run)


def suite_gold_pass(
    check_id: str,
    spec: SuiteSpec,
    requirement: str = "",
    kind: str = codes.KIND_DELTA,
) -> Check:
    def run(ctx: Ctx) -> CheckOutcome:
        data = _suite_cache(ctx, spec)
        structural = _structural_run_failure(data["gold"])
        if structural:
            return structural
        res = data["gold"].result
        if (
            res.get("skipped")
            or res.get("expected_failures")
            or res.get("unexpected_successes")
        ):
            return CheckOutcome(False, "FAIL_SKIP_XFAIL", str(res))
        if res.get("tests", 0) == 0:
            return CheckOutcome(False, _empty_code(data["info"]), "0 tests collected")
        if not res.get("ok"):
            return CheckOutcome(
                False, "FAIL_HIDDEN", "candidate tests fail on the gold SUT"
            )
        return CheckOutcome(True)

    return Check(check_id, kind, requirement, native=run)


def suite_kills(
    check_id: str,
    spec: SuiteSpec,
    mutant_names: tuple[str, ...],
    requirement: str = "",
    kind: str = codes.KIND_DELTA,
) -> Check:
    def run(ctx: Ctx) -> CheckOutcome:
        data = _suite_cache(ctx, spec)
        survivors = []
        for name in mutant_names:
            run_ = data["mutants"][name]
            structural = _structural_run_failure(run_)
            if structural:
                return structural
            res = run_.result
            killed = res.get("failures", 0) + res.get("errors", 0) > 0
            if not killed:
                survivors.append(name)
        if survivors:
            return CheckOutcome(False, "FAIL_HIDDEN", f"mutants survived: {survivors}")
        return CheckOutcome(True, metrics={"killed": list(mutant_names)})

    return Check(check_id, kind, requirement, native=run)


def suite_quality(
    check_id: str,
    spec: SuiteSpec,
    requirement: str = "",
    kind: str = codes.KIND_DELTA,
) -> Check:
    def run(ctx: Ctx) -> CheckOutcome:
        info = _suite_cache(ctx, spec)["info"]
        if info["parse_error"]:
            return CheckOutcome(False, "FAIL_HIDDEN", "test file does not parse")
        if info["skip_markers"]:
            return CheckOutcome(
                False, "FAIL_SKIP_XFAIL", "skip/xfail marker in test file"
            )
        if info["tests"] == 0:
            return CheckOutcome(False, _empty_code(info), "no test functions")
        if info["weak"]:
            return CheckOutcome(
                False,
                "FAIL_COMMENT_ONLY",
                f"tests without SUT assertion: {info['weak'][:5]}",
            )
        return CheckOutcome(True, metrics={"tests": info["tests"]})

    return Check(check_id, kind, requirement, native=run)


def unchanged_paths(
    check_id: str, paths: tuple[str, ...], requirement: str = ""
) -> Check:
    def run(ctx: Ctx) -> CheckOutcome:
        bad = [p for p in paths if ctx.end_files.get(p) != ctx.base_files.get(p)]
        if bad:
            return CheckOutcome(
                False, "FAIL_FORBIDDEN_WRITE", f"must stay unchanged: {bad}"
            )
        return CheckOutcome(True)

    return Check(check_id, codes.KIND_INVARIANT, requirement, native=run)
