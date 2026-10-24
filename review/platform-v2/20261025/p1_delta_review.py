"""Read-only source delta audit; writes only explicitly selected evidence files.

This is not the old scanner CLI/writer, a test runner or a runtime-binding proof.
Unchanged classifications are reused only when the recorded input hash matches.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
OLD = ROOT / "review/platform-v2/20261024/frontend-tests"
FRONT = ROOT.parent / "front"
FRONT_MAIN = ROOT.parent / "front-library-intent"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def delta(before: dict, after: dict) -> dict:
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
        "unchanged_count": sum(before[k] == after[k] for k in before.keys() & after.keys()),
    }


def front_manifest(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted((root / "src").rglob("*"))
            if p.is_file() and p.suffix in {".ts", ".tsx"}}


def back_manifest() -> dict:
    return {p.relative_to(ROOT).as_posix(): digest(p)
            for folder in ("tests", "unit_tests", "benchmark_tests")
            for p in sorted((ROOT / folder).rglob("*.py")) if p.is_file()}


def identity(root: Path) -> dict:
    return {"path": str(root), "sha": git(root, "rev-parse", "HEAD"),
            "status": git(root, "status", "--porcelain=v1").splitlines()}


def main() -> None:
    # Negative control: additions/removals/changes must not disappear as "unchanged".
    assert delta({"same": "1", "mutated": "1", "gone": "x"},
                 {"same": "1", "mutated": "2", "new": "y"}) == {
        "added": ["new"], "removed": ["gone"], "changed": ["mutated"], "unchanged_count": 1}
    summary = json.loads((OLD / "summary.json").read_text())
    previous = summary["input_provenance"]["source_sha256"]
    old_front = {k.removeprefix("front/"): v for k, v in previous.items() if k.startswith("front/")}
    old_back = {k.removeprefix("back/"): v for k, v in previous.items()
                if k.startswith(("back/tests/", "back/unit_tests/", "back/benchmark_tests/"))}
    before_identity = {label: identity(root) for label, root in
                       (("back", ROOT), ("shared_front", FRONT), ("front_main", FRONT_MAIN))}
    shared, front, back = front_manifest(FRONT), front_manifest(FRONT_MAIN), back_manifest()
    assert not before_identity["front_main"]["status"], "Selected main checkout must be clean"
    remote_main = git(FRONT_MAIN, "ls-remote", "origin", "refs/heads/main").split()[0]
    assert remote_main == before_identity["front_main"]["sha"], "Frontend is not remote main"

    spec = importlib.util.spec_from_file_location("v2_test_audit", ROOT / "scripts/platform_v2_test_audit.py")
    audit = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(audit)
    old_rows = {r["path"]: r for r in json.loads((OLD / "backend-test-classification.json").read_text())["test_files"]}
    rows, errors = [], []
    test_paths = sorted(p for p in back if Path(p).name.startswith("test_"))
    reused, parsed = [], []
    for rel in test_paths:
        if rel in old_rows and old_back.get(rel) == back[rel]:
            rows.append({**old_rows[rel], "evidence": "reused_identical_source_hash"})
            reused.append(rel)
        else:
            try:
                text = (ROOT / rel).read_text()
                row = audit.classify_backend_test(rel, ast.parse(text, filename=rel), text)
                rows.append({**row, "evidence": "current_delta_ast"})
                parsed.append(rel)
            except (OSError, SyntaxError, ValueError) as exc:
                errors.append({"path": rel, "error": str(exc)})

    front_delta = delta(old_front, front)
    changed_front = set(front_delta["added"] + front_delta["changed"])
    api_sources = {"src/api/client.ts", "src/api/stream.ts", "src/shell/useConnection.ts"}
    api_old = json.loads((OLD / "frontend-api-inventory.json").read_text())
    route_old = json.loads((OLD / "frontend-routes.json").read_text())
    # Parse only changed API source files with the existing TypeScript extractor.
    payload = [{"path": rel, "text": (FRONT_MAIN / rel).read_text(), "roles": ["api"]}
               for rel in sorted(changed_front & api_sources)]
    api_ast = audit._run_ts_ast(payload, front_root=FRONT_MAIN) if payload else None
    routes = audit.audit_frontend_routes(front_root=FRONT_MAIN) if "src/App.tsx" in changed_front else route_old
    calls = (api_ast or {}).get("api", {}).get("calls", [])
    old_call_ids = {(c["source_file"], c["function"], c["method"], c["endpoint"])
                    for c in api_old["endpoints"] if c["source_file"] in changed_front}
    new_call_ids = {(c["source_file"], c["function"], c["method"] or "GET", c["endpoint"]) for c in calls}
    old_routes = {r["path"] for r in route_old["routes"]}
    new_routes = {r["path"] for r in routes["routes"]}
    runtime_files = ["main.py", "generation_service.py", "background_consumers.py", "executor.py", "queued_work.py"]
    snapshot = json.loads((OUT / "p1-inventory-after-classification.json").read_text())
    runtime = {}
    for name in runtime_files:
        rel = "src/ttakkari/" + name
        path = ROOT / rel
        tree = ast.parse(path.read_text(), filename=rel)
        runtime[rel] = {"sha256": digest(path), "snapshot_sha256": snapshot["source_manifest"].get(rel),
                        "snapshot_matches": digest(path) == snapshot["source_manifest"].get(rel),
                        "definitions": [{"name": n.name, "line": n.lineno} for n in tree.body
                                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]}

    end_identity = {label: identity(root) for label, root in
                    (("back", ROOT), ("shared_front", FRONT), ("front_main", FRONT_MAIN))}
    assert before_identity == end_identity, "Git identity changed during scan"
    assert shared == front_manifest(FRONT) and front == front_manifest(FRONT_MAIN) and back == back_manifest(), "Input bytes changed during scan"
    assert not errors, errors
    assert len(rows) == len(test_paths)
    evidence = {
        "measured_at": datetime.now(UTC).isoformat(), "environment": "root Codex command sandbox",
        "source_revision": before_identity["back"]["sha"], "src_tree": git(ROOT, "rev-parse", "HEAD:src"),
        "identities": before_identity, "front_remote_main": remote_main,
        "baseline_summary_sha256": digest(OLD / "summary.json"),
        "scope": "Static delta; no runtime/import binding, test execution, DB or UI guarantees",
        "stable_during_scan": True, "delta_negative_control": "passed",
        "shared_front_delta": delta(old_front, shared), "front_main_delta": front_delta,
        "front_main_manifest": front, "shared_front_manifest": shared,
        "front_routes": {"evidence": "current_ast" if "src/App.tsx" in changed_front else "reused_identical_source_hash",
                         "added": sorted(new_routes - old_routes), "removed": sorted(old_routes - new_routes), "result": routes},
        "front_changed_api": {"parsed_sources": sorted(changed_front & api_sources),
                              "added_call_ids": sorted(new_call_ids - old_call_ids),
                              "removed_call_ids": sorted(old_call_ids - new_call_ids), "ast": api_ast,
                              "binding": "unknown; signature comparison only; no backend live resolution"},
        "back_delta": delta(old_back, back), "backend_source_manifest": back,
        "backend_tests": {"files": len(test_paths), "reused": reused, "parsed": parsed, "errors": errors,
                          "rows": rows, "historical_exclusion_now_included": sorted(p for p in test_paths if Path(p).name.startswith("test_platform_v2_")),
                          "non_test_python_inputs": sorted(set(back) - set(test_paths)),
                          "function_definitions": sum(r["test_count"] for r in rows),
                          "runtime_collected_cases": "unknown"},
        "runtime_source_bindings": runtime,
        "corrected_source_manifest_delta": {p: {"recorded": h, "current": digest(ROOT / p) if (ROOT / p).is_file() else None}
                                            for p, h in json.loads((OLD / "corrected-source-manifest.json").read_text()).items()
                                            if not (ROOT / p).is_file() or digest(ROOT / p) != h},
    }
    (OUT / "p1-frontend-runtime-tests-delta.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"shared_front_delta": evidence["shared_front_delta"],
                      "front_main_delta_counts": {k: len(v) if isinstance(v, list) else v for k, v in front_delta.items()},
                      "routes_added": evidence["front_routes"]["added"], "routes_removed": evidence["front_routes"]["removed"],
                      "api_added": len(new_call_ids - old_call_ids), "api_removed": len(old_call_ids - new_call_ids),
                      "backend_test_files": len(test_paths), "reused": len(reused), "parsed": len(parsed),
                      "errors": errors, "runtime_snapshot_matches": {k: v["snapshot_matches"] for k, v in runtime.items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
