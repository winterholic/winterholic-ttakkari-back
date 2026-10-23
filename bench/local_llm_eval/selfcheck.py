"""Self-check: delta/invariant classification, reference solutions and negative controls.

For one task this answers three questions with real subprocess runs (CPU only):
1. every delta check is red and every invariant check green at the stage start state; both green at gold
2. the reference solution(s) and the alternative reference pass the full evaluator
3. every negative control is rejected, and for the expected reason (false positives must be 0)
"""

import json
import sys
import tempfile
from pathlib import Path

from . import codes
from .evaluator import _run_checks, evaluate_attempt
from .manifest import diff_manifests, manifest_of_files, write_files
from .model import Ctx, Task
from .simulate import gold_ends, materialize, reports_for, stage_bases


def _eval_checks_on(
    task: Task,
    stage_index: int,
    files: dict[str, str],
    stage_base: dict[str, str],
    report: dict | None = None,
):
    stage = task.stages[stage_index]
    with tempfile.TemporaryDirectory(prefix="llme-sc-") as tmp:
        tree = Path(tmp) / "tree"
        write_files(tree, files)
        ctx = Ctx(
            task=task,
            stage=stage,
            base_files=task.base_files,
            stage_base_files=stage_base,
            end_files=files,
            eval_dir=tree,
            changed=diff_manifests(
                manifest_of_files(stage_base), manifest_of_files(files)
            ),
            report=report,
            trace=None,
            scratch=Path(tmp),
        )
        return {c.id: (c, o) for c, o in _run_checks(ctx, stage)}


def classification_rows(task: Task) -> list[dict]:
    ends = gold_ends(task)
    extra = task.gold_reports(task.base_files, ends) if task.gold_reports else None
    reports = reports_for(task, ends, extra)
    rows = []
    for index, (base, gold) in enumerate(
        zip(stage_bases(task, ends), ends, strict=True)
    ):
        at_start = _eval_checks_on(task, index, base, base)
        at_gold = _eval_checks_on(task, index, gold, base, reports[index])
        for check_id, (check, start_out) in at_start.items():
            gold_out = at_gold[check_id][1]
            row = {
                "stage": task.stages[index].id,
                "check": check_id,
                "kind": check.kind,
                "requirement": check.requirement,
                "green_at_start": start_out.passed,
                "green_at_gold": gold_out.passed,
            }
            if check.kind == codes.KIND_PROCESS or gold_out.passed is None:
                row["ok"] = True
                row["note"] = "pending/process (needs trace)"
            elif check.kind == codes.KIND_DELTA:
                row["ok"] = start_out.passed is False and gold_out.passed is True
            else:
                row["ok"] = start_out.passed is True and gold_out.passed is True
            if not row["ok"]:
                row["detail"] = (
                    f"start={start_out.detail[:120]} gold={gold_out.detail[:120]}"
                )
            rows.append(row)
    return rows


def _evaluate_ends(
    task: Task, ends, start_files=None, extra_reports=None, session=None
):
    reports = reports_for(task, ends, extra_reports)
    with materialize(
        task, ends, reports, start_files=start_files, session=session
    ) as attempt:
        return evaluate_attempt(task, attempt)


def reference_rows(task: Task) -> list[dict]:
    rows = []
    for name, fn in (("gold", task.gold), ("alt_gold", task.alt_gold)):
        if fn is None:
            continue
        ends = fn(task.base_files)
        extra = task.gold_reports(task.base_files, ends) if task.gold_reports else None
        ev = _evaluate_ends(task, ends, extra_reports=extra, session=task.gold_session)
        rows.append(
            {
                "reference": name,
                "verdict": ev.verdict,
                "fail_code": ev.fail_code,
                "ok": ev.verdict == codes.PASS,
                "gates_pending": ev.gates_pending,
                "detail": "" if ev.verdict == codes.PASS else _failure_text(ev),
            }
        )
    return rows


def _failure_text(ev) -> str:
    for stage in ev.stages:
        if stage.verdict != codes.PASS:
            bad = [c for c in stage.checks if c.get("passed") is False]
            return f"{stage.stage_id}: {stage.fail_code} {stage.detail} {bad[:3]}"
    return ""


def negative_rows(task: Task) -> list[dict]:
    gold = gold_ends(task)
    rows = []
    for neg in task.negatives:
        ends = neg.end(task.base_files, gold)
        start = neg.start(task.base_files, gold) if neg.start else None
        extra = neg.report(task.base_files, gold) if neg.report else None
        session = neg.session if neg.session is not None else task.gold_session
        ev = _evaluate_ends(
            task, ends, start_files=start, extra_reports=extra, session=session
        )
        ok = ev.verdict == neg.expect_verdict and (
            not neg.expect_codes or ev.fail_code in neg.expect_codes
        )
        rows.append(
            {
                "control": neg.id,
                "name": neg.name,
                "verdict": ev.verdict,
                "fail_code": ev.fail_code,
                "expected": list(neg.expect_codes),
                "false_positive": ev.verdict == codes.PASS,
                "ok": ok,
                "detail": "" if ok else _failure_text(ev),
            }
        )
    return rows


def selfcheck_task(task: Task) -> dict:
    classification = classification_rows(task)
    references = reference_rows(task)
    negatives = negative_rows(task)
    return {
        "task": task.id,
        "classification": classification,
        "references": references,
        "negatives": negatives,
        "summary": {
            "checks": len(classification),
            "classification_bad": sum(not r["ok"] for r in classification),
            "reference_fail": sum(not r["ok"] for r in references),
            "negatives": len(negatives),
            "negatives_not_rejected_for_expected_reason": sum(
                not r["ok"] for r in negatives
            ),
            "false_positives": sum(r["false_positive"] for r in negatives),
        },
    }


def main(argv: list[str]) -> int:
    from .tasks import REGISTRY

    json_out = None
    if "--json" in argv:
        index = argv.index("--json")
        json_out = Path(argv[index + 1])
        argv = argv[:index] + argv[index + 2 :]
    wanted = argv or sorted(REGISTRY)
    reports = []
    bad = 0
    for task_id in wanted:
        report = selfcheck_task(REGISTRY[task_id]())
        s = report["summary"]
        print(
            f"{task_id}: checks={s['checks']} classification_bad={s['classification_bad']} "
            f"reference_fail={s['reference_fail']} negatives={s['negatives']} "
            f"not_rejected_right={s['negatives_not_rejected_for_expected_reason']} "
            f"false_positives={s['false_positives']}"
        )
        for section in ("classification", "references", "negatives"):
            for row in report[section]:
                if not row["ok"]:
                    print(
                        f"  NOT OK {section}: {json.dumps(row, ensure_ascii=False)[:600]}"
                    )
        bad += (
            s["classification_bad"]
            + s["reference_fail"]
            + s["negatives_not_rejected_for_expected_reason"]
        )
        reports.append(report)
    if json_out is not None:
        json_out.write_text(
            json.dumps(
                {"tasks": reports, "problems": bad}, ensure_ascii=False, indent=1
            )
            + "\n",
            encoding="utf-8",
        )
    print(json.dumps({"tasks": len(reports), "problems": bad}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
