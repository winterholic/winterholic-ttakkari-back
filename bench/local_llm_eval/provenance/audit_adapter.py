"""Read the WBS 1.2 audit manifests into ledger-shaped rows (read-only, for cross-checking the ledger maths).

The audit CSVs live outside this repo (local-llm-worker-design worktree). The rows have no server or model
identity (the audit says so), so they are flagged non-comparable and summarised with
`require_comparable=False`; the point is to reproduce the audit's per-profile arithmetic, not to compare models.
"""

import csv
from pathlib import Path


def load_attempt_rows(attempt_manifest: Path) -> list[dict]:
    rows = []
    with attempt_manifest.open(encoding="utf-8-sig", newline="") as fh:
        for rec in csv.DictReader(fh):
            ok = rec["success_for_recount"] == "True"
            rows.append(
                {
                    "attempt_id": f"raw{int(rec['raw_line']):03d}",
                    "profile_id": rec["config"],
                    "task_id": rec["task"],
                    "repeat": int(rec["rep"]),
                    "verdict": "PASS" if ok else "FAIL",
                    "wall_s": float(rec["wall_s"] or 0.0),
                    "batch_id": rec["config"],
                    "comparable": False,
                }
            )
    return rows


def load_batch_elapsed(config_summary: Path) -> dict[str, float]:
    with config_summary.open(encoding="utf-8-sig", newline="") as fh:
        return {r["config"]: float(r["batch_wall_sum_s"]) for r in csv.DictReader(fh)}
