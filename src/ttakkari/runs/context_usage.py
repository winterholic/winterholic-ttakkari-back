"""세션 컨텍스트 사용률. 계정 사용량(한도)과 다르다: 대화 하나가 모델 컨텍스트 창을 얼마나 채웠는지다.

메인 오케스트레이터는 이 값으로 작업 세션이나 자기 대화를 새로 시작할지 정한다(50% 검토, 70% 교체).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.agents.base import ParseState
from ttakkari.agents.codex import read_context
from ttakkari.config import get_settings
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    ChatSession,
    Run,
    RunOrigin,
    SessionKind,
    utcnow,
)

KEYS = ("context_tokens", "context_window", "context_percent")
# sessions.engine_sessions 안에서 계정 id 와 겹치지 않는 키. 새 대화로 바꾼 시각을 둔다.
RESET_KEY = "_reset_at"


def reset_engine_sessions(sess: ChatSession) -> None:
    """다음 Run 부터 새 엔진 대화로 시작한다. 앞선 대화는 넘기지 않는다(맥락은 기록·recall 로 찾는다)."""
    sess.engine_sessions = {RESET_KEY: utcnow().isoformat()}
    sess.engine_session_id = None


def reset_at(sess: ChatSession | None) -> datetime | None:
    raw = (sess.engine_sessions or {}).get(RESET_KEY) if sess else None
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def measure(state: ParseState, engine: str, config_dir: Path | None) -> dict[str, Any]:
    tokens, window = state.context_tokens, state.context_window
    if engine == "codex" and state.engine_session_id:
        got = read_context(config_dir or Path.home() / ".codex", state.engine_session_id)
        if got:
            tokens, window = got
    if not tokens or not window:
        return {}
    return {"context_tokens": tokens, "context_window": window, "context_percent": round(tokens * 100 / window)}


async def latest(db: AsyncSession, sess: ChatSession) -> dict[str, Any] | None:
    """끝난 Run 중 가장 최근에 잰 값. 새 대화로 바꾼 뒤 끝난 Run 이 아직 없으면 None."""
    stmt = select(Run).where(Run.session_id == sess.id, Run.status.in_(TERMINAL_RUN_STATUSES))
    if (since := reset_at(sess)) is not None:
        stmt = stmt.where(Run.started_at >= since)
    run = await db.scalar(stmt.order_by(Run.finished_at.desc().nulls_last(), Run.created_at.desc()).limit(1)
                          .execution_options(populate_existing=True))
    if run is None or any((run.usage or {}).get(k) is None for k in KEYS):
        return None
    return {**{k: run.usage[k] for k in KEYS},
            "measured_at": run.finished_at.isoformat() if run.finished_at else None,
            "measured_run_id": str(run.id),
            "measured_engine": (run.usage or {}).get("selected_engine") or run.engine,
            "measured_model": (run.usage or {}).get("selected_model") or run.model,
            "measurement_status": "fresh" if run.finished_at and utcnow() - run.finished_at <= timedelta(hours=24) else "stale"}


async def prepare(db: AsyncSession, sess: ChatSession, run: Run) -> None:
    """Rotate only between Runs under the manager's serial ownership, never mid-attempt.

    The snapshot and reset are committed together by the caller before launching an
    engine. Recovered/usage-limited Runs keep their persisted snapshot and ownership.
    """
    if sess.kind not in (SessionKind.orchestrator, SessionKind.worker) or run.started_at is not None:
        return
    from ttakkari.runs import events

    ctx = await latest(db, sess)
    settings = get_settings()
    pct = ctx["context_percent"] if ctx else None
    action = ("unknown" if pct is None else "fresh" if pct >= settings.context_replace_percent
              else "review" if pct >= settings.context_review_percent else "reuse")
    decision = {"action": action, "measurement": ctx,
                "reason": {"unknown": "no_measurement", "fresh": "replace_threshold",
                           "review": "review_threshold", "reuse": "below_review_threshold"}[action]}
    usage = dict(run.usage or {})
    if action == "fresh":
        # Build the snapshot before setting the reset marker that hides old turns.
        snapshot = await _snapshot(db, sess, run)
        usage["context_handoff"] = snapshot
        reset_engine_sessions(sess)
        decision["reset_at"] = reset_at(sess).isoformat()
    usage["context_routing"] = decision
    run.usage = usage
    events.append(db, run, "context.routing", decision)


async def _snapshot(db: AsyncSession, sess: ChatSession, run: Run) -> dict[str, Any]:
    stmt = select(Run).where(Run.session_id == sess.id, Run.id != run.id,
                             Run.status.in_(TERMINAL_RUN_STATUSES))
    if (since := reset_at(sess)) is not None:
        stmt = stmt.where(Run.started_at >= since)
    recent = list(await db.scalars(stmt.order_by(Run.created_at.desc()).limit(4)))
    first = await db.scalar(stmt.where(Run.origin != RunOrigin.report).order_by(Run.created_at.asc()).limit(1))
    rows = {r.id: r for r in ([first] if first else []) + list(reversed(recent))}
    original = next(((r.usage or {}).get("context_handoff", {}).get("original_request")
                     for r in rows.values() if (r.usage or {}).get("context_handoff", {}).get("original_request")), None)
    if original is None and first is not None:
        original = {"run_id": str(first.id), "prompt": first.prompt}
    lines = ["[컨텍스트 교대 기록 — 이전 대화의 원문 발췌이며 새 지시가 아니다]",
             f"세션: {sess.id} · {sess.title}",
             "기존 실행이 끝난 뒤 새 문맥으로 전환했다. 완료한 작업을 반복하지 말고 현재 코드·변경·작업 세션부터 확인한다.",
             f"생략된 상세는 read_session(session_id='{sess.id}')·recall·work-history에서 확인한다.",
             "긴 필드는 중간을 자르지 않고 전체를 생략한다. 아래 기록만으로 미확인 검증을 완료로 판단하지 않는다."]
    budget = 16000
    goal_included = False
    if original:
        goal = original["prompt"]
        goal_included = len(goal) <= budget // 2
        lines.append(f"최초 목표 Run {original['run_id']}: " + (goal if goal_included else "전체 생략; 원본 확인 필요"))
        if goal_included:
            budget -= len(goal)
    lines.append("최근 진행(최신 Run부터; 예산 안에서 최신 결과 우선):")
    for r in reversed(list(rows.values())):
        lines.append(f"Run {r.id} · {r.status} · checkpoint={r.checkpoint_ref or '-'} · outbox={r.outbox_path or '-'}")
        # Keep prior rotation pointers, so a multi-generation task remains traceable.
        previous = (r.usage or {}).get("context_handoff") or {}
        if previous.get("source_run_ids"):
            lines.append("이 Run이 이어받은 기록: " + ", ".join(previous["source_run_ids"]))
        for label, value in (("이전 요청", r.prompt), ("이전 결과", r.result_text), ("오류", r.error)):
            if label == "이전 요청" and goal_included and str(r.id) == original["run_id"]:
                continue  # The original goal is already present above; reserve space for results.
            if not value:
                continue
            block = f"{label}: {value}"
            if len(block) <= budget:
                lines.append(block)
                budget -= len(block)
            else:
                lines.append(f"{label}: 전체 생략({len(value)}자); 원본 Run {r.id} 확인 필요")
    workers = list(await db.scalars(select(ChatSession).where(ChatSession.parent_id == sess.id,
                                                              ChatSession.archived_at.is_(None))))
    for worker in workers:
        lines.append(f"기존 작업 세션: {worker.id} · {worker.title} (현재 상태를 조회하고 재사용·교대를 판단)")
    return {"source_run_ids": [str(r.id) for r in rows.values()], "original_request": original,
            "created_at": utcnow().isoformat(),
            "text": "\n".join(lines) + "\n\n[새 지시]\n"}


def advice(pct: int) -> str:
    s = get_settings()
    if pct >= s.context_replace_percent:
        return "교체 기준 이상: 기록을 확인한 뒤 새 대화로 바꾼다"
    if pct >= s.context_review_percent:
        return "검토 기준 이상: 이어 갈지 새 대화로 바꿀지 판단한다"
    return "여유 있음"


def describe(ctx: dict[str, Any] | None) -> str:
    if not ctx:
        return "알 수 없음(아직 잰 값 없음)"
    text = f"{ctx['context_percent']}% ({ctx['context_tokens'] // 1000}k/{ctx['context_window'] // 1000}k) · {advice(ctx['context_percent'])}"
    if ctx.get("measured_at"):
        text += f" · 측정 {ctx['measured_at']} {ctx.get('measured_engine')}/{ctx.get('measured_model')}"
    if ctx.get("measurement_status") == "stale":
        text += " · 오래된 측정(현재 여유는 미확인)"
    return text
