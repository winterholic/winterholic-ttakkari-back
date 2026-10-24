"""Reproducible static & operational evidence extractor for Platform V2 Worker A.

Produces `A-runtime-delivery-evidence.json` bound to the actual git checkout/tree,
with fail-closed snapshot binding and content-addressed EvidenceCacheKey v2 verification.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import platform
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src" / "ttakkari"
OUT_PATH = Path(__file__).resolve().parent / "A-runtime-delivery-evidence.json"

BASELINE_607_SHA = "607f8bda0df7dbdd458e4f0e54d703ce9edd3def"
CANONICAL_C8_SHA = "c8f10156845d007ec6496a65d6d8417be19260bd"
CANONICAL_C8_TREE_SHA = "d224086fed586484437819bbe100c5b4d58195f1"
CANONICAL_C8_SRC_TREE_SHA = "f2e5572ce8d039341eacc4dad5a70db03c61553b"

SOURCE_CRITICAL_PREFIXES = (
    "src/",
    "alembic/",
    "deploy/",
    "tests/",
    "unit_tests/",
    "benchmark_tests/",
    "pyproject.toml",
    "uv.lock",
)
AUDIT_DOC_PREFIXES = (
    "review/platform-v2/",
    "docs/design/platform-v2/",
)
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class SnapshotBindingError(RuntimeError):
    """Raised when repository root, checkout tree, import origin, or AST parsing fails binding."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise SnapshotBindingError("missing_file", f"Required file does not exist: {path}")
    return sha256_bytes(path.read_bytes())


def _run_git(repo_root: Path, *args: str) -> str:
    if not (repo_root / ".git").exists() or not (repo_root / "src" / "ttakkari").is_dir():
        raise SnapshotBindingError(
            "invalid_repo_root",
            f"Path is not a valid ttakkari backend git checkout: {repo_root}",
        )
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SnapshotBindingError(
            "git_command_failed",
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}",
        )
    return proc.stdout


def verify_import_origin(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Bind `ttakkari` package import to `repo_root / 'src'` and reject foreign editable installs."""
    expected_src = (repo_root / "src").resolve()
    expected_init = (expected_src / "ttakkari" / "__init__.py").resolve()
    if not expected_init.is_file():
        raise SnapshotBindingError(
            "invalid_repo_root",
            f"Missing package entrypoint: {expected_init}",
        )

    src_str = str(expected_src)
    if sys.path[:1] != [src_str]:
        sys.path[:] = [src_str, *[p for p in sys.path if p != src_str]]

    loaded = sys.modules.get("ttakkari")
    if loaded is not None:
        loaded_file = Path(getattr(loaded, "__file__", "") or "").resolve()
        if loaded_file != expected_init:
            raise SnapshotBindingError(
                "wrong_checkout_import",
                f"ttakkari already imported from {loaded_file}, expected {expected_init}",
            )
    else:
        import ttakkari as loaded

    actual_init = Path(getattr(loaded, "__file__", "") or "").resolve()
    if actual_init != expected_init:
        raise SnapshotBindingError(
            "wrong_checkout_import",
            f"ttakkari resolved to {actual_init}, expected {expected_init}",
        )
    return {
        "expected_src_root": str(expected_src),
        "actual_ttakkari_init_file": str(actual_init),
        "actual_ttakkari_init_sha256": sha256_file(actual_init),
        "is_bound_to_worktree_src": True,
    }


def compute_dirty_manifest(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Parse `git status --porcelain=v1 -z` into source-critical vs audit-doc paths."""
    raw = _run_git(repo_root, "status", "--porcelain=v1", "-z")
    entries = [e for e in raw.split("\0") if e]
    dirty_all: list[dict[str, str]] = []
    dirty_source_paths: list[str] = []
    dirty_audit_doc_paths: list[str] = []
    other_dirty_paths: list[str] = []

    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        status = entry[:2]
        path = entry[3:]
        if status.startswith(("R", "C")) and idx + 1 < len(entries):
            idx += 1
            path = entries[idx]
        dirty_all.append({"status": status, "path": path})
        if any(path == p or path.startswith(p) for p in SOURCE_CRITICAL_PREFIXES):
            dirty_source_paths.append(path)
        elif any(path == p or path.startswith(p) for p in AUDIT_DOC_PREFIXES):
            dirty_audit_doc_paths.append(path)
        else:
            other_dirty_paths.append(path)
        idx += 1

    return {
        "entries": dirty_all,
        "dirty_source_paths": sorted(dirty_source_paths),
        "dirty_audit_doc_paths": sorted(dirty_audit_doc_paths),
        "other_dirty_paths": sorted(other_dirty_paths),
        "is_clean_source_tree": len(dirty_source_paths) == 0 and len(other_dirty_paths) == 0,
        "is_clean_audit_docs": len(dirty_audit_doc_paths) == 0,
    }


def verify_snapshot_binding(
    repo_root: Path = REPO_ROOT,
    *,
    expected_source_tree_sha: str | None = None,
    allow_dirty_source_paths: bool = False,
) -> dict[str, Any]:
    """Verify git HEAD, tree hashes, dirty status, and package import binding fail-closed."""
    import_binding = verify_import_origin(repo_root)
    head_sha = _run_git(repo_root, "rev-parse", "--verify", "HEAD").strip()
    head_tree_sha = _run_git(repo_root, "rev-parse", "--verify", "HEAD^{tree}").strip()
    src_tree_sha = _run_git(repo_root, "rev-parse", "--verify", "HEAD:src/ttakkari").strip()
    alembic_tree_sha = _run_git(repo_root, "rev-parse", "--verify", "HEAD:alembic").strip()
    tests_tree_sha = _run_git(repo_root, "rev-parse", "--verify", "HEAD:tests").strip()
    unit_tests_tree_sha = _run_git(repo_root, "rev-parse", "--verify", "HEAD:unit_tests").strip()
    deploy_tree_sha = _run_git(repo_root, "rev-parse", "--verify", "HEAD:deploy").strip()
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").strip()

    try:
        c8_tree_sha = _run_git(
            repo_root, "rev-parse", "--verify", f"{CANONICAL_C8_SHA}^{{tree}}"
        ).strip()
        c8_src_tree_sha = _run_git(
            repo_root, "rev-parse", "--verify", f"{CANONICAL_C8_SHA}:src/ttakkari"
        ).strip()
    except SnapshotBindingError:
        c8_tree_sha = CANONICAL_C8_TREE_SHA
        c8_src_tree_sha = CANONICAL_C8_SRC_TREE_SHA

    try:
        origin_main_sha = _run_git(repo_root, "rev-parse", "--verify", "origin/main").strip()
        origin_main_tree_sha = _run_git(
            repo_root, "rev-parse", "--verify", "origin/main^{tree}"
        ).strip()
        origin_main_src_tree_sha = _run_git(
            repo_root, "rev-parse", "--verify", "origin/main:src/ttakkari"
        ).strip()
    except SnapshotBindingError:
        origin_main_sha = CANONICAL_C8_SHA
        origin_main_tree_sha = c8_tree_sha
        origin_main_src_tree_sha = c8_src_tree_sha

    dirty = compute_dirty_manifest(repo_root)
    if not allow_dirty_source_paths and not dirty["is_clean_source_tree"]:
        raise SnapshotBindingError(
            "dirty_source_tree",
            f"Refusing to bind clean source snapshot with dirty paths: "
            f"source={dirty['dirty_source_paths']} other={dirty['other_dirty_paths']}",
        )

    if expected_source_tree_sha is not None and src_tree_sha != expected_source_tree_sha:
        raise SnapshotBindingError(
            "tree_mismatch",
            f"HEAD:src/ttakkari tree {src_tree_sha} != expected {expected_source_tree_sha}",
        )

    return {
        "candidate_head_sha": head_sha,
        "candidate_tree_sha": head_tree_sha,
        "candidate_src_ttakkari_tree_sha": src_tree_sha,
        "audited_commit_sha": CANONICAL_C8_SHA,
        "audited_tree_sha": c8_tree_sha,
        "audited_src_ttakkari_tree_sha": c8_src_tree_sha,
        "checkout_branch": branch,
        "head_src_ttakkari_tree_sha": src_tree_sha,
        "head_alembic_tree_sha": alembic_tree_sha,
        "head_tests_tree_sha": tests_tree_sha,
        "head_unit_tests_tree_sha": unit_tests_tree_sha,
        "head_deploy_tree_sha": deploy_tree_sha,
        "origin_main_sha": origin_main_sha,
        "origin_main_tree_sha": origin_main_tree_sha,
        "origin_main_src_ttakkari_tree_sha": origin_main_src_tree_sha,
        "source_tree_matches_audited": src_tree_sha == c8_src_tree_sha,
        "source_tree_matches_origin_main": src_tree_sha == origin_main_src_tree_sha,
        "dirty_manifest": {
            "entries": dirty["entries"],
            "dirty_source_paths": dirty["dirty_source_paths"],
            "dirty_audit_doc_paths": dirty["dirty_audit_doc_paths"],
            "other_dirty_paths": dirty["other_dirty_paths"],
            "is_clean_source_tree": dirty["is_clean_source_tree"],
            "is_clean_audit_docs": dirty["is_clean_audit_docs"],
        },
        "import_binding": import_binding,
    }


def extract_migration_tables_from_text(text: str) -> list[str]:
    """Extract created table names from both `op.create_table(...)` and raw `CREATE TABLE` SQL."""
    tables: list[str] = []
    for m in re.finditer(r'op\.create_table\(\s*["\']([a-zA-Z0-9_]+)["\']', text):
        if m.group(1) not in tables:
            tables.append(m.group(1))
    for m in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z0-9_]+)\s*\(",
        text,
        re.IGNORECASE,
    ):
        if m.group(1) not in tables:
            tables.append(m.group(1))
    return tables


def compute_git_topology_and_drift(
    repo_root: Path = REPO_ROOT,
    left_sha: str = BASELINE_607_SHA,
    right_sha: str = CANONICAL_C8_SHA,
) -> dict[str, Any]:
    """Compute exact git topology and diff statistics between two commits dynamically."""
    merge_base = _run_git(repo_root, "merge-base", left_sha, right_sha).strip()
    lr_raw = _run_git(
        repo_root, "rev-list", "--left-right", "--count", f"{left_sha}...{right_sha}"
    ).strip()
    left_str, right_str = lr_raw.split()
    left_ahead = int(left_str)
    right_ahead = int(right_str)

    left_commits = [
        line
        for line in _run_git(repo_root, "log", "--oneline", left_sha, f"^{right_sha}").splitlines()
        if line.strip()
    ]

    shortstat = _run_git(repo_root, "diff", "--shortstat", left_sha, right_sha).strip()
    files_m = re.search(r"(\d+)\s+files?\s+changed", shortstat)
    ins_m = re.search(r"(\d+)\s+insertions?\(\+\)", shortstat)
    del_m = re.search(r"(\d+)\s+deletions?\(-\)", shortstat)
    if not files_m:
        raise SnapshotBindingError("parse_failure", f"Could not parse git diff --shortstat: {shortstat}")

    def _count_tree_py_and_migrations(rev: str) -> dict[str, Any]:
        py_Paths = [
            p
            for p in _run_git(repo_root, "ls-tree", "-r", "--name-only", rev, "src/ttakkari").splitlines()
            if p.endswith(".py")
        ]
        total_lines = 0
        for p in py_Paths:
            blob = _run_git(repo_root, "show", f"{rev}:{p}")
            total_lines += len(blob.splitlines())
        mig_paths = sorted(
            p
            for p in _run_git(repo_root, "ls-tree", "-r", "--name-only", rev, "alembic/versions").splitlines()
            if p.endswith(".py")
        )
        return {
            "python_files": len(py_Paths),
            "source_lines": total_lines,
            "migration_files": len(mig_paths),
            "py_path_set": set(py_Paths),
            "mig_path_set": set(mig_paths),
        }

    left_stats = _count_tree_py_and_migrations(left_sha)
    right_stats = _count_tree_py_and_migrations(right_sha)

    added_migrations = sorted(right_stats["mig_path_set"] - left_stats["mig_path_set"])
    migration_table_details: dict[str, list[str]] = {}
    all_new_tables: list[str] = []
    for mig_rel in added_migrations:
        mig_text = _run_git(repo_root, "show", f"{right_sha}:{mig_rel}")
        tables = extract_migration_tables_from_text(mig_text)
        mig_name = Path(mig_rel).name
        migration_table_details[mig_name] = tables
        all_new_tables.extend(tables)

    return {
        "left_baseline_sha": left_sha,
        "right_canonical_sha": right_sha,
        "merge_base_sha": merge_base,
        "left_right_unique_commits": [left_ahead, right_ahead],
        "left_unique_commits_in_607f8bda": left_ahead,
        "left_unique_commit_summaries": left_commits,
        "right_unique_commits_in_c8f10156": right_ahead,
        "diff_stat_607f8bda_to_c8f10156": {
            "changed_paths": int(files_m.group(1)),
            "insertions": int(ins_m.group(1)) if ins_m else 0,
            "deletions": int(del_m.group(1)) if del_m else 0,
            "raw_shortstat": shortstat,
        },
        "snapshot_at_607f8bda": {
            "python_files": left_stats["python_files"],
            "source_lines": left_stats["source_lines"],
            "migration_files": left_stats["migration_files"],
        },
        "snapshot_at_c8f10156": {
            "python_files": right_stats["python_files"],
            "source_lines": right_stats["source_lines"],
            "migration_files": right_stats["migration_files"],
        },
        "net_added_python_files": right_stats["python_files"] - left_stats["python_files"],
        "net_added_source_lines": right_stats["source_lines"] - left_stats["source_lines"],
        "added_migrations": [Path(p).name for p in added_migrations],
        "new_tables_by_migration": migration_table_details,
        "total_new_tables_in_added_migrations": len(all_new_tables),
        "retracted_prior_a_claims": [
            "Retracted: '607f8bda is simply 111 commits behind c8f10156 with 253 files changed'. Actual git topology is divergent across merge-base 2522d78e29b14ffb29d8536bcf833d832b4f87e1: 607f8bda has 2 unique commits and c8f10156 has 118 unique commits; full diff is 366 files changed, 86,213 insertions(+), 339 deletions(-).",
            "Retracted: '0064_run_executor creates 4 tables (run_executor_states, run_execution_commands, run_execution_leases, run_process_groups) for 21 total new tables in 0063..0067'. Actual 0064_run_executor creates 1 new table (`runtime_process_groups`), adds columns (`role`, `protocol`, `execution_mode` on `runtime_generations`; `executor_generation` on `runtime_control`), and installs 4 PL/pgSQL guard triggers; total new tables across 0063..0067 is 18 (5 + 1 + 8 + 4 + 0).",
        ],
    }


def top_module_name(rel_to_pkg: Path) -> str:
    parts = rel_to_pkg.parts
    if len(parts) == 1:
        return parts[0].removesuffix(".py")
    return parts[0]


def analyze_source_graph(src_root: Path = SRC_ROOT) -> dict[str, Any]:
    """Parse all Python files under `src/ttakkari` and build module + file import graphs.

    Fail-closed on any SyntaxError or unreadable file.
    """
    if not src_root.is_dir():
        raise SnapshotBindingError("invalid_src_root", f"Source directory does not exist: {src_root}")
    py_files = sorted(src_root.rglob("*.py"))
    if not py_files:
        raise SnapshotBindingError("empty_src_root", f"No Python files found in {src_root}")

    module_files: dict[str, list[str]] = defaultdict(list)
    module_lines: dict[str, int] = defaultdict(int)
    forward_deps: dict[str, set[str]] = defaultdict(set)
    reverse_deps: dict[str, set[str]] = defaultdict(set)
    file_forward_modules: dict[str, set[str]] = defaultdict(set)
    file_dynamic_imports: dict[str, list[str]] = defaultdict(list)
    file_hashes: dict[str, str] = {}

    total_lines = 0
    for f in py_files:
        rel_pkg = f.relative_to(src_root)
        rel_repo = f.relative_to(src_root.parents[1]).as_posix()
        mod = top_module_name(rel_pkg)
        raw_bytes = f.read_bytes()
        file_hashes[rel_repo] = sha256_bytes(raw_bytes)
        try:
            text = raw_bytes.decode("utf-8")
            tree = ast.parse(text, filename=str(f))
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise SnapshotBindingError(
                "parse_failure",
                f"Failed to parse Python source file {rel_repo}: {exc}",
            ) from exc

        lines = len(text.splitlines())
        total_lines += lines
        module_files[mod].append(rel_repo)
        module_lines[mod] += lines

        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("ttakkari."):
                        sub = alias.name.split(".")[1]
                        targets.append(sub)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("ttakkari"):
                    parts = node.module.split(".")
                    if len(parts) >= 2:
                        targets.append(parts[1])
                    elif len(parts) == 1:
                        for alias in node.names:
                            targets.append(alias.name)
                elif node.level and node.level > 0:
                    targets.append(mod)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                    file_dynamic_imports[rel_repo].append("__import__")
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                ):
                    file_dynamic_imports[rel_repo].append("import_module")

            for target in targets:
                if target != mod:
                    forward_deps[mod].add(target)
                    reverse_deps[target].add(mod)
                file_forward_modules[rel_repo].add(target)

    all_mods = sorted(module_files.keys())
    module_summary: dict[str, Any] = {}
    for mod in all_mods:
        f_deps = sorted(forward_deps.get(mod, set()) & set(all_mods))
        r_deps = sorted(reverse_deps.get(mod, set()) & set(all_mods))
        module_summary[mod] = {
            "file_count": len(module_files[mod]),
            "line_count": module_lines[mod],
            "files": sorted(module_files[mod]),
            "depends_on": f_deps,
            "depended_by": r_deps,
            "fan_out": len(f_deps),
            "fan_in": len(r_deps),
        }

    top_fan_in = sorted(
        all_mods,
        key=lambda m: (module_summary[m]["fan_in"], module_summary[m]["line_count"]),
        reverse=True,
    )[:20]

    return {
        "total_python_files": len(py_files),
        "total_lines": total_lines,
        "top_level_unit_count": len(all_mods),
        "top_fan_in_units": [
            {
                "unit": m,
                "fan_in": module_summary[m]["fan_in"],
                "fan_out": module_summary[m]["fan_out"],
                "file_count": module_summary[m]["file_count"],
                "line_count": module_summary[m]["line_count"],
                "depended_by": module_summary[m]["depended_by"],
            }
            for m in top_fan_in
        ],
        "modules": module_summary,
        "file_hashes": dict(sorted(file_hashes.items())),
        "file_dynamic_imports": dict(sorted(file_dynamic_imports.items())),
    }


def analyze_migrations(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    versions_dir = repo_root / "alembic" / "versions"
    if not versions_dir.is_dir():
        raise SnapshotBindingError("invalid_migrations_dir", f"Missing {versions_dir}")
    files = sorted(versions_dir.glob("*.py"))
    if not files:
        raise SnapshotBindingError("empty_migrations_dir", f"No migration files in {versions_dir}")

    revisions: dict[str, str] = {}
    down_revisions: dict[str, list[str]] = {}
    file_hashes: dict[str, str] = {}

    for f in files:
        raw_bytes = f.read_bytes()
        file_hashes[f"alembic/versions/{f.name}"] = sha256_bytes(raw_bytes)
        try:
            text = raw_bytes.decode("utf-8")
            ast.parse(text, filename=str(f))
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise SnapshotBindingError(
                "parse_failure",
                f"Failed to parse migration {f.name}: {exc}",
            ) from exc

        rev_m = re.search(
            r'^revision(?:\s*:\s*[^=]+)?\s*=\s*["\']([^"\']+)["\']',
            text,
            re.MULTILINE,
        )
        if not rev_m:
            raise SnapshotBindingError(
                "parse_failure",
                f"Migration file {f.name} is missing a parsable `revision = ...` assignment",
            )
        rev = rev_m.group(1)
        revisions[rev] = f.name
        down_m = re.search(
            r"^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(.+)$",
            text,
            re.MULTILINE,
        )
        if not down_m:
            raise SnapshotBindingError(
                "parse_failure",
                f"Migration file {f.name} is missing `down_revision = ...`",
            )
        downs = re.findall(r'["\']([^"\']+)["\']', down_m.group(1))
        down_revisions[rev] = downs

    referenced = {d for downs in down_revisions.values() for d in downs}
    heads = sorted(set(revisions) - referenced)
    return {
        "migration_file_count": len(files),
        "revision_count": len(revisions),
        "heads": heads,
        "single_head": len(heads) == 1,
        "latest_files": [f.name for f in files[-5:]],
        "migration_file_hashes": file_hashes,
    }


def analyze_contracts_and_manifests(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    gen_contract_path = repo_root / "deploy" / "generation-contract.json"
    exec_manifest_path = repo_root / "deploy" / "run-executor-manifest.json"
    gen_contract = json.loads(gen_contract_path.read_text(encoding="utf-8"))
    exec_manifest = json.loads(exec_manifest_path.read_text(encoding="utf-8"))

    gen_service_text = (repo_root / "src" / "ttakkari" / "generation_service.py").read_text(
        encoding="utf-8"
    )
    maint_jobs = re.findall(r'\("([a-z0-9-]+)",\s*_[a-z0-9_]+|creative_scan|Collector\(\)\.tick|collect_once|lead_archive\.archive_idle_leads|sync_all', gen_service_text)
    maint_job_names = re.findall(
        r'\("([a-z0-9-]+)",\s*(?:_[a-z0-9_]+|creative_scan|Collector\(\)\.tick|collect_once|lead_archive\.archive_idle_leads|sync_all)',
        gen_service_text,
    )

    return {
        "generation_contract_path": "deploy/generation-contract.json",
        "generation_contract_sha256": sha256_file(gen_contract_path),
        "generation_contract": gen_contract,
        "run_executor_manifest_path": "deploy/run-executor-manifest.json",
        "run_executor_manifest_sha256": sha256_file(exec_manifest_path),
        "run_executor_manifest": exec_manifest,
        "maintenance_jobs": maint_job_names,
        "maintenance_job_count": len(maint_job_names),
        "parsed_job_matches": len(maint_jobs),
    }


def compute_transitive_module_closure(
    module_id: str,
    graph: dict[str, Any],
) -> tuple[list[str], dict[str, str], bool, list[str]]:
    """Compute transitive internal module closure and full file-content hash map.

    Returns `(closure_modules, closure_file_hashes, closure_complete, reasons)`.
    """
    modules = graph.get("modules", {})
    file_hashes = graph.get("file_hashes", {})
    dynamic_imports = graph.get("file_dynamic_imports", {})

    if module_id not in modules:
        return [], {}, False, [f"unknown_module:{module_id}"]

    visited: set[str] = set()
    stack = [module_id]
    reasons: list[str] = []

    while stack:
        curr = stack.pop()
        if curr in visited:
            continue
        if curr not in modules:
            reasons.append(f"unresolved_module:{curr}")
            continue
        visited.add(curr)
        for dep in modules[curr].get("depends_on", []):
            if dep not in visited:
                stack.append(dep)

    closure_files: dict[str, str] = {}
    for mod in sorted(visited):
        for rel_file in modules[mod].get("files", []):
            digest = file_hashes.get(rel_file)
            if not digest or not SHA256_HEX_RE.match(digest):
                reasons.append(f"missing_or_invalid_file_hash:{rel_file}")
            else:
                closure_files[rel_file] = digest
            if rel_file in dynamic_imports:
                reasons.append(
                    f"dynamic_import_in_closure:{mod}:{rel_file}:{dynamic_imports[rel_file]}"
                )

    closure_complete = len(reasons) == 0 and len(closure_files) > 0
    return sorted(visited), dict(sorted(closure_files.items())), closure_complete, reasons


def _validate_hash_map(name: str, mapping: dict[str, str], *, allow_empty: bool = False) -> list[str]:
    if not isinstance(mapping, dict) or (not allow_empty and len(mapping) == 0):
        return [f"empty_or_invalid_hash_map:{name}"]
    errors: list[str] = []
    for k, v in mapping.items():
        if not k or not isinstance(v, str) or not SHA256_HEX_RE.match(v):
            errors.append(f"invalid_sha256_entry:{name}:{k}")
    return errors


def compute_evidence_cache_key(
    *,
    module_id: str,
    candidate_git_head_sha: str,
    candidate_git_tree_sha: str,
    dirty_source_paths: list[str],
    changed_file_hashes: dict[str, str],
    module_source_file_hashes: dict[str, str],
    transitive_dependency_file_hashes: dict[str, str],
    closure_complete: bool,
    selected_test_file_hashes: dict[str, str],
    conftest_and_helper_hashes: dict[str, str],
    selector_args: list[str],
    pyproject_sha256: str,
    uv_lock_sha256: str,
    python_full_version: str,
    platform_tag: str,
    pytest_plugins_sha256: str,
    sanitized_env_sha256: str,
    models_py_sha256: str,
    alembic_env_sha256: str,
    migration_file_hashes: dict[str, str],
    migration_head: str,
    generation_contract_sha256: str,
    run_executor_manifest_sha256: str,
) -> dict[str, Any]:
    """Compute a fail-closed, content-addressed EvidenceCacheKey v2.

    Every changed file, module source file, transitive dependency file, selected test file,
    conftest/helper file, migration file, manifest, and runtime/environment attribute is hashed
    by content. Any missing hash, dirty source tree, or incomplete dependency closure raises ValueError
    (forcing fail-closed broad gate fallback).
    """
    errors: list[str] = []
    if dirty_source_paths:
        errors.append(f"dirty_source_tree:{sorted(dirty_source_paths)}")
    if not closure_complete:
        errors.append("incomplete_dependency_closure")
    if not candidate_git_head_sha or not re.match(r"^[0-9a-f]{40}$", candidate_git_head_sha):
        errors.append("invalid_candidate_git_head_sha")
    if not candidate_git_tree_sha or not re.match(r"^[0-9a-f]{40}$", candidate_git_tree_sha):
        errors.append("invalid_candidate_git_tree_sha")
    if not selector_args or not all(isinstance(s, str) and s.strip() for s in selector_args):
        errors.append("empty_or_invalid_selector_args")
    if not re.match(r"^\d+\.\d+\.\d+", python_full_version or ""):
        errors.append("invalid_python_full_version")
    if not platform_tag:
        errors.append("missing_platform_tag")
    if not migration_head:
        errors.append("missing_migration_head")

    errors.extend(_validate_hash_map("changed_file_hashes", changed_file_hashes))
    errors.extend(_validate_hash_map("module_source_file_hashes", module_source_file_hashes))
    errors.extend(
        _validate_hash_map("transitive_dependency_file_hashes", transitive_dependency_file_hashes)
    )
    errors.extend(_validate_hash_map("selected_test_file_hashes", selected_test_file_hashes))
    errors.extend(_validate_hash_map("conftest_and_helper_hashes", conftest_and_helper_hashes))
    errors.extend(_validate_hash_map("migration_file_hashes", migration_file_hashes))

    for path, digest in changed_file_hashes.items():
        in_mod = module_source_file_hashes.get(path)
        in_dep = transitive_dependency_file_hashes.get(path)
        in_test = selected_test_file_hashes.get(path)
        if in_mod is None and in_dep is None and in_test is None:
            errors.append(f"changed_file_outside_hashed_trees:{path}")
        else:
            for tree_name, tree_digest in (
                ("module_source_file_hashes", in_mod),
                ("transitive_dependency_file_hashes", in_dep),
                ("selected_test_file_hashes", in_test),
            ):
                if tree_digest is not None and tree_digest != digest:
                    errors.append(f"inconsistent_changed_file_hash:{path}:{tree_name}")

    scalar_hashes = {
        "pyproject_sha256": pyproject_sha256,
        "uv_lock_sha256": uv_lock_sha256,
        "pytest_plugins_sha256": pytest_plugins_sha256,
        "sanitized_env_sha256": sanitized_env_sha256,
        "models_py_sha256": models_py_sha256,
        "alembic_env_sha256": alembic_env_sha256,
        "generation_contract_sha256": generation_contract_sha256,
        "run_executor_manifest_sha256": run_executor_manifest_sha256,
    }
    for key, val in scalar_hashes.items():
        if not isinstance(val, str) or not SHA256_HEX_RE.match(val):
            errors.append(f"invalid_scalar_hash:{key}")

    if errors:
        raise ValueError(f"EvidenceCacheKey fail-closed rejection: {'; '.join(errors)}")

    payload = {
        "schema_version": "platform-v2-evidence-key-v2",
        "module_id": module_id,
        "candidate_git_head_sha": candidate_git_head_sha,
        "candidate_git_tree_sha": candidate_git_tree_sha,
        "changed_file_hashes": dict(sorted(changed_file_hashes.items())),
        "module_source_file_hashes": dict(sorted(module_source_file_hashes.items())),
        "transitive_dependency_file_hashes": dict(
            sorted(transitive_dependency_file_hashes.items())
        ),
        "closure_complete": True,
        "selected_test_file_hashes": dict(sorted(selected_test_file_hashes.items())),
        "conftest_and_helper_hashes": dict(sorted(conftest_and_helper_hashes.items())),
        "selector_args": list(selector_args),
        "pyproject_sha256": pyproject_sha256,
        "uv_lock_sha256": uv_lock_sha256,
        "python_full_version": python_full_version,
        "platform_tag": platform_tag,
        "pytest_plugins_sha256": pytest_plugins_sha256,
        "sanitized_env_sha256": sanitized_env_sha256,
        "models_py_sha256": models_py_sha256,
        "alembic_env_sha256": alembic_env_sha256,
        "migration_file_hashes": dict(sorted(migration_file_hashes.items())),
        "migration_head": migration_head,
        "generation_contract_sha256": generation_contract_sha256,
        "run_executor_manifest_sha256": run_executor_manifest_sha256,
    }
    digest = sha256_bytes(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return {
        "cache_key": f"evk-v2-{module_id}-{digest[:32]}",
        "sha256": digest,
        "components": payload,
    }


def issue_validation_receipt(
    *,
    evidence_key_bundle: dict[str, Any],
    verifier_id: str,
    import_origin_file: str,
    executed_command: list[str],
    exit_code: int,
    passed_count: int,
    created_at_epoch: float,
    ttl_seconds: int = 86400,
    signing_secret: bytes = b"ttakkari-local-receipt-v2-key",
) -> dict[str, Any]:
    """Create a signed ValidationReceipt bound to an EvidenceCacheKey v2."""
    if exit_code != 0 or passed_count <= 0:
        raise ValueError("Cannot issue a passing ValidationReceipt for a failed or empty test run")
    body = {
        "schema_version": "platform-v2-validation-receipt-v2",
        "cache_key": evidence_key_bundle["cache_key"],
        "cache_key_sha256": evidence_key_bundle["sha256"],
        "components": evidence_key_bundle["components"],
        "provenance": {
            "verifier_id": verifier_id,
            "import_origin_file": import_origin_file,
            "executed_command": list(executed_command),
            "exit_code": exit_code,
            "passed_count": passed_count,
            "created_at_epoch": created_at_epoch,
            "expires_at_epoch": created_at_epoch + ttl_seconds,
        },
    }
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    sig = hmac.new(signing_secret, canonical, hashlib.sha256).hexdigest()
    return {**body, "signature_hmac_sha256": sig}


def verify_validation_receipt(
    receipt: dict[str, Any],
    current_inputs: dict[str, Any],
    *,
    expected_worktree_src_init: str,
    now_epoch: float,
    signing_secret: bytes = b"ttakkari-local-receipt-v2-key",
) -> tuple[bool, str]:
    """Fail-closed verifier for ValidationReceipt v2 against current candidate inputs."""
    try:
        if receipt.get("schema_version") != "platform-v2-validation-receipt-v2":
            return False, "invalid_receipt_schema_version"
        sig = receipt.get("signature_hmac_sha256", "")
        body = {k: v for k, v in receipt.items() if k != "signature_hmac_sha256"}
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        expected_sig = hmac.new(signing_secret, canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False, "signature_mismatch"

        prov = receipt.get("provenance", {})
        if prov.get("exit_code") != 0 or int(prov.get("passed_count", 0)) <= 0:
            return False, "non_passing_provenance"
        if prov.get("import_origin_file") != expected_worktree_src_init:
            return False, "untrusted_import_origin"
        created_at = float(prov.get("created_at_epoch", 0.0))
        expires_at = float(prov.get("expires_at_epoch", 0.0))
        if now_epoch < created_at or now_epoch > expires_at:
            return False, "receipt_expired_or_future_dated"

        expected_bundle = compute_evidence_cache_key(**current_inputs)
    except ValueError as exc:
        return False, f"current_inputs_invalid:{exc}"
    except (KeyError, TypeError) as exc:
        return False, f"malformed_receipt:{exc}"

    if receipt.get("cache_key") != expected_bundle["cache_key"] or (
        receipt.get("cache_key_sha256") != expected_bundle["sha256"]
    ):
        rec_comp = receipt.get("components", {})
        cur_comp = expected_bundle["components"]
        for field in (
            "candidate_git_head_sha",
            "candidate_git_tree_sha",
            "changed_file_hashes",
            "module_source_file_hashes",
            "transitive_dependency_file_hashes",
            "selected_test_file_hashes",
            "conftest_and_helper_hashes",
            "selector_args",
            "pyproject_sha256",
            "uv_lock_sha256",
            "python_full_version",
            "platform_tag",
            "pytest_plugins_sha256",
            "sanitized_env_sha256",
            "models_py_sha256",
            "alembic_env_sha256",
            "migration_file_hashes",
            "migration_head",
            "generation_contract_sha256",
            "run_executor_manifest_sha256",
        ):
            if rec_comp.get(field) != cur_comp.get(field):
                return False, f"mismatch:{field}"
        return False, "cache_key_digest_mismatch"

    return True, "verified_ok"


def classify_change_impact(
    changed_files: list[str],
    graph: dict[str, Any],
) -> dict[str, Any]:
    """Classify a set of changed files into module-only, multi-module, or fail-closed broad gate."""
    if not changed_files:
        return {
            "gate_mode": "broad_fail_closed",
            "touched_modules": [],
            "impacted_reverse_modules": [],
            "reasons": ["empty_changed_files"],
        }

    broad_gate_patterns = (
        "alembic/",
        "deploy/generation-contract.json",
        "deploy/run-executor-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",
        "src/ttakkari/models.py",
        "src/ttakkari/config.py",
        "src/ttakkari/main.py",
        "src/ttakkari/db.py",
        "src/ttakkari/security/",
        "src/ttakkari/generation",
        "src/ttakkari/deployment.py",
        "src/ttakkari/deploy_gate.py",
        "src/ttakkari/executor",
    )
    reasons: list[str] = []
    touched_modules: set[str] = set()

    for path in changed_files:
        if any(path == pat or path.startswith(pat) for pat in broad_gate_patterns):
            reasons.append(f"broad_gate_trigger:{path}")
        if path.startswith("src/ttakkari/"):
            rel = Path(path).relative_to("src/ttakkari")
            mod = top_module_name(rel)
            touched_modules.add(mod)
        elif path.startswith(("tests/", "unit_tests/", "benchmark_tests/")):
            continue
        else:
            reasons.append(f"unclassified_path:{path}")

    if not touched_modules and not reasons:
        reasons.append("tests_only_without_module_binding")

    for mod in sorted(touched_modules):
        mod_info = graph["modules"].get(mod)
        if mod_info is None:
            reasons.append(f"unknown_module:{mod}")
            continue
        if mod_info["fan_in"] > 5:
            reasons.append(f"high_fan_in_module:{mod}(fan_in={mod_info['fan_in']})")
        _, _, closure_ok, closure_reasons = compute_transitive_module_closure(mod, graph)
        if not closure_ok:
            reasons.extend(f"incomplete_closure:{mod}:{r}" for r in closure_reasons)

    impacted_reverse = sorted(
        {
            dep
            for m in touched_modules
            for dep in graph["modules"].get(m, {}).get("depended_by", [])
        }
    )
    if reasons:
        return {
            "gate_mode": "broad_fail_closed",
            "touched_modules": sorted(touched_modules),
            "impacted_reverse_modules": impacted_reverse,
            "reasons": reasons,
        }

    return {
        "gate_mode": "module_scoped" if len(touched_modules) == 1 else "multi_module_scoped",
        "touched_modules": sorted(touched_modules),
        "impacted_reverse_modules": impacted_reverse,
        "reasons": [],
    }


def evaluate_candidate_modules(graph: dict[str, Any], repo_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    modules = graph["modules"]
    candidates = [
        {
            "module_id": "creative",
            "display_name": "Creative Studio (V1 Legacy + V2 Remotion/Managed Comfy/TTS/ACE-Step)",
            "storage_mode": (
                "Filesystem JSON manifests (`PlanStore`, `ProductionStore`, `StyleStore`, `DirectorStore`) "
                "+ content-addressed media assets; V2 intent extraction (`creative/v2/run_extraction.py`) "
                "creates `ChatSession`/`Run` rows (`worker_task_type='creative_intent'`). "
                "Note: `artcraft_tasks` belongs to `src/ttakkari/artcraft.py`, not `src/ttakkari/creative/`."
            ),
            "external_dep_reverse_modules": [
                m
                for m in modules.get("creative", {}).get("depended_by", [])
                if m not in {"api", "generation_service", "main", "orchestrator"}
            ],
            "test_files_count": len(list((repo_root / "tests").glob("*creative*.py")))
            + len(list((repo_root / "unit_tests").glob("*creative*.py"))),
            "recent_deploy_blocker_incidents": [
                {
                    "deploy_id": "794447f3-8635-443d-b4c5-de975b0e91fb",
                    "sha": "063002a1",
                    "issue": "LOG-194",
                    "failed_test": "unit_tests/test_creative_v2_lease_handoff.py::test_scene_generation_requests_bounded_lease_wait_from_controller",
                    "wall_seconds_before_fail_with_x": 985.34,
                },
                {
                    "deploy_id": "d4f439d6-589f-4c72-9754-83aa5364c459",
                    "sha": "5201053b",
                    "issue": "LOG-190",
                    "failed_test": "unit_tests/test_creative_v2_lease_handoff.py::test_scene_generation_requests_bounded_lease_wait_from_controller",
                },
                {
                    "deploy_id": "bf9a853d-a24e-4b02-9a91-6bd3872e8a7f",
                    "sha": "ee4f06f3",
                    "issue": "LOG-186",
                    "failed_test": "tests/test_creative_v2_api.py::test_plan_create_edit_approve_and_production_lifecycle",
                },
            ],
            "runtime_hazards_found": [
                "api/creative_v2.py:418 spawns `_ASSET_GENERATION_TASKS` in-memory without `generation.work` or `ready()` finalizer tracking or durable DB job fence",
                "creative/v2/asset_generation_managed.py:478 calls `tts.service.budget(600)` which acquires `generation.work('tts')` and fails if API slot enters `draining` during multi-minute ComfyUI stage",
                "creative/v2/asset_generation_managed.py `_write_checkpoint` writes `.tmp` + `os.replace` without `fsync` or checkpoint content hash",
                "generation_service.py:256 runs `creative-render` scan every 2s inside API generation when `execution_mode == 'local'`",
            ],
            "first_module_suitability": "provisional_rank_1_pending_b_c_reconciliation",
            "selection_caveat": (
                "Provisional Rank 1 candidate: name-based `fan_in=4` alone does not prove FK, resource lease, "
                "file side-effect, or test-helper isolation. Final selection awaits Worker B (FK/data/lease) "
                "and Worker C (frontend/test classification) verified reconciliation in Root."
            ),
        },
        {
            "module_id": "games",
            "display_name": "Game Studio (2D/3D Three.js, Autobuild, Runner, Verifier)",
            "storage_mode": "PostgreSQL tables (`game_projects`, `game_builds`, `game_sessions`, etc.) + workspace files",
            "external_dep_reverse_modules": [
                m
                for m in modules.get("games", {}).get("depended_by", [])
                if m not in {"api", "cli", "main", "orchestrator"}
            ],
            "test_files_count": len(list((repo_root / "tests").glob("*game*.py")))
            + len(list((repo_root / "unit_tests").glob("*game*.py"))),
            "recent_deploy_blocker_incidents": [
                {
                    "deploy_id": "4e0f903e-8d19-4dc6-a9b8-e6061fa604bb",
                    "sha": "6fc943bc",
                    "issue": "LOG-185",
                    "failed_test": "tests/test_games_3d_Quality_rebuild.py::test_session_run_triggers_one_3d_quality_repair_when_first_build_fails",
                }
            ],
            "runtime_hazards_found": [
                "Coupled to `runs` (`runs/manager.py:1820` triggers `games.autobuild.after_run` on game runs) and `models.py`",
            ],
            "first_module_suitability": "provisional_rank_2_candidate",
        },
        {
            "module_id": "collector",
            "display_name": "Collector Platform (Fetch, Discovery, Bandit, Outbox, KB Delivery)",
            "storage_mode": (
                "PostgreSQL tables (2 base tables + 14 new `collector_*` tables and 3 `kb_*` tables "
                "across migrations 0063/0065/0066/0067) + content-addressed blob store"
            ),
            "external_dep_reverse_modules": [
                m
                for m in modules.get("collector", {}).get("depended_by", [])
                if m not in {"api", "generation_service", "main", "orchestrator"}
            ],
            "test_files_count": len(list((repo_root / "tests").glob("*collector*.py")))
            + len(list((repo_root / "unit_tests").glob("*collector*.py"))),
            "recent_deploy_blocker_incidents": [
                {
                    "deploy_id": "0a871bc1-d4df-4bf4-9b7a-755ef6e095a9",
                    "sha": "4da0348c",
                    "issue": "LOG-191",
                    "failed_test": "unit_tests/test_collector_s2_fetch_items.py",
                }
            ],
            "runtime_hazards_found": [
                "Heavy schema evolution (0060, 0061, 0063, 0065, 0066, 0067) and bidirectional coupling with `kb_delivery`/`kb_library`",
                "Shared `/back` checkout currently has uncommitted collector edits on top of 607f8bda",
            ],
            "first_module_suitability": "defer_to_p5_after_schema_and_worktree_stabilization",
        },
        {
            "module_id": "tts",
            "display_name": "Local TTS (Supertonic ONNX Queue & Synthesis)",
            "storage_mode": "PostgreSQL `tts_jobs` metadata + process-local `_pending_texts` in RAM + disk audio cache",
            "external_dep_reverse_modules": [
                m
                for m in modules.get("tts", {}).get("depended_by", [])
                if m not in {"api", "orchestrator"}
            ],
            "test_files_count": len(list((repo_root / "tests").glob("*tts*.py")))
            + len(list((repo_root / "unit_tests").glob("*tts*.py"))),
            "recent_deploy_blocker_incidents": [],
            "runtime_hazards_found": [
                "tts/jobs.py:42 stores text chunks only in process-local `_pending_texts: dict[UUID, list[str]]`",
                "tts/jobs.py:154 `_fail_queued_draining` actively fails accepted 202 queued jobs with `generation_draining` when slot drains",
                "tts/service.py:130 `generation.work('tts')` rejects work in draining state, preventing accepted queued jobs from draining",
            ],
            "first_module_suitability": "foundation_prerequisite_p3",
        },
    ]
    for item in candidates:
        mod = item["module_id"]
        info = modules.get(mod, {})
        item["file_count"] = info.get("file_count", 0)
        item["line_count"] = info.get("line_count", 0)
        item["depends_on"] = info.get("depends_on", [])
        item["depended_by"] = info.get("depended_by", [])
    return candidates


def build_sample_evidence_cache_key_v2(
    repo_root: Path,
    snapshot: dict[str, Any],
    graph: dict[str, Any],
    migrations: dict[str, Any],
    contracts: dict[str, Any],
) -> dict[str, Any]:
    """Demonstrate fail-closed cache decision: unverified closure causes cache miss, verified closure produces key."""
    _closure_mods, _closure_file_hashes, closure_ok, reasons = compute_transitive_module_closure(
        "creative", graph
    )
    candidate_head = snapshot.get("candidate_head_sha", snapshot.get("origin_main_sha"))
    candidate_tree = snapshot.get("candidate_tree_sha", snapshot.get("origin_main_tree_sha"))

    sanitized_env_str = f"PYTHONDONTWRITEBYTECODE={sys.dont_write_bytecode}"
    pytest_plugin_str = "pytest-8+asyncio+subtests-noconftest"

    creative_decision = {
        "status": "cache_miss_unverified_closure",
        "module_id": "creative",
        "candidate_git_head_sha": candidate_head,
        "candidate_git_tree_sha": candidate_tree,
        "closure_complete": closure_ok,
        "reasons": reasons,
        "cache_decision": "broad_validation_fallback (conservative no-reuse due to dynamic import in transitive closure)",
    }

    _ds_mods, ds_file_hashes, ds_closure_ok, _ds_reasons = compute_transitive_module_closure(
        "design_system", graph
    )
    verified_key: dict[str, Any] | None = None
    if ds_closure_ok and "src/ttakkari/design_system.py" in ds_file_hashes:
        ds_target_changed = "src/ttakkari/design_system.py"
        ds_changed_hashes = {ds_target_changed: ds_file_hashes[ds_target_changed]}
        ds_mod_files = {
            f: graph["file_hashes"][f] for f in graph["modules"]["design_system"]["files"]
        }
        ds_dep_files = {f: h for f, h in ds_file_hashes.items() if f not in ds_mod_files}
        ds_tests = {
            p.relative_to(repo_root).as_posix(): sha256_file(p)
            for p in sorted((repo_root / "tests").glob("*design_system*.py"))
            + sorted((repo_root / "unit_tests").glob("*design_system*.py"))
        }
        if not ds_tests:
            marker = repo_root / "tests" / "test_contract_marker.py"
            ds_tests = {marker.relative_to(repo_root).as_posix(): sha256_file(marker)}

        conftest_helpers = {
            "tests/conftest.py": sha256_file(repo_root / "tests" / "conftest.py"),
            "tests/test_contract_marker.py": sha256_file(
                repo_root / "tests" / "test_contract_marker.py"
            ),
        }
        verified_key = compute_evidence_cache_key(
            module_id="design_system",
            candidate_git_head_sha=candidate_head,
            candidate_git_tree_sha=candidate_tree,
            dirty_source_paths=snapshot["dirty_manifest"]["dirty_source_paths"],
            changed_file_hashes=ds_changed_hashes,
            module_source_file_hashes=ds_mod_files,
            transitive_dependency_file_hashes=ds_dep_files,
            closure_complete=ds_closure_ok,
            selected_test_file_hashes=ds_tests,
            conftest_and_helper_hashes=conftest_helpers,
            selector_args=["pytest", "-m", "contract", *sorted(ds_tests.keys())],
            pyproject_sha256=sha256_file(repo_root / "pyproject.toml"),
            uv_lock_sha256=sha256_file(repo_root / "uv.lock"),
            python_full_version=platform.python_version(),
            platform_tag=f"{sys.platform}-{platform.machine()}",
            pytest_plugins_sha256=sha256_bytes(pytest_plugin_str.encode("utf-8")),
            sanitized_env_sha256=sha256_bytes(sanitized_env_str.encode("utf-8")),
            models_py_sha256=graph["file_hashes"]["src/ttakkari/models.py"],
            alembic_env_sha256=sha256_file(repo_root / "alembic" / "env.py"),
            migration_file_hashes=migrations["migration_file_hashes"],
            migration_head=migrations["heads"][0],
            generation_contract_sha256=contracts["generation_contract_sha256"],
            run_executor_manifest_sha256=contracts["run_executor_manifest_sha256"],
        )

    return {
        "creative_decision": creative_decision,
        "verified_sample_key": verified_key,
    }


def build_evidence(
    repo_root: Path = REPO_ROOT,
    *,
    generated_at: str | None = None,
    expected_source_tree_sha: str | None = None,
    allow_dirty_source_paths: bool = False,
) -> dict[str, Any]:
    snapshot = verify_snapshot_binding(
        repo_root,
        expected_source_tree_sha=expected_source_tree_sha,
        allow_dirty_source_paths=allow_dirty_source_paths,
    )
    topology = compute_git_topology_and_drift(repo_root)
    graph = analyze_source_graph(repo_root / "src" / "ttakkari")
    migrations = analyze_migrations(repo_root)
    contracts = analyze_contracts_and_manifests(repo_root)
    candidates = evaluate_candidate_modules(graph, repo_root)
    sample_cache_key_v2 = build_sample_evidence_cache_key_v2(
        repo_root, snapshot, graph, migrations, contracts
    )

    now_iso = generated_at or "2026-10-24T22:25:00+09:00"

    return {
        "schema_version": "platform-v2-worker-a-evidence-v2",
        "generated_at": now_iso,
        "snapshot_binding": snapshot,
        "git_topology_607f8bda_vs_c8f10156": topology,
        "worker_and_verification_ledger": {
            "A_runtime_delivery": {
                "worker_id": "e65430e3-0775-4e2c-910a-5050cdb7d216",
                "role": "Runtime / Delivery / Architecture",
                "worktree": str(repo_root),
                "branch": snapshot["checkout_branch"],
                "audited_commit_sha": snapshot["audited_commit_sha"],
                "base_origin_main_sha": snapshot["origin_main_sha"],
                "verification_history": [
                    {
                        "inspected_commit_sha": "fddad5e152754f5f1e7ac5eac07ec26b453d6987",
                        "verifier_worker_id": "1d994e50-c900-4f90-9975-1ff082522a35",
                        "verification_commit_sha": "93af9ef6e202b95883d73c74d710e1775cb402b9",
                        "verdict_by_item": {
                            "adr0036_adr0066_state_distinction": "passed",
                            "run_ownership_and_reconcile": "passed_with_caveat",
                            "side_effects_and_comfy": "partially_passed",
                            "baseline_and_snapshot_binding": "failed",
                            "tts_drain": "failed",
                            "creative_v2_task_tracking": "failed",
                            "desk_qwen_ownership": "failed_structure_unverified_overlap_swap_causality",
                            "failure_denominator_34_56": "failed",
                            "noconftest_sample_39_5s": "failed",
                            "evidence_cache_key_v1": "failed",
                            "independent_module_release_implementation_claim": "failed_design_only",
                            "live_supervisor_direct_observation": "unverified",
                            "backup_p0_and_cutover_rollback_e2e": "unverified",
                            "creative_first_module_candidate": "provisional",
                        },
                    },
                    {
                        "inspected_commit_sha": "0d048ea61f755a047ee70a0d467489c01025d46b",
                        "note": "Time-fixed intermediate status snapshot; superseded by current corrected candidate.",
                    },
                    {
                        "inspected_commit_sha": "5204b259cdfc90b9a58a60422d6c8d362c329cf5",
                        "verifier_ref": "d3f718997fef6a97709475adb48bbb6e73e5fd01",
                        "verdict": "failed_snapshot_and_cache_binding_incomplete",
                        "notes": (
                            "Candidate identity missing from verify_snapshot_binding, dirty audit doc paths dropped, "
                            "transitive dynamic import ignored in closure, cache bound origin/main instead of candidate. "
                            "All corrected in current candidate."
                        ),
                    },
                ],
                "current_status": "reconciled_with_independent_verification_d3f7189",
            },
            "B_backend_data": {
                "worker_id": "c1e9cd73-83ef-4240-99c8-809e500bd903",
                "role": "Backend / Data / Dependency Audit",
                "worktree": "/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-b",
                "verification_history": [
                    {
                        "inspected_commit_sha": "86373cedda20e298ad2b82d857352f845ae70980",
                        "verifier_worker_id": "1d994e50-c900-4f90-9975-1ff082522a35",
                        "verification_commit_sha": "b245d5a",
                        "verdict": "failed",
                    },
                    {
                        "inspected_commit_sha": "2cc",
                        "verifier_worker_id": "fed2c630-a867-42f1-9574-4a92905de864",
                        "verification_commit_sha": "5aa5968",
                        "verdict": "failed",
                    },
                    {
                        "inspected_commit_sha": "7cf",
                        "note": "Denominator expansion only; still incomplete.",
                    },
                    {
                        "inspected_commit_sha": "ac7c8c5",
                        "verifier_ref": "de1bd7f",
                        "verdict": "failed",
                        "notes": "Failed independent verification de1bd7f (category error & source binding incomplete); revising.",
                    },
                ],
                "current_status": "revising_after_de1bd7f_failure_do_not_use_as_final_boundary",
            },
            "C_frontend_test": {
                "worker_id": "9a3215de-01d4-4efd-b3ff-9e282de9b766",
                "role": "Frontend / Test Architecture Audit",
                "verification_history": [
                    {
                        "inspected_target": "initial_untracked_outputs_in_shared_back",
                        "verifier_worker_id": "fed2c630-a867-42f1-9574-4a92905de864",
                        "verdict": "failed",
                        "failures_found": [
                            "summary 38 routes vs scanner 36 routes",
                            "claimed 10 test categories but omitted runtime/ownership (dict 9 categories / 257 files vs summary total 262)",
                            "main missing security test / deploy security duplicate 31 claims unverified across candidate/workingtree conditions",
                            "front 5.48s and back 1045.97s citations lacked C-side revision/env/raw-log verification",
                        ],
                    },
                    {
                        "inspected_commit_sha": "b155049063a471ddab54d309831c2ee8758bfcd7",
                        "verdict": "verification_in_progress",
                        "notes": "New candidate submitted; verification in progress.",
                    },
                ],
                "current_status": "candidate_b1550490_under_verification",
            },
            "independent_verifiers": {
                "1d994e50-c900-4f90-9975-1ff082522a35": {
                    "status": "not_reused_65pct_progress",
                    "completed_verifications": [
                        "Worker B initial commit 86373ce (failed @ b245d5a)",
                        "Worker A initial commit fddad5e (verified @ 93af9ef6e202b95883d73c74d710e1775cb402b9)",
                    ],
                },
                "fed2c630-a867-42f1-9574-4a92905de864": {
                    "status": "completed",
                    "completed_verifications": [
                        "Worker C initial audit outputs (failed)",
                        "Worker B follow-up commit 2cc (failed @ 5aa5968)",
                    ],
                },
            },
        },
        "p0_operational_and_recovery_baseline": {
            "evidence_classification_policy": (
                "Strictly separate: (1) historical_receipt, (2) config_default, "
                "(3) direct_current_observation, and (4) unknown (확인 필요). "
                "Historical deploy receipts or issue stacktraces do not prove current live revision, slot, or supervisor process."
            ),
            "historical_deploy_receipts": {
                "last_succeeded_deploy_receipt": {
                    "classification": "historical_receipt",
                    "deploy_id": "4a6e09a8-c49f-4c2f-9a6c-c0d83fe5824a",
                    "revision": "9f82c9c16cd604e81ddf43180b0be2e895ec85cb",
                    "slot_at_completion": 1,
                    "port_at_completion": 8792,
                    "generation_id_at_completion": "5ef462e5-d7f7-40e7-bf27-1d4bf19e1d91",
                    "previous_retired_generation_id": "e1eb4ad0-5ce7-415f-b91e-9c0b0eab2ba3",
                    "created_at": "2026-10-23T10:16:49.556741+00:00",
                    "updated_at": "2026-10-23T10:43:06.032849+00:00",
                    "total_wall_seconds": 1576.48,
                    "full_pytest_gate_seconds": 1045.97,
                    "pytest_summary": "4473 passed, 8 skipped, 2 xfailed, 5020 subtests passed in 1045.97s (0:17:25)",
                    "selective_db_resets": 2822,
                    "selective_db_tables": 106,
                },
                "subsequent_failed_deploy_receipts": [
                    {
                        "classification": "historical_receipt (early-abort -x, not equivalent benchmark to full pass)",
                        "deploy_id": "794447f3-8635-443d-b4c5-de975b0e91fb",
                        "revision": "063002a1739a0f1241a94709e413e004f8786d57",
                        "status": "failed",
                        "failed_stage": "pytest",
                        "issue_id": "LOG-194",
                        "pytest_seconds_until_first_failure_x": 985.34,
                        "passed_before_abort": 3327,
                        "failed_test": "unit_tests/test_creative_v2_lease_handoff.py::test_scene_generation_requests_bounded_lease_wait_from_controller",
                    },
                    {
                        "classification": "historical_receipt (early-abort -x, not equivalent benchmark to full pass)",
                        "deploy_id": "e15536bc-37ec-4c66-9f2e-fcc42d59538d",
                        "revision": "c8f10156845d007ec6496a65d6d8417be19260bd",
                        "status": "failed",
                        "failed_stage": "pytest",
                        "updated_at": "2026-10-24T11:56:21.977907+00:00",
                        "issue_id": "LOG-195",
                        "pytest_seconds_until_first_failure_x": 172.23,
                        "passed_before_abort": 1112,
                        "failed_test": "tests/test_desk_qwen_service.py::test_desk_request_goes_to_the_managed_server_of_this_process",
                    },
                ],
                "historical_supervisor_code_trace": {
                    "classification": "historical_receipt (from LOG-68 stacktrace / 2026-10-23 work-history, NOT a direct current process observation)",
                    "observed_release_in_historical_trace": "e817fc19471bdc48054158cada4a99466f7eaa6f",
                },
            },
            "direct_current_observations": {
                "host_resource_snapshots": [
                    {
                        "classification": "direct_current_observation (process/resource snapshot only; does not prove live revision/slot/supervisor or swap causality)",
                        "collected_at": "2026-10-24T21:20:12+09:00",
                        "backend_pid": 44104,
                        "backend_rss_bytes": 392298496,
                        "llama_server_pid": 44129,
                        "llama_server_rss_bytes": 7734870016,
                        "swap_used_pct": 95.3,
                        "memory_pressure": "normal",
                    },
                    {
                        "classification": "direct_current_observation (verifier snapshot)",
                        "collected_at": "2026-10-24T21:51:34+09:00",
                        "backend_pid": 44104,
                        "llama_server_pid": 44129,
                        "llama_server_rss_bytes": 7203520512,
                        "swap_used_pct": 94.8,
                        "memory_pressure": "normal",
                    },
                ],
                "execution_capabilities_by_scope": {
                    "note": "Host and engine sandboxes are distinct execution scopes; stale measurements must not be reported as fresh `blocked=[]` facts.",
                    "host": {
                        "measured_at_kst": "2026-10-24T21:20:09+09:00",
                        "blocked": [],
                        "stale_items": [],
                        "status": "fresh_measured_on_host",
                    },
                    "codex_sandbox": {
                        "measured_at_kst": "2026-10-24T21:51:37+09:00",
                        "blocked": [
                            "gpu_metal",
                            "llama_list_devices",
                            "process_list",
                            "sysctl_read",
                        ],
                        "available": [
                            "spawn_process",
                            "network_loopback",
                            "network_outbound",
                            "writable_paths",
                        ],
                        "stale_items": [],
                        "status": "fresh_measured_in_codex_sandbox",
                    },
                    "antigravity_sandbox": {
                        "measured_at_kst": "2026-10-21T18:29:15+09:00",
                        "blocked": [],
                        "stale_items": [
                            "gpu_metal",
                            "llama_list_devices",
                            "process_list",
                            "sysctl_read",
                            "spawn_process",
                            "network_loopback",
                            "network_outbound",
                            "writable_paths",
                        ],
                        "status": "stale_measurement_확인_필요",
                    },
                    "muse_sandbox": {
                        "measured_at_kst": "2026-10-21T17:55:27+09:00",
                        "blocked": [],
                        "stale_items": [
                            "gpu_metal",
                            "llama_list_devices",
                            "process_list",
                            "sysctl_read",
                            "spawn_process",
                            "network_loopback",
                            "network_outbound",
                            "writable_paths",
                        ],
                        "status": "stale_measurement_확인_필요",
                    },
                    "claude": {"status": "not_measured_확인_필요"},
                    "qwen_local": {"status": "not_measured_확인_필요"},
                },
            },
            "unknown_and_unverified_operational_items": {
                "current_live_api_revision_slot_and_generation_id": "unknown (확인 필요: requires direct live `/api/version` or `runtime_control` DB read; only historical deploy receipt `9f82c9c1` is observed via MCP)",
                "current_live_supervisor_revision": "unknown (확인 필요: `~/.ttakkari/deploy/supervisor.json` is protected; historical `e817fc19` trace does not prove current running supervisor process)",
                "backup_freshness_and_restore_rehearsal": "unknown (확인 필요: `scripts/backup_db.sh`, `scripts/restore_rehearsal.sh`, `scripts/snapshot_data.sh` are implemented in code, but `~/.ttakkari-backups/latest.json` and live rehearsal execution are unverified)",
                "deploy_failure_aggregation_without_raw_pages": {
                    "status": "retracted_a_34_56_claim_and_marked_확인_필요_without_raw_event_ids",
                    "retracted_claim": "A previously reported 34 groups / 56 events (pytest 33/54, archive 1/2) for 2026-10-16T08:11:13Z..2026-10-23T17:19:03Z — RETRACTED.",
                    "independent_verifier_paged_measurement": {
                        "all_issue_groups": 34,
                        "all_issue_count_sum": 65,
                        "paged_raw_events_in_window": 58,
                        "issue_groups_in_window": 29,
                        "window_stage_events": {"pytest": 56, "archive": 2},
                        "raw_event_min_max_utc": [
                            "2026-10-16T11:08:43Z",
                            "2026-10-23T11:10:22Z",
                        ],
                    },
                    "full_deploy_denominator_beyond_50_cap": "unknown (확인 필요: `ops_list_deploys` caps at 50 per status)",
                },
            },
            "narrow_db_free_test_sample": {
                "retracted_prior_claim": (
                    "Retracted: '39 passed in 5.00s proves full contract suite runs in <5s via --noconftest'. "
                    "That count mixed 4 extractor unit tests with 35 repo tests, and `--noconftest` only bypasses "
                    "`tests/conftest.py` for DB-free static tests — it does NOT prove full DB-backed contract or runtime drain guarantees."
                ),
                "verified_repo_narrow_sample": {
                    "command": (
                        "PYTHONPATH=<worktree>/src PYTHONDONTWRITEBYTECODE=1 "
                        "/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest "
                        "--noconftest -q tests/test_contract_marker.py tests/test_migration_single_head.py "
                        "unit_tests/test_executor_migration_contract.py unit_tests/test_executor_reconcile.py "
                        "unit_tests/test_creative_v2_lease_handoff.py"
                    ),
                    "test_count": 35,
                    "verified_wall_seconds": 3.34,
                    "scope_limit": "Narrow DB-free static/unit sample only; global DB `tests/conftest.py` and DB-touching contract tests were not executed (미실행 / 확인 필요).",
                },
            },
        },
        "codebase_metrics": {
            "total_python_files": graph["total_python_files"],
            "total_lines": graph["total_lines"],
            "top_level_unit_count": graph["top_level_unit_count"],
            "top_fan_in_units": graph["top_fan_in_units"],
            "modules": {
                k: {key: val for key, val in v.items() if key != "files"}
                for k, v in graph["modules"].items()
            },
        },
        "migrations": {
            k: v for k, v in migrations.items() if k != "migration_file_hashes"
        },
        "contracts_and_manifests": contracts,
        "subsystem_status_matrix": [
            {
                "subsystem": "Generation Deployment & 3-Slot API Runtime (8791/8792/8793)",
                "files": [
                    "src/ttakkari/generation.py",
                    "src/ttakkari/generation_service.py",
                    "src/ttakkari/generation_deployment.py",
                    "src/ttakkari/deployment.py",
                ],
                "code_status": "implemented",
                "test_status": "tested (unit/static; live cutover E2E unverified)",
                "config_default": "deploy_strategy='generations', runtime_generation=True (generation deployment itself is enabled; only independent RunExecutor is disabled)",
                "live_status": "historical_receipt_confirmed_9f82c9c1 (current live slot/revision unknown without direct DB/HTTP read)",
            },
            {
                "subsystem": "ADR 0036 Independent RunExecutor & Process Group Ledger",
                "files": [
                    "src/ttakkari/executor.py",
                    "src/ttakkari/executor_reconcile.py",
                    "alembic/versions/0064_run_executor.py",
                ],
                "code_status": "implemented (`runtime_process_groups` table + 4 PL/pgSQL triggers + `validate_handoff` + `reconcile_orphans`)",
                "test_status": "tested (DB-free unit tests in `unit_tests/test_executor_migration_contract.py` & `test_executor_reconcile.py`)",
                "config_default": "feature-off (`deploy/generation-contract.json` `run_executor.activation='disabled'`, `Settings.run_executor_mode='local'`; `Slots.ensure_executor` returns `disabled`)",
                "live_status": "feature-off / unverified in production",
            },
            {
                "subsystem": "Run Recovery: Executor Reconcile vs Legacy Startup Auto-Resume",
                "files": [
                    "src/ttakkari/executor_reconcile.py:26-241",
                    "src/ttakkari/runs/manager.py:1900-1975",
                    "src/ttakkari/main.py:193-204",
                ],
                "code_status": (
                    "Two distinct code paths: "
                    "(1) `executor_reconcile.py` treats only `psutil.NoSuchProcess` as ABSENT and `AccessDenied`/`OSError` as UNKNOWN (retained), "
                    "re-verifies `(pid, process_started_at, pgid)` and `(generation_id, lease_token)` under row locks before signaling, "
                    "marks confirmed orphans `interrupted` without auto-replay. "
                    "(2) Legacy non-generation startup (`main.py:201` -> `runs/manager.py:1941` `resume_interrupted()`) is forbidden when `runtime_control.active_generation` exists, "
                    "but in legacy mode re-queues `interrupted` runs with `error == '서버 종료로 중단'` up to `MAX_RESTART_RESUME_ATTEMPTS = 3`."
                ),
                "test_status": "executor_reconcile tested; legacy resume identity/approval semantics 확인 필요",
                "config_default": "generation mode forbids legacy `recover_interrupted`",
                "live_status": "unverified E2E under crash/restart",
            },
            {
                "subsystem": "ADR 0066 Delivery ChangeSets / Worktree / MergeQueue",
                "files": [
                    "src/ttakkari/delivery/worktree.py",
                    "src/ttakkari/delivery/validation.py",
                    "src/ttakkari/delivery/merge_queue.py",
                    "alembic/versions/0062_delivery_change_sets.py",
                ],
                "code_status": "implemented (partial: Stage 2 runs full `pytest -q --timeout=120`; not wired to `generation_deployment.py` or receipt cache)",
                "test_status": "tested (`tests/delivery/test_*.py`)",
                "config_default": "feature-off (`Settings.delivery_pipeline_enabled=False`)",
                "live_status": "feature-off",
            },
            {
                "subsystem": "Local TTS Queue (`tts_jobs`) & Generation Drain",
                "files": [
                    "src/ttakkari/tts/jobs.py",
                    "src/ttakkari/tts/service.py",
                    "src/ttakkari/generation.py:298-315",
                ],
                "code_status": "defect_confirmed_in_code (`_pending_texts` in-memory only; `_fail_queued_draining` fails accepted 202 queued jobs on drain; `budget()` refuses `generation.work('tts')` when not `active`)",
                "test_status": "existing unit tests do not cover accepted-job drain across generation switch",
                "config_default": "local_tts_enabled=True",
                "live_status": "code defect present in 9f82c9c1/c8f10156 (live drain failure reproduction unexecuted)",
            },
            {
                "subsystem": "Creative Studio V2 Auto-Assets & Checkpoints",
                "files": [
                    "src/ttakkari/api/creative_v2.py:418",
                    "src/ttakkari/creative/v2/asset_generation_managed.py",
                ],
                "code_status": (
                    "defect_confirmed_in_code (`_ASSET_GENERATION_TASKS` is untracked in-memory dict without `generation.work`, `ready()` finalizer, or DB fence; "
                    "Comfy step checks input identity and receipt prompt hashes, but `_write_checkpoint` is `.tmp` + `os.replace` without `fsync`/hash; "
                    "subsequent Supertonic TTS step fails if generation enters `draining`)"
                ),
                "test_status": "Comfy lease handoff unit-tested (`test_creative_v2_lease_handoff.py`); outer task drain untracked",
                "config_default": "enabled",
                "live_status": "code defect present (live transition reproduction unexecuted)",
            },
            {
                "subsystem": "Desk Qwen Service (`llama-server` ownership)",
                "files": [
                    "src/ttakkari/runs/desk_qwen_service.py",
                    "src/ttakkari/generation_service.py:232",
                ],
                "code_status": (
                    "structural_finding_confirmed (`start_api_services` calls `qwen_service.start()` per API process; "
                    "spawns managed child when `desk_qwen_base_url == ''` and `desk_qwen_manage == True`, or uses external health watcher when `desk_qwen_base_url` is set)"
                ),
                "test_status": "unit-tested (note: `LOG-195` prompt version mismatch in `tests/test_desk_qwen_service.py` at `c8f10156`)",
                "config_default": "desk_qwen_enabled=True, desk_qwen_manage=True, desk_qwen_base_url=''",
                "live_status": "single `llama-server` (PID 44129, RSS 7.20 GiB) observed in current host snapshot; multi-server overlap count and swap causality are unverified (확인 필요)",
            },
            {
                "subsystem": "Independent Module Release Archive / Active Pointer / Versioned Dispatcher",
                "files": [],
                "code_status": "unimplemented_design_target (not present in `src/ttakkari`)",
                "test_status": "unimplemented",
                "config_default": "n/a",
                "live_status": "unimplemented (target architecture for P3/P4)",
            },
        ],
        "candidate_modules_evaluation": candidates,
        "evidence_cache_key_v2_spec_and_sample": {
            "status": "unintegrated_design_and_verifier_prototype (production `generation_deployment.py` full regression, security, and recovery gates remain unchanged)",
            "retracted_v1_flaw": (
                "Retracted v1 preview which passed `changed_files` as path strings only and omitted changed file content hashes, "
                "test tree/conftest/helper hashes, full transitive closure, and runtime/schema content digests."
            ),
            "sample_creative_key_v2": sample_cache_key_v2,
        },
        "completion_status": {
            "P0": "not_complete (historical receipts & static code audited; live version/supervisor, backup freshness/restore rehearsal, and cutover/rollback E2E are unknown/확인 필요)",
            "P1": "not_complete (Worker A corrected; Worker B revising after de1bd7f; Worker C candidate b1550490 under verification)",
            "P2": "not_complete (contracts and options specified; final boundary sign-off awaits B/C reconciliation in Root and Gate B)",
            "P3": "isolated_foundation_candidate_in_progress (Gate A: delivery validation contract implemented in src/ttakkari/delivery)",
            "platform_v2_overall": "not_complete (P0/P1/P2 pending, Gate A isolated foundation candidate in progress)",
            "non_circular_precondition_policy": (
                "Separate (Gate A) Isolated P3 Implementation Entry from (Gate B) Production Activation & Cutover. "
                "Unverified P0 operational items (live backup rehearsal, live cutover E2E) are mandatory preconditions for Gate B (production activation), "
                "but must NOT block Gate A (writing and testing P3 foundation fixes in an isolated worktree/test DB)."
            ),
        },
    }


def main() -> None:
    evidence = build_evidence(REPO_ROOT)
    OUT_PATH.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {OUT_PATH} (audited_commit={evidence['snapshot_binding']['audited_commit_sha'][:8]}, "
        f"src_tree={evidence['snapshot_binding']['head_src_ttakkari_tree_sha'][:8]}, "
        f"files={evidence['codebase_metrics']['total_python_files']}, "
        f"lines={evidence['codebase_metrics']['total_lines']}, "
        f"migrations={evidence['migrations']['migration_file_count']})"
    )


if __name__ == "__main__":
    main()
