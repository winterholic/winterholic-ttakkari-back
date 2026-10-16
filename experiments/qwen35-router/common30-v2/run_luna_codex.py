"""Local helpers for the fixed Luna service benchmark.

Model execution must use the authenticated Ttakkari MCP benchmark API. This file intentionally does not
start Codex from an already sandboxed Run.
"""
from __future__ import annotations

import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPETITIONS = 3


def pct(xs, p):
    values = sorted(xs)
    return round(values[max(0, min(len(values) - 1, math.ceil(p * len(values)) - 1))], 2) if values else None


def expand_cases(cases, repetitions=REPETITIONS):
    return [{**case, "repetition": repetition}
            for repetition in range(1, repetitions + 1) for case in cases]


def summarize_instability(rows):
    grouped = {}
    for row in rows:
        if row.get("scored", True) and row.get("valid") and row.get("output"):
            action = row["output"].get("action")
            if action is not None:
                grouped.setdefault(row["case_id"], []).append(action)
    analyzed = sorted(cid for cid, actions in grouped.items() if len(actions) == REPETITIONS)
    unstable = sorted(cid for cid in analyzed if len(set(grouped[cid])) > 1)
    return {"analyzed_case_count": len(analyzed), "unstable_case_count": len(unstable),
            "unstable_case_ids": unstable}


def main():
    print("모델 실행은 승인된 MCP 도구 start_luna_common30_v2()로 시작하세요. "
          "결과는 luna_common30_v2_status(job_id)에서 받습니다.")


if __name__ == "__main__":
    main()
