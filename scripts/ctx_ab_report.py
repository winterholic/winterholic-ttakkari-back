#!/usr/bin/env python3
"""컨텍스트 개선 A/B 결과 보고(읽기 전용).

runs.usage의 세 arm 키별로 arm 간 비교 표를 낸다. 지표는 Run당 비캐시 입력,
도구 호출 수, 가짜완료 비율, 완료 계약 `부분` 비율, 실패·취소 비율, 표본 수다.
DB 접근은 harness_audit.py와 같은 방식(`ttk get` owner API JSON)이다.
arm당 표본 30 미만이면 "표본 부족"을 표시하고 결론을 내지 않는다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor

ARM_KEYS = ("code_map_arm", "session_memo_arm", "impact_check_arm")
MIN_SAMPLE = 30
EVENT_PAGE = 2000
EVENT_PAGES = 5  # Run당 이벤트 조회 상한. 초과분은 도구 호출 수에서 빠진다.


def noncached_input(usage: dict) -> int | None:
    """Run당 비캐시 입력. 엔진별 키가 달라 있으면 빼고 없으면 전체를 쓴다."""
    if not isinstance(usage.get("input_tokens"), (int, float)):
        return None
    cached = usage.get("cached_input_tokens", usage.get("cache_read_input_tokens", 0))
    if not isinstance(cached, (int, float)):
        cached = 0
    return int(usage["input_tokens"] - cached)


def summarize(runs: list[dict], arm_key: str) -> dict[str, dict]:
    """arm별 집계. verdict만이 결론 게이트이며 수치는 표본 수와 함께 둔다."""
    rows: dict[str, dict] = {}
    for arm in ("on", "control"):
        group = [r for r in runs if (r.get("usage") or {}).get(arm_key) == arm]
        inputs = [v for r in group if (v := noncached_input(r.get("usage") or {})) is not None]
        calls = [r["tool_calls"] for r in group if isinstance(r.get("tool_calls"), (int, float))]
        falses = [(r.get("usage") or {}).get("false_completion") for r in group]
        falses = [f for f in falses if f is not None]
        comps = [(r.get("usage") or {}).get("completion") for r in group]
        comps = [c for c in comps if c]
        failed = sum(1 for r in group if r.get("status") in ("failed", "cancelled"))
        rows[arm] = {"n": len(group)}
        if inputs:
            rows[arm]["mean_noncached_input"] = round(statistics.fmean(inputs), 1)
            rows[arm]["noncached_n"] = len(inputs)
        if calls:
            rows[arm]["mean_tool_calls"] = round(statistics.fmean(calls), 2)
            rows[arm]["tool_calls_n"] = len(calls)
        if falses:
            rows[arm]["false_completion_rate"] = round(sum(1 for f in falses if f) / len(falses), 3)
            rows[arm]["false_completion_n"] = len(falses)
        if comps:
            rows[arm]["partial_rate"] = round(
                sum(1 for c in comps if c.get("status") == "부분") / len(comps), 3)
            rows[arm]["partial_n"] = len(comps)
        rows[arm]["fail_cancel_rate"] = round(failed / len(group), 3) if group else 0.0
    for arm in ("on", "control"):
        other = "control" if arm == "on" else "on"
        if rows[arm]["n"] < MIN_SAMPLE:
            rows[arm]["verdict"] = "표본 부족"
        elif rows[other]["n"] < MIN_SAMPLE:
            rows[arm]["verdict"] = "대조군 표본 부족"
        else:
            rows[arm]["verdict"] = "비교 가능"
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-19")
    ap.add_argument("--ttk", default="ttk")
    args = ap.parse_args()

    def get(path: str):
        out = subprocess.run([args.ttk, "get", path], capture_output=True, text=True, check=False).stdout
        try:
            return json.loads(out)
        except ValueError:
            return []

    seen = {s["id"]: s for s in get("/api/sessions?include_archived=true&limit=200")}
    frontier = list(seen.values())
    while frontier:
        nxt = []
        for s in frontier:
            if s["kind"] in ("desk", "orchestrator"):
                for c in get(f"/api/sessions?parent_id={s['id']}&include_archived=true&limit=200"):
                    if c["id"] not in seen:
                        seen[c["id"]] = c
                        nxt.append(c)
        frontier = nxt
    workers = [s for s in seen.values() if s["kind"] == "worker" and s["created_at"] >= args.since]

    def runs_of(s):
        return [dict(r, _sid=s["id"]) for r in get(f"/api/sessions/{s['id']}/runs")]

    with ThreadPoolExecutor(6) as pool:
        runs = [r for rs in pool.map(runs_of, workers) for r in rs]

    def tool_calls(run: dict) -> int | None:
        if all((run.get("usage") or {}).get(k) not in ("on", "control") for k in ARM_KEYS):
            return None  # 실험 대상이 아니면 이벤트를 조회하지 않는다.
        total, after = 0, 0
        for _ in range(EVENT_PAGES):
            events = get(f"/api/runs/{run['id']}/events?limit={EVENT_PAGE}&after={after}") or []
            if not events:
                break
            total += sum(1 for e in events if e.get("type") == "tool.call")
            after = events[-1].get("seq", after)
            if len(events) < EVENT_PAGE:
                break
        return total

    with ThreadPoolExecutor(6) as pool:
        for run, calls in zip(runs, pool.map(tool_calls, runs)):
            run["tool_calls"] = calls

    print(f"since {args.since}: workers {len(workers)}, runs {len(runs)}")
    for key in ARM_KEYS:
        rows = summarize(runs, key)
        print(f"[{key}]")
        print("arm      n  비캐시입력평균  도구호출평균  가짜완료  부분  실패취소  판정")
        for arm in ("on", "control"):
            r = rows[arm]
            inp = f"{r['mean_noncached_input']}({r['noncached_n']})" if "mean_noncached_input" in r else "-"
            calls = f"{r['mean_tool_calls']}({r['tool_calls_n']})" if "mean_tool_calls" in r else "-"
            false = f"{r['false_completion_rate']:.0%}({r['false_completion_n']})" if "false_completion_rate" in r else "-"
            part = f"{r['partial_rate']:.0%}({r['partial_n']})" if "partial_rate" in r else "-"
            print(f"{arm:8}{r['n']:>3}  {inp:>12}  {calls:>10}  {false:>7}  {part:>5}  "
                  f"{r['fail_cancel_rate']:.0%}  {r['verdict']}")


if __name__ == "__main__":
    main()
