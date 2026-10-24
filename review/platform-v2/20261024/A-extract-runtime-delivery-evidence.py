"""Reproducible extractor for Worker A (runtime / delivery / architecture) evidence.

Generates `review/platform-v2/20261024/A-runtime-delivery-evidence.json` by combining:
1. Deterministic static/AST analysis of the backend repository at the audited revision.
2. Verified P0 operational baseline receipts collected via Ttakkari MCP ops/deploy tools.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src" / "ttakkari"
OUTPUT_PATH = Path(__file__).resolve().parent / "A-runtime-delivery-evidence.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def top_module_name(rel_path: Path) -> str:
    """Return top-level subpackage or module name under `src/ttakkari`."""
    parts = rel_path.parts
    if len(parts) == 1:
        return parts[0].removesuffix(".py")
    return parts[0]


def extract_ast_imports(py_file: Path) -> set[str]:
    """Return internal `ttakkari.<top>` targets imported by `py_file`."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("ttakkari."):
                    sub = alias.name.split(".")[1]
                    targets.add(sub)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "ttakkari":
                for alias in node.names:
                    targets.add(alias.name)
            elif node.module.startswith("ttakkari."):
                sub = node.module.split(".")[1]
                targets.add(sub)
    return targets


def analyze_source_graph(src_root: Path = SRC_ROOT) -> dict[str, Any]:
    py_files = sorted(src_root.rglob("*.py"))
    module_files: dict[str, list[str]] = defaultdict(list)
    module_lines: dict[str, int] = defaultdict(int)
    forward_deps: dict[str, set[str]] = defaultdict(set)
    reverse_deps: dict[str, set[str]] = defaultdict(set)
    file_reverse_deps: dict[str, set[str]] = defaultdict(set)

    total_lines = 0
    for f in py_files:
        rel = f.relative_to(src_root)
        mod = top_module_name(rel)
        rel_repo = f.relative_to(src_root.parents[1]).as_posix()
        lines = len(f.read_text(encoding="utf-8").splitlines())
        total_lines += lines
        module_files[mod].append(rel_repo)
        module_lines[mod] += lines
        imports = extract_ast_imports(f)
        for target in imports:
            if target != mod:
                forward_deps[mod].add(target)
                reverse_deps[target].add(mod)
            file_reverse_deps[target].add(rel_repo)

    all_mods = sorted(module_files.keys())
    module_summary: dict[str, Any] = {}
    for mod in all_mods:
        f_deps = sorted(forward_deps.get(mod, set()))
        r_deps = sorted(reverse_deps.get(mod, set()))
        module_summary[mod] = {
            "file_count": len(module_files[mod]),
            "line_count": module_lines[mod],
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
    }


def analyze_migrations(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    versions_dir = repo_root / "alembic" / "versions"
    files = sorted(versions_dir.glob("*.py"))
    revisions: dict[str, str] = {}
    down_revisions: dict[str, list[str]] = {}
    for f in files:
        text = f.read_text(encoding="utf-8")
        rev_m = re.search(
            r'^revision(?:\s*:\s*[^=]+)?\s*=\s*["\']([^"\']+)["\']',
            text,
            re.MULTILINE,
        )
        if not rev_m:
            continue
        rev = rev_m.group(1)
        revisions[rev] = f.name
        down_m = re.search(
            r"^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(.+)$",
            text,
            re.MULTILINE,
        )
        downs: list[str] = []
        if down_m:
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
    }


def analyze_contracts_and_manifests(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    gen_contract_path = repo_root / "deploy" / "generation-contract.json"
    exec_manifest_path = repo_root / "deploy" / "run-executor-manifest.json"
    gen_contract = json.loads(gen_contract_path.read_text(encoding="utf-8"))
    exec_manifest = json.loads(exec_manifest_path.read_text(encoding="utf-8"))

    gen_service_path = repo_root / "src" / "ttakkari" / "generation_service.py"
    gen_service_tree = ast.parse(gen_service_path.read_text(encoding="utf-8"))
    maintenance_names: list[str] = []
    for node in gen_service_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "maintenance_jobs":
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Tuple)
                    and len(child.elts) == 3
                    and isinstance(child.elts[0], ast.Constant)
                    and isinstance(child.elts[0].value, str)
                ):
                    maintenance_names.append(child.elts[0].value)

    contract_marker_path = repo_root / "tests" / "test_contract_marker.py"
    contract_marker_tree = ast.parse(contract_marker_path.read_text(encoding="utf-8"))
    contract_files: list[str] = []
    for node in contract_marker_tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "CONTRACT_TEST_FILES":
                    contract_files = list(ast.literal_eval(node.value))

    return {
        "generation_contract": gen_contract,
        "generation_contract_sha256": sha256_file(gen_contract_path),
        "run_executor_manifest": exec_manifest,
        "run_executor_manifest_sha256": sha256_file(exec_manifest_path),
        "maintenance_job_names": maintenance_names,
        "maintenance_job_count": len(maintenance_names),
        "contract_test_files": contract_files,
    }


def evaluate_candidate_modules(graph: dict[str, Any], repo_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    modules = graph["modules"]
    candidates = [
        {
            "module_id": "creative",
            "display_name": "Creative Studio (V1 + V2)",
            "plane": "Domain + Execution adapter",
            "storage_mode": "Filesystem JSON manifests + immutable media (`~/.ttakkari/data/creative*`, `studio_lab_root`); 0 dedicated PostgreSQL tables (uses `runs` only for optional V2 intent extraction)",
            "external_dep_reverse_modules": [
                m for m in modules.get("creative", {}).get("depended_by", []) if m not in {"api", "orchestrator"}
            ],
            "test_files_count": len(list((repo_root / "tests").glob("*creative*.py")))
            + len(list((repo_root / "unit_tests").glob("*creative*.py"))),
            "recent_deploy_blocker_incidents": [
                "794447f3 (063002a1): LOG-194 test_creative_v2_lease_handoff failed after 985.34s",
                "d4f439d6 (81a95a50): LOG-190 test_creative_records_api failed after 696.95s",
                "bf9a853d (a2a0ad9c): LOG-186 test_creative_v2_auto_assets_api failed after 197.81s",
            ],
            "runtime_hazards_found": [
                "api/creative_v2.py:418 spawns untracked asyncio.create_task(_run_asset_generation) outside generation.work()",
                "creative/v2/asset_generation_managed.py:478 calls tts.service.budget() which fails if API slot enters draining during ComfyUI run",
                "creative/render.py:576 runs inside API generation maintenance_jobs('creative-render') every 2s",
            ],
            "first_module_suitability": "rank_1_recommended",
        },
        {
            "module_id": "games",
            "display_name": "Game Studio",
            "plane": "Domain + Execution adapter (Docker/OrbStack)",
            "storage_mode": "PostgreSQL tables (`game_projects`, `game_builds`, `game_sessions`, etc.) + git repos (`~/development/projects/games`) + `~/.ttakkari/data/game-builds`",
            "external_dep_reverse_modules": [
                m for m in modules.get("games", {}).get("depended_by", []) if m not in {"api", "orchestrator"}
            ],
            "test_files_count": len(list((repo_root / "tests").glob("*game*.py")))
            + len(list((repo_root / "unit_tests").glob("*game*.py"))),
            "recent_deploy_blocker_incidents": [
                "5693d3aa (a7f89304): LOG-185 test_neon_survivor_progression_balance failed after 1004.55s",
            ],
            "runtime_hazards_found": [
                "Coupled to `api/orchestrator.py:_game_worker_context` and `runs/manager.py` auto-build/auto-repair hooks",
            ],
            "first_module_suitability": "rank_2_after_creative",
        },
        {
            "module_id": "collector",
            "display_name": "Collector Platform",
            "plane": "Domain + Execution worker",
            "storage_mode": "PostgreSQL tables (`collector_*`, 0060/0061/0063 migrations) + blob store (`~/.ttakkari/data/collector`)",
            "external_dep_reverse_modules": [
                m for m in modules.get("collector", {}).get("depended_by", []) if m not in {"api", "orchestrator"}
            ],
            "test_files_count": len(list((repo_root / "tests").glob("*collector*.py")))
            + len(list((repo_root / "unit_tests").glob("*collector*.py"))),
            "recent_deploy_blocker_incidents": [
                "166996a7 (981149ea): LOG-191 test_collector_fetch_tls failed after 690.51s",
            ],
            "runtime_hazards_found": [
                "Heavy dirty state in shared `/back` worktree; cross-coupled with `kb` (`knowledge_rag`) and `generation_service.py` (`collector-consume`, `collector-control`)",
            ],
            "first_module_suitability": "defer_until_p5",
        },
        {
            "module_id": "tts",
            "display_name": "Local TTS (Supertonic)",
            "plane": "Execution capability / media runtime",
            "storage_mode": "PostgreSQL `tts_jobs` metadata + process-local `_pending_texts` in RAM + disk audio cache",
            "external_dep_reverse_modules": [
                m for m in modules.get("tts", {}).get("depended_by", []) if m not in {"api", "orchestrator"}
            ],
            "test_files_count": len(list((repo_root / "tests").glob("*tts*.py")))
            + len(list((repo_root / "unit_tests").glob("*tts*.py"))),
            "recent_deploy_blocker_incidents": [],
            "runtime_hazards_found": [
                "tts/jobs.py:42 stores text chunks only in process-local `_pending_texts: dict[UUID, list[str]]`",
                "tts/jobs.py:154 `_fail_queued_draining` actively fails accepted 202 queued jobs with `generation_draining` when slot drains",
                "tts/service.py:130 `generation.work('tts')` rejects work in draining state, preventing queued jobs from draining",
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


def compute_evidence_cache_key_preview(
    *,
    module_id: str,
    changed_files: list[str],
    dependency_closure_hashes: dict[str, str],
    uv_lock_sha256: str,
    migration_head: str,
    generation_contract_sha256: str,
    suite_selector: str,
    python_version: str = "3.13",
) -> dict[str, Any]:
    """Compute a deterministic EvidenceCacheKey for module-scoped validation."""
    payload = {
        "schema_version": "platform-v2-evidence-key-v1",
        "module_id": module_id,
        "changed_files": sorted(changed_files),
        "dependency_closure_hashes": dict(sorted(dependency_closure_hashes.items())),
        "uv_lock_sha256": uv_lock_sha256,
        "migration_head": migration_head,
        "generation_contract_sha256": generation_contract_sha256,
        "suite_selector": suite_selector,
        "python_version": python_version,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "cache_key": f"evk-v1-{module_id}-{digest[:24]}",
        "sha256": digest,
        "components": payload,
    }


def classify_change_impact(
    changed_files: list[str],
    graph: dict[str, Any],
) -> dict[str, Any]:
    """Classify a set of changed files into module-only, targeted multi-module, or fail-closed broad gate."""
    broad_gate_patterns = (
        "alembic/",
        "deploy/generation-contract.json",
        "deploy/run-executor-manifest.json",
        "pyproject.toml",
        "uv.lock",
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

    for mod in sorted(touched_modules):
        mod_info = graph["modules"].get(mod)
        if mod_info is None:
            reasons.append(f"unknown_module:{mod}")
            continue
        if mod_info["fan_in"] > 5:
            reasons.append(f"high_fan_in_module:{mod}(fan_in={mod_info['fan_in']})")

    if reasons:
        return {
            "gate_mode": "broad_fail_closed",
            "touched_modules": sorted(touched_modules),
            "impacted_reverse_modules": sorted(
                {
                    dep
                    for m in touched_modules
                    for dep in graph["modules"].get(m, {}).get("depended_by", [])
                }
            ),
            "reasons": reasons,
        }

    impacted_reverse = sorted(
        {
            dep
            for m in touched_modules
            for dep in graph["modules"].get(m, {}).get("depended_by", [])
        }
    )
    return {
        "gate_mode": "module_scoped" if len(touched_modules) == 1 else "multi_module_scoped",
        "touched_modules": sorted(touched_modules),
        "impacted_reverse_modules": impacted_reverse,
        "reasons": [],
    }


def build_evidence(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    graph = analyze_source_graph(repo_root / "src" / "ttakkari")
    migrations = analyze_migrations(repo_root)
    contracts = analyze_contracts_and_manifests(repo_root)
    candidates = evaluate_candidate_modules(graph, repo_root)

    uv_lock_sha = sha256_file(repo_root / "uv.lock")
    creative_hashes = {
        f"src/ttakkari/{rel}.py": sha256_file(repo_root / "src" / "ttakkari" / f"{rel}.py")
        for rel in sorted(graph["modules"]["creative"]["depends_on"])
        if (repo_root / "src" / "ttakkari" / f"{rel}.py").is_file()
    }
    sample_cache_key = compute_evidence_cache_key_preview(
        module_id="creative",
        changed_files=["src/ttakkari/creative/render.py"],
        dependency_closure_hashes=creative_hashes,
        uv_lock_sha256=uv_lock_sha,
        migration_head=migrations["heads"][0] if migrations["heads"] else "unknown",
        generation_contract_sha256=contracts["generation_contract_sha256"],
        suite_selector="pytest -m contract + tests/test_creative*.py + unit_tests/test_creative*.py",
    )

    return {
        "schema_version": "platform-v2-worker-a-evidence-v1",
        "generated_at": "2026-10-24T21:33:00+09:00",
        "worker": {
            "id": "A",
            "role": "runtime / delivery / architecture",
            "worktree": "/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a",
            "branch": "audit/platform-v2-a-runtime-delivery-20261024",
        },
        "git_revisions": {
            "audited_origin_main_sha": "c8f10156845d007ec6496a65d6d8417be19260bd",
            "live_deployed_sha": "9f82c9c16cd604e81ddf43180b0be2e895ec85cb",
            "retired_previous_sha": "4f341c54813830c59fc0dd2ab41a8d37c811df52",
            "supervisor_pinned_release_sha": "e817fc19471bdc48054158cada4a99466f7eaa6f",
            "shared_back_worktree_head_sha": "607f8bda0df7dbdd458e4f0e54d703ce9edd3def",
            "commits_between_shared_head_and_origin_main": 111,
            "commits_between_live_and_origin_main": 4,
            "front_origin_main_sha": "dbe0c9f042b006e2a627b8e59c295ad14b7a2caf",
            "ui_worker_session_verified": {
                "session_id": "6e33860e-3773-48bf-8df5-a3dc2c330a3e",
                "title": "Worker: 메인 화면 카드 겹침·잘림 전수 수정 및 재발 방지",
                "repo_touched": "/Users/winterholic/development/projects/winterholic-ttakkari/front",
                "commit_sha": "dbe0c9f042b006e2a627b8e59c295ad14b7a2caf",
                "backend_side_effects": "none",
            },
        },
        "p0_operational_baseline": {
            "active_deployment": {
                "job_id": "4a6e09a8-c49f-4c2f-9a6c-c0d83fe5824a",
                "status": "succeeded",
                "strategy": "generations",
                "activated_revision": "9f82c9c16cd604e81ddf43180b0be2e895ec85cb",
                "release_path": "/Users/winterholic/.ttakkari/deploy/releases/9f82c9c16cd604e81ddf43180b0be2e895ec85cb",
                "candidate_slot": 1,
                "candidate_port": 8792,
                "candidate_generation": "5ef462e5-d7f7-40e7-bf27-1d4bf19e1d91",
                "previous_slot": 0,
                "previous_port": 8791,
                "retired_generation": "e1eb4ad0-5ce7-415f-b91e-9c0b0eab2ba3",
                "created_at": "2026-10-23T10:16:49.556741+00:00",
                "updated_at": "2026-10-23T10:43:06.032849+00:00",
                "wall_clock_seconds": 1576.48,
                "pytest_wall_seconds": 1045.97,
                "pytest_passed": 4473,
                "pytest_skipped": 8,
                "pytest_xfailed": 2,
                "pytest_subtests_passed": 5020,
                "db_selective_resets": 2822,
                "db_reset_tables": 106,
            },
            "latest_blocked_deployments": [
                {
                    "job_id": "e15536bc-c7f9-4e1a-89cb-95e2f91192e5",
                    "target_sha": "c8f10156845d007ec6496a65d6d8417be19260bd",
                    "status": "failed",
                    "failing_stage": "pytest",
                    "issue_id": "LOG-195",
                    "pytest_seconds_before_abort": 172.23,
                    "passed_before_abort": 1112,
                    "failed_test": "tests/test_desk_qwen_service.py::test_desk_request_goes_to_the_managed_server_of_this_process",
                    "cause": "AssertionError: assert 'harness-v3-topic-boundary' == 'harness-v2'",
                },
                {
                    "job_id": "794447f3-8635-443d-b4c5-de975b0e91fb",
                    "target_sha": "063002a14c21fc7d3fa668db66eb3e503f4cb5cc",
                    "status": "failed",
                    "failing_stage": "pytest",
                    "issue_id": "LOG-194",
                    "pytest_seconds_before_abort": 985.34,
                    "passed_before_abort": 3327,
                    "failed_test": "unit_tests/test_creative_v2_lease_handoff.py::test_scene_generation_requests_bounded_lease_wait_from_controller",
                    "cause": "AssertionError: assert 349 == 324 (prompt length changed by caption word-wrapping fix)",
                },
            ],
            "deploy_history_window": {
                "period_utc": "2026-10-16T08:11:13Z..2026-10-23T17:19:03Z",
                "deploy_issue_groups_total": 34,
                "deploy_failure_events_total": 56,
                "by_stage": {
                    "deploy:pytest": {"issue_groups": 33, "events": 54},
                    "deploy:archive": {"issue_groups": 1, "events": 2},
                },
                "sampled_job_counts": {
                    "succeeded_recent_cap": 50,
                    "failed_recent_cap": 50,
                    "superseded_recent": 40,
                    "draining_active": 0,
                    "waiting_slot_active": 0,
                },
            },
            "host_resources": {
                "sampled_at": "2026-10-24T12:20:12.690830+00:00",
                "chip": "Apple M5 Max",
                "cpu_logical_cores": 18,
                "gpu_cores": 40,
                "cpu_usage_percent": 17.6,
                "gpu_usage_percent": 0.0,
                "memory_total_bytes": 68719476736,
                "memory_used_percent": 42.1,
                "swap_total_bytes": 35745689856,
                "swap_used_bytes": 34059649024,
                "swap_used_percent": 95.3,
                "disk_used_percent": 77.3,
                "observed_backend_pid": 44104,
                "observed_llama_server_pid": 44129,
                "observed_llama_server_rss_bytes": 7734657024,
            },
            "backup_and_restore": {
                "backup_db_script": "scripts/backup_db.sh",
                "restore_rehearsal_script": "scripts/restore_rehearsal.sh",
                "snapshot_data_script": "scripts/snapshot_data.sh",
                "status_reader_module": "src/ttakkari/backup_status.py",
                "api_endpoint": "GET /api/system/backup-status",
                "code_status": "implemented",
                "live_latest_manifest_state": "unknown (확인 필요: ~/.ttakkari-backups/latest.json is outside workspace writable/readable roots and no MCP wrapper exposes /api/system/backup-status)",
            },
        },
        "subsystem_status_matrix": [
            {
                "subsystem": "ADR 0036 Standalone RunExecutor & Postgres Command Queue",
                "code_status": "implemented",
                "test_status": "tested (unit_tests/test_executor_migration_contract.py, unit_tests/test_executor_reconcile.py, tests/test_run_executor.py)",
                "config_contract_default": "feature-off (deploy/generation-contract.json run_executor.activation='disabled', Settings.run_executor_mode='local')",
                "live_status": "feature-off (code & migration 0064_run_executor deployed in 9f82c9c1, but active slot runs in execution_mode='local')",
                "evidence_paths": [
                    "src/ttakkari/executor.py",
                    "src/ttakkari/executor_reconcile.py",
                    "alembic/versions/0064_run_executor.py",
                    "deploy/generation-contract.json",
                    "deploy/run-executor-manifest.json",
                ],
            },
            {
                "subsystem": "ADR 0066 AI-Native Delivery Pipeline (ChangeSet / MergeQueue / Worktree)",
                "code_status": "implemented (partial: Stage 2 validator still runs full pytest; no daemon or MCP submit_change tool)",
                "test_status": "tested (tests/delivery/test_*.py)",
                "config_contract_default": "feature-off (Settings.delivery_pipeline_enabled=False)",
                "live_status": "feature-off (deployed in 9f82c9c1, not enabled in production)",
                "evidence_paths": [
                    "src/ttakkari/delivery_models.py",
                    "src/ttakkari/delivery/merge_queue.py",
                    "src/ttakkari/delivery/validation.py",
                    "src/ttakkari/delivery/worktree.py",
                    "src/ttakkari/delivery/provenance.py",
                    "src/ttakkari/delivery/diff_guard.py",
                    "src/ttakkari/delivery/repair_policy.py",
                    "alembic/versions/0062_delivery_change_sets.py",
                ],
            },
            {
                "subsystem": "3-Slot Generation Deployment (8791/8792/8793) & Cutover Controller",
                "code_status": "implemented",
                "test_status": "tested (tests/test_generation_deployment.py, unit_tests/test_deploy_stepwise_gate.py)",
                "config_contract_default": "enabled in prod (deploy_strategy='generations', runtime_generation=True)",
                "live_status": "live & measured (slot 1 port 8792 active on 9f82c9c1; supervisor running from pinned release e817fc19)",
                "evidence_paths": [
                    "src/ttakkari/generation.py",
                    "src/ttakkari/generation_service.py",
                    "src/ttakkari/generation_deployment.py",
                    "src/ttakkari/deployment.py",
                    "src/ttakkari/deploy_gate.py",
                ],
            },
            {
                "subsystem": "Local TTS Queue & Supertonic Provider",
                "code_status": "implemented (with process-local _pending_texts defect)",
                "test_status": "tested (tests/test_tts*.py)",
                "config_contract_default": "local_tts_enabled=True",
                "live_status": "live (process-local _pending_texts and _fail_queued_draining active in API process)",
                "evidence_paths": [
                    "src/ttakkari/tts/jobs.py",
                    "src/ttakkari/tts/service.py",
                ],
            },
            {
                "subsystem": "Desk Qwen Managed Server (Qwen3.5-4B llama-server)",
                "code_status": "implemented (per-API-generation child process ownership)",
                "test_status": "tested (tests/test_desk_qwen_service.py)",
                "config_contract_default": "desk_qwen_enabled=True, desk_qwen_manage=True, desk_qwen_base_url=''",
                "live_status": "live & measured (pid 44129 llama-server RSS 7.20 GiB)",
                "evidence_paths": [
                    "src/ttakkari/runs/desk_qwen_service.py",
                    "src/ttakkari/generation_service.py",
                ],
            },
            {
                "subsystem": "Exclusive Host GPU Resource Lease (G1)",
                "code_status": "implemented",
                "test_status": "tested (unit_tests/test_host_resource_lease.py, unit_tests/test_creative_v2_lease_handoff.py)",
                "config_contract_default": "enabled (locks data_dir/host-gpu/lock + data_dir/host-resource/lease.json)",
                "live_status": "live (deployed in 9f82c9c1)",
                "evidence_paths": [
                    "src/ttakkari/host_resource.py",
                    "src/ttakkari/host_gpu.py",
                    "src/ttakkari/managed_comfy.py",
                ],
            },
        ],
        "codebase_metrics": graph,
        "migrations": migrations,
        "contracts_and_manifests": contracts,
        "candidate_first_modules": candidates,
        "sample_evidence_cache_key": sample_cache_key,
        "sample_impact_classifications": {
            "creative_only_change": classify_change_impact(
                ["src/ttakkari/creative/render.py", "tests/test_creative_studio.py"],
                graph,
            ),
            "alembic_schema_change": classify_change_impact(
                ["alembic/versions/0064_run_executor.py"],
                graph,
            ),
            "high_fan_in_core_change": classify_change_impact(
                ["src/ttakkari/models.py"],
                graph,
            ),
        },
    }


def main() -> None:
    evidence = build_evidence(REPO_ROOT)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {OUTPUT_PATH} "
        f"(files={evidence['codebase_metrics']['total_python_files']}, "
        f"lines={evidence['codebase_metrics']['total_lines']}, "
        f"migrations={evidence['migrations']['migration_file_count']}, "
        f"single_head={evidence['migrations']['single_head']})"
    )


if __name__ == "__main__":
    main()
