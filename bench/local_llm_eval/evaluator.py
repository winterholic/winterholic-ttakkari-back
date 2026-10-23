"""Independent evaluator (spec section 5.1): reset check, structural gates, delta/invariant checks.

Order of judgement per stage: (a) start state equals the pristine/previous-end manifest,
(b) forbidden writes and checker tampering, (c) no-op / comment-only / required paths,
(d) delta and invariant checks in a clean tree, (e) structured report reconciliation.
Model self-report never makes an attempt pass; it can only add a failure.
"""

import ast
import fnmatch
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import codes
from .integrity import violations
from .judges import Fail, MissingObservation, Obs
from .manifest import (
    changed_paths,
    diff_manifests,
    dir_manifest,
    manifest_of_files,
    read_files,
    tree_hash,
    write_files,
)
from .model import (
    Attempt,
    Check,
    CheckOutcome,
    Ctx,
    Evaluation,
    Stage,
    StageAttempt,
    StageResult,
    Task,
)
from .runner import CheckRun, run_check

CHECKER_PATCH_NAMES = {
    "conftest.py",
    "sitecustomize.py",
    "usercustomize.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
}
WORKERS = int(os.environ.get("LOCAL_LLM_EVAL_WORKERS", "4"))


class ConfigError(Exception):
    """The task definition itself is unusable (for example a stage without a delta check)."""


def outcome_from_run(check: Check, run: CheckRun) -> CheckOutcome:
    """Map an observation run to an outcome. The judge runs HERE, in the evaluator process."""
    metrics = {"wall_s": round(run.wall_s, 3)}
    if run.status == "stub_exit0":
        return CheckOutcome(False, "FAIL_STUB_EXIT0", run.detail, metrics)
    if run.status in ("integrity", "multi_obs", "bad_obs"):
        return CheckOutcome(False, "FAIL_CHECKER_PATCH", run.detail, metrics)
    if run.status == "timeout":
        return CheckOutcome(False, check.fail_code, "check_timeout", metrics)
    if run.status != "OBS":
        return CheckOutcome(
            False, check.fail_code, run.detail or run.stderr_tail[-200:], metrics
        )
    obs = Obs(run.obs)
    if obs.get("__tampered__") == ["module capability os._exit"]:
        return CheckOutcome(
            False, "FAIL_STUB_EXIT0", "os._exit blocked before premature exit", metrics
        )
    if obs.get("__tampered__"):
        return CheckOutcome(
            False, "FAIL_CHECKER_PATCH", f"patched: {obs['__tampered__']}", metrics
        )
    error = obs.get("__error__")
    if error and error.startswith("SystemExit"):
        return CheckOutcome(False, "FAIL_STUB_EXIT0", error, metrics)
    if error:
        return CheckOutcome(False, check.fail_code, error, metrics)
    try:
        check.judge(obs, run.facts)
    except Fail as exc:
        return CheckOutcome(False, exc.code, exc.detail[:300], metrics)
    except AssertionError as exc:
        return CheckOutcome(
            False, check.fail_code, str(exc)[:300] or "assertion failed", metrics
        )
    except MissingObservation as exc:
        return CheckOutcome(
            False,
            "FAIL_CHECKER_PATCH",
            f"observation {exc} missing: stream cut or forged",
            metrics,
        )
    except Exception as exc:  # noqa: BLE001 - candidate data the judge cannot interpret is a wrong result
        return CheckOutcome(
            False,
            check.fail_code,
            f"malformed observation: {type(exc).__name__}: {exc}"[:300],
            metrics,
        )
    return CheckOutcome(True, None, "", metrics)


def _py_ast_dump(source: str) -> str | None:
    try:
        return ast.dump(ast.parse(source))
    except SyntaxError:
        return None


def _is_comment_only(base: dict[str, str], end: dict[str, str], diff: dict) -> bool:
    """True when every change is a .py file whose AST is unchanged (comments/whitespace)."""
    if diff["deleted"]:
        return False
    paths = diff["added"] + diff["modified"]
    if not paths:
        return False
    for path in paths:
        if not path.endswith(".py"):
            return False
        new = _py_ast_dump(end[path])
        if path in base:
            if new is None or new != _py_ast_dump(base[path]):
                return False
        elif new is None or ast.dump(ast.parse(end[path])) != ast.dump(ast.parse("")):
            # an added file with real statements (docstring-only counts as empty)
            tree = ast.parse(end[path])
            body = [
                n
                for n in tree.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            ]
            if body:
                return False
    return True


def _fail(stage_id: str, code: str, detail: str, **kw) -> StageResult:
    return StageResult(stage_id, codes.FAIL, code, detail=detail, **kw)


def _structural_gate(
    task: Task,
    stage: Stage,
    base_manifest: dict[str, str],
    stage_base_files: dict[str, str],
    end_manifest: dict[str, str],
    end_files: dict[str, str],
    report: dict | None,
) -> tuple[str, str] | None:
    """Return (fail_code, detail) for the first structural failure, else None."""
    for path, digest in end_manifest.items():
        if digest.startswith("symlink:"):
            return "FAIL_FORBIDDEN_WRITE", f"symlink {path}"
        if (path == ".git" or path.startswith(".git/")) and path not in base_manifest:
            return "FAIL_FORBIDDEN_WRITE", f"git metadata {path}"
    total = diff_manifests(base_manifest, end_manifest)
    for path in total["added"] + total["modified"]:
        name = path.rsplit("/", 1)[-1]
        if name in CHECKER_PATCH_NAMES or name.endswith(".pth"):
            return "FAIL_CHECKER_PATCH", f"checker-affecting file {path}"
    for path in total["modified"] + total["deleted"]:
        if any(fnmatch.fnmatchcase(path, pat) for pat in task.frozen_paths):
            return "FAIL_TEST_TAMPER", f"frozen path changed: {path}"
    stage_diff = diff_manifests(manifest_of_files(stage_base_files), end_manifest)
    changed = changed_paths(stage_diff)
    if stage.change_class == codes.CHANGE_READ_ONLY:
        extra = [p for p in changed if p not in stage.allowed_writes]
        if extra:
            return "FAIL_FORBIDDEN_WRITE", f"read-only stage wrote {extra[:5]}"
        if stage.result_artifact is not None:
            missing = stage.result_artifact not in end_files
        else:
            missing = report is None
        if missing:
            return "FAIL_NOOP", "no result artifact/report produced"
        return None
    if not changed:
        return "FAIL_NOOP", "no change from the stage start state"
    required = stage.required_change_paths
    touched = [p for p in required if p in changed]
    for path in touched:
        if path in end_files and not end_files[path].strip():
            return "FAIL_EMPTY_RESULT", f"required path emptied: {path}"
    if _is_comment_only(stage_base_files, end_files, stage_diff):
        return "FAIL_COMMENT_ONLY", "only comments/whitespace changed"
    if required:
        if not touched:
            return (
                "FAIL_MISSING_TARGET",
                f"none of required paths changed: {list(required)}",
            )
        if len(touched) < len(required):
            left = [p for p in required if p not in touched]
            return "FAIL_PARTIAL_PATHS", f"required paths unchanged: {left}"
    return None


def _build_eval_tree(
    task: Task, end_manifest: dict[str, str], end_dir: Path, dest: Path
) -> None:
    write_files(dest, task.base_files)
    total = diff_manifests(manifest_of_files(task.base_files), end_manifest)
    for rel in total["added"] + total["modified"]:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((end_dir / rel).read_bytes())
    for rel in total["deleted"]:
        (dest / rel).unlink(missing_ok=True)
    for rel, content in task.base_files.items():  # frozen paths always canonical
        if any(fnmatch.fnmatchcase(rel, pat) for pat in task.frozen_paths):
            (dest / rel).write_text(content, encoding="utf-8")


def _reconcile_report(
    report: dict | None, changed: list[str], trace: list[dict] | None
) -> tuple[str | None, bool]:
    """Return (mismatch detail or None, trace_pending)."""
    if not isinstance(report, dict):
        return "report_missing", False
    for key in ("status", "changed_files", "commands_run"):
        if key not in report:
            return f"report_missing_field:{key}", False
    claimed = {str(p) for p in report["changed_files"]}
    if claimed != set(changed):
        return f"changed_files claimed={sorted(claimed)} actual={changed}", False
    if trace is None:
        return None, True
    events = [
        str(e.get("command", "")) for e in trace if e.get("kind") in ("command", "test")
    ]
    for cmd in report["commands_run"]:
        if not any(str(cmd) in event for event in events):
            return f"command not in trace: {cmd}", False
    return None, False


def _isolated_run(ctx: Ctx, check: Check) -> CheckRun:
    """Each probe gets its own copy of the eval tree (probes may write, e.g. a sentinel module)."""
    copy = ctx.scratch / f"check-{check.id}"
    shutil.copytree(ctx.eval_dir, copy)
    return run_check(copy, check.id, check.probe, check.timeout, check.dirs)


def _run_checks(ctx: Ctx, stage: Stage) -> list[tuple[Check, CheckOutcome]]:
    probe_checks = [c for c in stage.checks if c.probe is not None]
    native_checks = [c for c in stage.checks if c.native is not None]
    results: dict[str, CheckOutcome] = {}
    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
        futures = {c.id: pool.submit(_isolated_run, ctx, c) for c in probe_checks}
        for check in probe_checks:
            results[check.id] = outcome_from_run(check, futures[check.id].result())
    for check in native_checks:
        results[check.id] = check.native(ctx)
    return [(c, results[c.id]) for c in stage.checks]


def _aggregate(
    pairs: list[tuple[Check, CheckOutcome]],
) -> tuple[str | None, list[str], list[dict]]:
    public_ok = [o.passed for c, o in pairs if c.public]
    all_public_green = bool(public_ok) and all(v is True for v in public_ok)
    fails, pending, rows = [], [], []
    for check, out in pairs:
        rows.append(
            {
                "id": check.id,
                "kind": check.kind,
                "requirement": check.requirement,
                "passed": out.passed,
                "code": out.code,
                "detail": out.detail[:300],
                "metrics": out.metrics,
            }
        )
        if check.kind == codes.KIND_PROCESS:
            continue
        if out.passed is None:
            pending.append(check.id)
        elif out.passed is False:
            code = out.code or check.fail_code
            if check.kind == codes.KIND_INVARIANT and code == "FAIL_HIDDEN":
                code = "FAIL_REGRESSION"
            if code == "FAIL_HIDDEN" and all_public_green:
                code = "FAIL_HIDDEN_ONLY"
            fails.append(code)
    best = min(fails, key=codes.FAIL_PRIORITY.index) if fails else None
    return best, pending, rows


def evaluate_stage(
    task: Task,
    stage: Stage,
    stage_base_files: dict[str, str],
    sa: StageAttempt,
) -> tuple[StageResult, dict[str, str]]:
    """Evaluate one stage. Returns the result and the stage end manifest.

    stage_base_files is the pristine task for stage 0 and the previous stage end otherwise;
    the evaluator hashes the real start tree itself and requires it to equal that baseline's
    manifest (a workspace changed between stages is not a model result).
    """
    base_manifest = manifest_of_files(task.base_files)
    end_manifest = dir_manifest(sa.end_dir)
    expected_start = manifest_of_files(stage_base_files)
    if sa.start_dir is None or not Path(sa.start_dir).is_dir():
        res = StageResult(
            stage.id,
            codes.UNKNOWN,
            "UNKNOWN",
            detail="start workspace tree not provided",
        )
        return res, end_manifest
    actual_start = dir_manifest(
        Path(sa.start_dir)
    )  # hashed here, never taken from the caller
    start_hash = tree_hash(actual_start)
    if actual_start != expected_start:
        diff = diff_manifests(expected_start, actual_start)
        res = StageResult(
            stage.id,
            codes.INFRA,
            codes.INFRA_BASE_MISMATCH,
            start_hash_ok=False,
            start_tree_sha256=start_hash,
            detail=f"start tree differs from the sealed baseline: {changed_paths(diff)[:8]}",
        )
        return res, end_manifest
    end_files = read_files(sa.end_dir)
    gate = _structural_gate(
        task, stage, base_manifest, stage_base_files, end_manifest, end_files, sa.report
    )
    stage_diff = diff_manifests(manifest_of_files(stage_base_files), end_manifest)
    changed = changed_paths(stage_diff)
    if gate is not None:
        res = _fail(
            stage.id,
            gate[0],
            gate[1],
            changed=stage_diff,
            start_hash_ok=True,
            start_tree_sha256=start_hash,
        )
        _add_claim_flags(res, sa.report)
        return res, end_manifest
    for path, source in end_files.items():
        if path.endswith(".py") and (bad := violations(source)):
            res = _fail(
                stage.id,
                "FAIL_CHECKER_PATCH",
                f"{path}: {bad[0]}",
                changed=stage_diff,
                start_hash_ok=True,
                start_tree_sha256=start_hash,
            )
            _add_claim_flags(res, sa.report)
            return res, end_manifest
    with tempfile.TemporaryDirectory(prefix="llme-eval-") as tmp:
        eval_dir = Path(tmp) / "tree"
        eval_dir.mkdir()
        _build_eval_tree(task, end_manifest, sa.end_dir, eval_dir)
        ctx = Ctx(
            task=task,
            stage=stage,
            base_files=task.base_files,
            stage_base_files=stage_base_files,
            end_files=end_files,
            eval_dir=eval_dir,
            changed=stage_diff,
            report=sa.report,
            trace=sa.trace,
            scratch=Path(tmp),
        )
        pairs = _run_checks(ctx, stage)
    if not any(c.kind == codes.KIND_DELTA for c, _ in pairs):
        raise ConfigError(f"stage {stage.id} of {task.id} has no delta check")
    code, pending, rows = _aggregate(pairs)
    flags: list[str] = []
    if stage.report_required:
        mismatch, trace_pending = _reconcile_report(sa.report, changed, sa.trace)
        if mismatch:
            if code is None:
                flags.append("code_pass_report_mismatch")
                code = "FAIL_REPORT_MISMATCH"
            rows.append({"id": "REPORT", "passed": False, "detail": mismatch[:300]})
        if trace_pending:
            pending.append("trace_reconcile")
    if sa.trace is None:
        flags.append("trace_unavailable")
    res = StageResult(
        stage.id,
        codes.FAIL if code else codes.PASS,
        code,
        flags=flags,
        gates_pending=pending,
        checks=rows,
        changed=stage_diff,
        start_hash_ok=True,
        start_tree_sha256=start_hash,
    )
    _add_claim_flags(res, sa.report)
    return res, end_manifest


def _add_claim_flags(res: StageResult, report: dict | None) -> None:
    claimed_done = isinstance(report, dict) and report.get("status") == "done"
    if claimed_done and res.verdict == codes.FAIL:
        res.flags.append("false_done")


def evaluate_attempt(task: Task, attempt: Attempt) -> Evaluation:
    if attempt.infra is not None:
        proven = bool(attempt.infra.get("proven")) and attempt.infra.get("code")
        verdict = codes.INFRA if proven else codes.UNKNOWN
        flags = ["infra_not_evaluated"]
        if attempt.meta.get("wall_s") == 0:
            flags.append("zero_wall_no_response")
        return Evaluation(
            verdict,
            attempt.infra.get("code") if proven else "UNKNOWN",
            flags,
            [],
            [],
            evaluated=False,
            detail=str(attempt.infra)[:300],
        )
    if len(attempt.stages) != len(task.stages):
        raise ConfigError(f"{task.id}: expected {len(task.stages)} stage attempts")
    results: list[StageResult] = []
    prev_files = task.base_files
    for stage, sa in zip(task.stages, attempt.stages, strict=True):
        result, _ = evaluate_stage(task, stage, prev_files, sa)
        prev_files = read_files(sa.end_dir)
        results.append(result)
    pending = sorted({g for r in results for g in r.gates_pending})
    flags = sorted({f for r in results for f in r.flags})
    if task.session_check is not None:
        if attempt.session is None:
            pending.append("session")
            flags.append("session_unavailable")
        else:
            for check_id, out in task.session_check(attempt):
                if out.passed is None:
                    pending.append(check_id)
                elif not out.passed:
                    result = _fail(
                        "session",
                        out.code or "FAIL_HIDDEN",
                        f"{check_id}: {out.detail}",
                    )
                    results.append(result)
    verdict, code = codes.PASS, None
    for rank in (codes.INFRA, codes.UNKNOWN, codes.FAIL):
        hit = next((r for r in results if r.verdict == rank), None)
        if hit is not None:
            verdict, code = rank, hit.fail_code
            break
    return Evaluation(verdict, code, flags, sorted(set(pending)), results)
