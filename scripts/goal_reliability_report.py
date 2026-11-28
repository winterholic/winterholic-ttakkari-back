#!/usr/bin/env python3
"""목표 신뢰성 운영 관측(2차 안정화 §12) 집계. 읽기 전용이며 새 이벤트·테이블을 만들지 않는다.

기존 RunEvent(work.*, completion.*, run.finished.termination), AuditLog(work.*), Run.usage·cost_usd, 목표 계약(AppSetting)을
세기만 한다. 값이 없으면 0 이고 추정하지 않는다. 사용: `uv run python scripts/goal_reliability_report.py --hours 24`
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

EVENT_TYPES = ("work.continued", "work.no_progress", "work.blocked", "work.paused", "work.resumed", "work.dependency_waiting",
               "work.dependency_expired", "work.dependency_resumed", "work.cancel_isolated", "work.intake_failed",
               "completion.evidence_mismatch", "completion.closure")


async def collect(db, since) -> dict[str, Any]:
    from ttakkari.models import AppSetting, AuditLog, Run, RunEvent, utcnow
    from ttakkari.orchestrator import work
    from ttakkari.runs.termination import cancellation_stats

    out: dict[str, Any] = {"since": since.isoformat(), "generated_at": utcnow().isoformat()}
    # 1. 미완료 종료 실행 수: 성공이 아닌 종료, 성공했지만 상태 줄이 부분·막힘, 목표가 미완료라 후속이 붙은 Run(work.continued)
    by_status = dict((await db.execute(select(Run.status, func.count()).where(Run.created_at >= since)
                                       .group_by(Run.status))).all())
    partial = await db.scalar(select(func.count()).select_from(Run).where(
        Run.created_at >= since, Run.usage["completion"]["status"].as_string().in_(("부분", "막힘"))))
    events = Counter(dict((await db.execute(select(RunEvent.type, func.count()).where(
        RunEvent.created_at >= since, RunEvent.type.in_(EVENT_TYPES)).group_by(RunEvent.type))).all()))
    out["incomplete_runs"] = {"by_status": by_status, "completion_partial_or_blocked": partial or 0,
                              "ended_with_unfinished_goal(work.continued)": events["work.continued"]}
    # 3·4·10. 자동 이어가기, 무진전, 자동화가 만든 Run 과 그 비용
    out["events"] = {t: events[t] for t in EVENT_TYPES}
    blocked = (await db.execute(select(RunEvent.payload).where(RunEvent.created_at >= since, RunEvent.type == "work.blocked"))).scalars()
    out["blocked_by_cause"] = dict(Counter(str((p or {}).get("cause") or (p or {}).get("failure_kind") or "no_progress/other")
                                           for p in blocked))
    runs = list(await db.scalars(select(Run).where(Run.created_at >= since).limit(50000)))
    auto = [r for r in runs if isinstance((r.usage or {}).get("work_continuation"), dict) or (r.usage or {}).get("closure")]

    def spend(rows: list[Run]) -> dict[str, Any]:
        tokens = lambda key: sum(int((r.usage or {}).get(key) or 0) for r in rows)
        return {"runs": len(rows), "cost_usd": round(sum(r.cost_usd or 0.0 for r in rows), 4),
                "cost_null_runs": sum(1 for r in rows if r.cost_usd is None),
                "input_tokens": tokens("input_tokens"), "output_tokens": tokens("output_tokens")}
    out["automation_spend"] = {"continuation_and_closure_runs": spend(auto), "all_runs": spend(runs)}
    # 5. 증거 불일치 거부: 목표 완료 거부(AuditLog) + 작업자 완료 보고 불일치(RunEvent)
    rejected = await db.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.at >= since, AuditLog.action == "work.evidence_rejected"))
    out["evidence_rejections"] = {"goal_completion_rejected": rejected or 0, "worker_report_mismatch": events["completion.evidence_mismatch"]}
    # 7. 의존성 충족 후 재개 여부
    resumed: Counter[str] = Counter()
    for detail in (await db.execute(select(AuditLog.detail).where(AuditLog.at >= since, AuditLog.action == "work.dependency_resumed"))).scalars():
        resumed["resumed" if (detail or {}).get("resumed") else f"not_resumed:{(detail or {}).get('reason', '?')}"] += 1
    out["dependency_resume"] = dict(resumed)
    # 8. 종료 원인 미확인 비율(cancelled Run)
    out["cancellation"] = await cancellation_stats(db, since=since)
    # 2·6·9. 목표별 상태, 후속 미연결 목표, 수동 재지시
    goals: Counter[str] = Counter()
    unowned: list[str] = []
    manual = 0
    for row in (await db.scalars(select(AppSetting).where(AppSetting.key.like("lead-work:%")))).all():
        state = (row.value or {}).get("state")
        if not state:
            continue
        goals[state["status"]] += 1
        manual += len((row.value or {}).get("additional_requests") or [])
        if state["status"] == "active":
            sid = row.key.removeprefix("lead-work:")
            import uuid
            summary = await work.summary(db, uuid.UUID(sid))
            if summary and not summary["has_owner"] and summary["phase"] == "running":
                unowned.append(sid)
    out["goals"] = {"by_status": dict(goals), "active_without_owner": len(unowned), "active_without_owner_ids": unowned[:50]}
    out["manual_reinstruction"] = {"additional_requests_on_contracts": manual, "work_resumed_events": events["work.resumed"],
                                   "note": "work.resumed 의 by 가 user/desk 이면 사용자가 직접 재개한 것"}
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()
    from ttakkari.db import sessionmaker
    from ttakkari.models import utcnow
    async with sessionmaker()() as db:
        print(json.dumps(await collect(db, utcnow() - timedelta(hours=args.hours)), ensure_ascii=False, indent=2, default=str))
        await db.rollback()


if __name__ == "__main__":
    asyncio.run(main())
