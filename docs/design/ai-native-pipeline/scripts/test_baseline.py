"""pytest JUnit XML 여러 개(같은 커밋 반복 실행)에서 테스트 자산 기준값을 뽑는다.

사용: python test_baseline.py run1.xml [run2.xml ...] > baseline.json
- 테스트별 시간(실행 간 중앙값), 분포, 느린 상위, 파일별 합계
- 실행 간 결과가 다른 테스트 = flaky 후보(같은 SHA 기준)
"""
from __future__ import annotations

import json
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


def outcome(case: ET.Element) -> str:
    for tag in ("failure", "error", "skipped"):
        if case.find(tag) is not None:
            return tag
    return "passed"


def load(path: str) -> dict[str, tuple[str, float]]:
    rows = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        key = f"{case.get('classname')}::{case.get('name')}"
        rows[key] = (outcome(case), float(case.get("time") or 0))
    return rows


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, int(p * len(s)))], 3) if s else 0.0


def main(paths: list[str]) -> dict:
    runs = [load(p) for p in paths]
    keys = sorted(set().union(*runs))
    times = {k: statistics.median([r[k][1] for r in runs if k in r]) for k in keys}
    outcomes = {k: [r[k][0] if k in r else "missing" for r in runs] for k in keys}
    per_file: dict[str, float] = defaultdict(float)
    for k, t in times.items():
        parts = k.split("::")[0].split(".")
        # classname은 "tests.test_x" 또는 "tests.test_x.TestC" 이므로 test_ 로 시작하는 첫 조각까지가 모듈이다.
        end = next((i for i, p in enumerate(parts) if p.startswith("test_")), len(parts) - 1)
        per_file[".".join(parts[: end + 1])] += t
    vals = list(times.values())
    run_totals = [round(sum(t for _, t in r.values()), 1) for r in runs]
    return {
        "runs": paths,
        "tests": len(keys),
        "outcome_counts_per_run": [
            {o: sum(1 for v in r.values() if v[0] == o) for o in ("passed", "failure", "error", "skipped")}
            for r in runs
        ],
        "sum_of_test_seconds_per_run": run_totals,
        "distribution_seconds": {"p50": pct(vals, .5), "p90": pct(vals, .9), "p99": pct(vals, .99),
                                 "max": round(max(vals), 3) if vals else 0,
                                 "over_1s": sum(v > 1 for v in vals), "over_5s": sum(v > 5 for v in vals),
                                 "over_30s": sum(v > 30 for v in vals)},
        "slowest_tests": sorted(((round(t, 2), k) for k, t in times.items()), reverse=True)[:25],
        "slowest_modules": sorted(((round(t, 1), f) for f, t in per_file.items()), reverse=True)[:20],
        "always_failing": [k for k, o in outcomes.items() if all(x in ("failure", "error") for x in o)],
        "flaky_candidates": {k: o for k, o in outcomes.items() if len(set(o)) > 1},
    }


if __name__ == "__main__":
    json.dump(main(sys.argv[1:]), sys.stdout, ensure_ascii=False, indent=1)
