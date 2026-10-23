"""Holdout self-check. Reads the sealed bundle, so it is run by the operator and never by repository CI.

For each holdout task (CPU only, no model): every delta check is red and every invariant check green at the
stage start state, the reference and the alternative reference pass the full evaluator, every mutant and
negative control is rejected for its expected reason, the equivalent-mutant screen and the leak scan are
clean, and the candidate-visible export equals the pinned base tree.

The report carries counts, ids, codes and hashes only. Expected values, observed values, mutant
descriptions and gold content stay in the bundle; `--verbose` (author only) adds failure details.

    cd bench && python3 -m local_llm_eval.bundle_selfcheck [--json OUT] [--verbose] [TASK_ID ...]

Exit: 0 clean, 1 problems, 2 OS isolation unavailable (INFRA_ISOLATION_UNAVAILABLE, nothing was read),
3 bundle missing or commitment mismatch (INFRA_SEALED_*).
"""

import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

from . import bundle, codes, leakscan, sandbox
from .bundle_paths import COMMITMENTS, HOLDOUT_IDS, SEALED_ROOT
from .manifest import dir_manifest, manifest_of_files
from .selfcheck import classification_rows, negative_rows, reference_rows

REPO_MANIFEST = COMMITMENTS.with_name("benchmark-manifest-draft.json")


def _ref(section: str, row: dict, index: int) -> str:
    if section == "classification":
        return f"{row['stage']}/{row['check']}"
    if section == "references":
        return row["reference"]
    return f"{row['control']}#{index}"


def _problems(section: str, rows: list[dict], verbose: bool) -> list[dict]:
    out = []
    for index, row in enumerate(rows):
        if not row["ok"]:
            item = {"section": section, "ref": _ref(section, row, index)}
            if verbose:
                item["detail"] = json.dumps(row, ensure_ascii=False)[:600]
            out.append(item)
    return out


def _manifest_problems(task, pinned: dict, entry: dict | None) -> list[str]:
    problems = []
    checks = [c for s in task.stages for c in s.checks]
    requirements = {c.requirement for c in checks if c.requirement}
    if pinned.get("checks") != len(checks):
        problems.append("check count differs from the commitment")
    if pinned.get("requirements") != len(requirements):
        problems.append("requirement count differs from the commitment")
    if entry is None:
        return problems + ["task absent from the repository manifest"]
    extra = 2 if task.id == "B4-LNG-H" else 0
    if len(requirements) + extra != entry["n_requirements"]:
        problems.append("requirement count differs from the repository manifest")
    implemented = {n.id for n in task.negatives}
    if not set(entry["required_negative_controls"]) <= implemented:
        problems.append("a required negative control is not implemented")
    classes = [s.change_class for s in task.stages]
    expected = entry.get("change_class")
    if expected in (codes.CHANGE_DIFF, codes.CHANGE_READ_ONLY) and set(classes) != {
        expected
    }:
        problems.append("change class differs from the repository manifest")
    return problems


def _public_export_ok(task_id: str, task, root: Path, commitments_path: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="llme-public-") as tmp:
        prompts = bundle.export_public(task_id, Path(tmp), root, commitments_path)
        exported = dir_manifest(Path(tmp))
    stage_ids = [s.id for s in task.stages]
    return exported == manifest_of_files(task.base_files) and list(prompts) == stage_ids


def check_task(task, pinned: dict, entry: dict | None, root, commitments_path, verbose):
    classification = classification_rows(task)
    references = reference_rows(task)
    negatives = negative_rows(task)
    problems = (
        _problems("classification", classification, verbose)
        + _problems("references", references, verbose)
        + _problems("negatives", negatives, verbose)
    )
    kinds = Counter(r["kind"] for r in classification)
    delta_red = sum(
        1
        for r in classification
        if r["kind"] == codes.KIND_DELTA and r["green_at_start"] is False
    )
    invariant_green = sum(
        1
        for r in classification
        if r["kind"] == codes.KIND_INVARIANT and r["green_at_start"] is True
    )
    summary = {
        "checks": len(classification),
        "check_kinds": dict(kinds),
        "delta_red_at_base": f"{delta_red}/{kinds.get(codes.KIND_DELTA, 0)}",
        "invariant_green_at_base": f"{invariant_green}/{kinds.get(codes.KIND_INVARIANT, 0)}",
        "references": {r["reference"]: r["verdict"] for r in references},
        "negatives": {
            "total": len(negatives),
            "rejected_for_expected_reason": sum(r["ok"] for r in negatives),
            "false_positives": sum(r["false_positive"] for r in negatives),
            "by_control": dict(
                sorted(Counter(r["control"] for r in negatives).items())
            ),
        },
        "suite_mutants": task.meta.get("suite_mutants", 0),
    }
    screen = task.meta.get("mutant_screen")
    if screen is not None:
        result = screen()
        distinct = result["nonequivalent"]
        equal = result["equivalent_confirmed"]
        summary["mutant_screen"] = {
            "mutants_nonequivalent": f"{sum(distinct.values())}/{len(distinct)}",
            "equivalent_candidates_confirmed_equal": f"{sum(equal.values())}/{len(equal)}",
        }
        if not all(distinct.values()) or not all(equal.values()):
            problems.append({"section": "mutant_screen", "ref": "screen"})
    log_check = task.meta.get("log_reproducible")
    if log_check is not None:
        summary["failure_log_reproducible"] = bool(log_check())
        if not summary["failure_log_reproducible"]:
            problems.append({"section": "failure_log", "ref": "regenerate"})
    scan = leakscan.scan(task, task.meta.get("leak_allow"))
    summary["leak_scan"] = {
        "findings": len(scan.findings),
        "allowed": len(scan.allowed),
        "spec_stated": scan.spec_stated,
    }
    if scan.findings:
        item = {"section": "leak_scan", "ref": f"{len(scan.findings)} finding(s)"}
        if verbose:
            item["detail"] = json.dumps(scan.findings, ensure_ascii=False)[:600]
        problems.append(item)
    summary["public_export_equals_base"] = _public_export_ok(
        task.id, task, root, commitments_path
    )
    if not summary["public_export_equals_base"]:
        problems.append({"section": "public_export", "ref": "tree"})
    for text in _manifest_problems(task, pinned, entry):
        problems.append({"section": "manifest", "ref": text})
    summary["problems"] = len(problems)
    return summary, problems


def run(
    task_ids,
    verbose=False,
    root: Path = SEALED_ROOT,
    commitments_path: Path = COMMITMENTS,
) -> tuple[int, dict]:
    isolation = sandbox.availability()
    if not isolation.available:
        return 2, {
            "code": "INFRA_ISOLATION_UNAVAILABLE",
            "reason": isolation.reason,
            "read_nothing": True,
        }
    try:
        data, commitments = bundle.verified_bundle(root, commitments_path)
    except bundle.BundleError as exc:
        return 3, {"code": exc.code, "detail": exc.detail}
    manifest = json.loads(REPO_MANIFEST.read_text(encoding="utf-8"))
    entries = {t["id"]: t for t in manifest["tasks"]}
    report = {
        "bundle_sha256": commitments["bundle_sha256"],
        "files": len(data),
        "isolation": {"available": True, "reason": isolation.reason},
        "tasks": {},
        "problems": [],
    }
    for task_id in task_ids:
        try:
            task = bundle.task_from_verified(task_id, data, commitments)
        except bundle.BundleError as exc:
            return 3, {"code": exc.code, "detail": exc.detail}
        summary, problems = check_task(
            task,
            commitments["tasks"][task_id],
            entries.get(task_id),
            root,
            commitments_path,
            verbose,
        )
        report["tasks"][task_id] = summary
        report["problems"] += [{"task": task_id, **p} for p in problems]
    return (1 if report["problems"] else 0), report


def main(argv: list[str]) -> int:
    verbose = "--verbose" in argv
    argv = [a for a in argv if a != "--verbose"]
    json_out = None
    if "--json" in argv:
        index = argv.index("--json")
        json_out = Path(argv[index + 1])
        argv = argv[:index] + argv[index + 2 :]
    task_ids = argv or list(HOLDOUT_IDS)
    code, report = run(task_ids, verbose)
    if json_out is not None:
        json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    if "tasks" in report:
        for task_id, summary in report["tasks"].items():
            negatives = summary["negatives"]
            print(
                f"{task_id}: checks={summary['checks']} delta_red={summary['delta_red_at_base']} "
                f"invariant_green={summary['invariant_green_at_base']} "
                f"references={summary['references']} negatives={negatives['rejected_for_expected_reason']}/"
                f"{negatives['total']} false_positives={negatives['false_positives']} "
                f"leak_findings={summary['leak_scan']['findings']} problems={summary['problems']}"
            )
        print(
            json.dumps(
                {
                    "tasks": len(report["tasks"]),
                    "problems": len(report["problems"]),
                    "bundle_sha256": report["bundle_sha256"],
                    "files": report["files"],
                }
            )
        )
        for item in report["problems"]:
            print("  PROBLEM", json.dumps(item, ensure_ascii=False))
    else:
        print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
