"""Append-only attempt ledger (spec sections 6.4 and 5.2).

Every attempt row is kept: retries, zero-second rows, API errors, timeouts, truncations, UNKNOWN.
Nothing is deleted or merged. Summaries never pool different profiles and never replace the
all-attempt cost by a success-only mean.
"""

import json
from collections import defaultdict
from pathlib import Path

from . import codes

REQUIRED_NON_MISSING = (
    "server_run_id",
    "server_instance_id",
    "model_artifact_sha256",
    "engine_build",
    "config_digest",
    "base_tree_sha",
    "file_sha256_at_start",
    "per_call_http_status",
)


def _blank(value) -> bool:
    return value is None or value in ("", [], {})


def normalise(record: dict) -> dict:
    """Return the ledger form of a record: identity gaps and zero-wall rows are flagged, not dropped."""
    rec = dict(record)
    flags = list(rec.get("flags", []))
    missing = [f for f in REQUIRED_NON_MISSING if _blank(rec.get(f))]
    rec.setdefault("comparable", True)
    if missing:
        rec["comparable"] = False
        rec["unknown_reasons"] = [f"missing:{name}" for name in missing]
        if rec.get("verdict") in (codes.PASS, codes.FAIL):
            rec["evaluator_verdict"] = rec["verdict"]
            rec["evaluator_fail_code"] = rec.get("fail_code")
            rec["verdict"] = codes.UNKNOWN
            rec["fail_code"] = "UNKNOWN"
    if rec.get("wall_s") == 0 and "zero_wall_no_response" not in flags:
        flags.append("zero_wall_no_response")
    rec["flags"] = sorted(set(flags))
    return rec


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def append(self, record: dict) -> dict:
        rec = normalise(record)
        attempt_id = rec.get("attempt_id")
        if _blank(attempt_id):
            raise ValueError("attempt_id is required")
        if any(r["attempt_id"] == attempt_id for r in self.rows()):
            raise ValueError(
                f"duplicate attempt_id {attempt_id}: ledger rows are immutable"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
        return rec


def summarize(
    rows: list[dict],
    batch_elapsed: dict[str, float] | None = None,
    require_comparable: bool = True,
) -> dict[str, dict]:
    """Per-profile totals. A task x repeat key counts once; retries add cost, not completions."""
    by_profile: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_profile[row["profile_id"]].append(row)
    out = {}
    for profile, group in sorted(by_profile.items()):
        keys: dict[tuple, list[dict]] = defaultdict(list)
        for row in group:
            keys[(row["task_id"], row["repeat"])].append(row)
        wall_all = sum(float(r.get("wall_s") or 0.0) for r in group)
        ok_rows = [
            r
            for r in group
            if r.get("verdict") == codes.PASS
            and (r.get("comparable", True) or not require_comparable)
        ]
        successes = {k for k, rs in keys.items() if any(r in ok_rows for r in rs)}
        batch_ids = {r.get("batch_id") for r in group if r.get("batch_id")}
        elapsed = None
        if batch_elapsed and batch_ids and batch_ids <= set(batch_elapsed):
            elapsed = sum(batch_elapsed[b] for b in batch_ids)
        success_mean = (
            sum(float(r.get("wall_s") or 0.0) for r in ok_rows) / len(ok_rows)
            if ok_rows
            else None
        )
        out[profile] = {
            "attempt_rows": len(group),
            "unique_task_repeat_keys": len(keys),
            "unique_successes": len(successes),
            "extra_retry_rows": len(group) - len(keys),
            "wall_all_attempts_s": round(wall_all, 3),
            "cost_all_attempt_wall_per_success_s": (
                round(wall_all / len(successes), 3) if successes else None
            ),
            "cost_batch_elapsed_per_success_s": (
                round(elapsed / len(successes), 3)
                if (successes and elapsed is not None)
                else None
            ),
            "batch_elapsed_s": elapsed,
            "success_row_mean_wall_s_not_a_cost": (
                round(success_mean, 3) if success_mean is not None else None
            ),
            "unknown_rows": sum(r.get("verdict") == codes.UNKNOWN for r in group),
            "infra_rows": sum(r.get("verdict") == codes.INFRA for r in group),
            "fail_rows": sum(r.get("verdict") == codes.FAIL for r in group),
            "zero_wall_rows": sum(
                "zero_wall_no_response" in r.get("flags", []) or r.get("wall_s") == 0
                for r in group
            ),
            "non_comparable_rows": sum(not r.get("comparable", True) for r in group),
            "total_cost_note": (
                "no verified completion: report total cost, completion cost is N/A"
                if not successes
                else ""
            ),
        }
    return out
