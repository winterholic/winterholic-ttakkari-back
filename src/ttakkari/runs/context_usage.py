"""세션 컨텍스트 사용률. 계정 사용량(한도)과 다르다: 대화 하나가 모델 컨텍스트 창을 얼마나 채웠는지다.

메인 오케스트레이터는 이 값으로 작업 세션이나 자기 대화를 새로 시작할지 정한다(50% 검토, 70% 교체).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.agents.base import ParseState
from ttakkari.agents.codex import read_context
from ttakkari.config import get_settings
from ttakkari.models import TERMINAL_RUN_STATUSES, ChatSession, Run, utcnow

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
        stmt = stmt.where(Run.created_at > since)
    run = await db.scalar(stmt.order_by(Run.finished_at.desc().nulls_last(), Run.created_at.desc()).limit(1)
                          .execution_options(populate_existing=True))
    if run is None or not (run.usage or {}).get("context_percent"):
        return None
    return {k: run.usage[k] for k in KEYS}


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
    return f"{ctx['context_percent']}% ({ctx['context_tokens'] // 1000}k/{ctx['context_window'] // 1000}k) · {advice(ctx['context_percent'])}"
