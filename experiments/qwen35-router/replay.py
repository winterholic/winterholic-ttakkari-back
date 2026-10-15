"""Run the small synthetic pilot sequentially; it has no production inputs or side effects."""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

from ttakkari.experiments.qwen35_shadow import CandidateSnapshot, infer_once

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "synthetic-cases.jsonl"
RESULTS = ROOT / "pilot-results.json"


async def main() -> None:
    candidates = CandidateSnapshot(
        lead_ids=("synthetic-lead-1",), model_ids=(), tool_ids=(), permission_ids=()
    )
    observations: list[dict] = []
    latencies: list[float] = []
    for line in CASES.read_text().splitlines():
        case = json.loads(line)
        prompt = (
            f"[작업판]\n{case['context']['board']}\n[최근 대화]\n{case['context']['history']}"
            f"\n[사용자 메시지]\n{case['message']}"
        )
        for repeat in range(1, 4):
            started = time.perf_counter()
            row = {
                "case_id": case["case_id"],
                "repeat": repeat,
                "expected": case["expected"],
            }
            try:
                decision = await infer_once("http://127.0.0.1:18786", prompt, candidates)
                elapsed = (time.perf_counter() - started) * 1000
                latencies.append(elapsed)
                row |= {
                    "valid": True,
                    "action": decision.action,
                    "correct": decision.action == case["expected"],
                    "elapsed_ms": round(elapsed, 1),
                }
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                # Record failure without logging prompts or model text.
                elapsed = (time.perf_counter() - started) * 1000
                latencies.append(elapsed)
                row |= {
                    "valid": False,
                    "action": None,
                    "correct": False,
                    "elapsed_ms": round(elapsed, 1),
                    "failure_type": type(exc).__name__,
                    "failure_code": str(exc)[:100],
                }
            observations.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    sorted_ms = sorted(latencies)
    summary = {
        "kind": "synthetic_pilot_only",
        "n": len(observations),
        "valid": sum(x["valid"] for x in observations),
        "correct": sum(x["correct"] for x in observations),
        "pass3_cases": sum(
            all(x["correct"] for x in observations if x["case_id"] == case_id)
            for case_id in sorted({x["case_id"] for x in observations})
        ),
        "case_count": len({x["case_id"] for x in observations}),
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "latency_p95_ms": sorted_ms[max(0, int(len(sorted_ms) * 0.95 + 0.999) - 1)] if latencies else None,
        "observations": observations,
    }
    RESULTS.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print("SUMMARY " + json.dumps({k: v for k, v in summary.items() if k != "observations"}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
