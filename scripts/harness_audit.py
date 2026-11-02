#!/usr/bin/env python3
"""작업자 Run 하네스 지표를 운영 API에서 다시 집계한다(전후 비교용).

사용: scripts/harness_audit.py [--since 2026-10-19] [--ttk ttk]
`ttk get <경로>`가 owner API JSON을 돌려주는 명령이어야 한다(비밀번호·토큰은 이 스크립트가 다루지 않는다).
집계 항목은 docs/design/harness-audit-20261102.md 의 수치와 같은 정의다.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-19")
    ap.add_argument("--ttk", default="ttk")
    args = ap.parse_args()

    def get(path: str):
        out = subprocess.run([args.ttk, "get", path], capture_output=True, text=True).stdout
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
        return [dict(r, _sid=s["id"], _parent=s["parent_id"]) for r in get(f"/api/sessions/{s['id']}/runs")]

    with ThreadPoolExecutor(6) as pool:
        runs = [r for rs in pool.map(runs_of, workers) for r in rs]
    u = lambda r, k: (r.get("usage") or {}).get(k)  # noqa: E731

    print(f"since {args.since}: workers {len(workers)}, runs {len(runs)}")
    print("engine:", dict(collections.Counter(u(r, "selected_engine") or "?" for r in runs).most_common()))
    follow = len(runs) - len({r["_sid"] for r in runs})
    print(f"follow-up runs: {follow} ({follow / max(1, len(runs)):.0%})")

    ok = [r for r in runs if r["status"] == "succeeded"]
    comp = [u(r, "completion") for r in ok if u(r, "completion")]
    print(f"completion contract: {len(comp)}/{len(ok)} succeeded runs evaluated")
    if comp:
        c = collections.Counter("위반" if x.get("violation") else x.get("status") for x in comp)
        print("  ", dict(c), f"violation {c['위반'] / len(comp):.0%}")
    ver = [u(r, "false_completion") for r in runs if u(r, "false_completion") is not None]
    if ver:
        print(f"independently verified: {len(ver)}, false completion {sum(1 for v in ver if v) / len(ver):.0%}")

    prompts = [r["prompt"] for r in runs if r["origin"] == "orchestrator" and len(r["prompt"]) > 200
               and not r["prompt"].startswith("[")]
    if prompts:
        low = sum(1 for p in prompts if p.count(" ") / len(p) < 0.08)
        print(f"unspaced lead prompts: {low}/{len(prompts)} ({low / len(prompts):.0%})")
    per = collections.Counter(r["_parent"] for r in runs)
    if per:
        print("runs per lead: median", statistics.median(per.values()), "max", max(per.values()))


if __name__ == "__main__":
    main()
