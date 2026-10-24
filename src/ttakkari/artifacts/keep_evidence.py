"""내 보관함 보관(keep) 근거: 사용자가 실제로 한 말에서 인용을 찾아 근거로 남긴다.

사용자 메시지는 별도 테이블이 아니라 Run.prompt 로 저장된다. 근거가 될 수 있는 출처는 둘뿐이다.
- message: 이 세션에 사용자가 직접 보낸 메시지(origin=user Run 의 prompt 전체).
- run_prompt: 접수가 기획 세션에 넘긴 사용자 요청(origin=desk Run 의 prompt 에서 '[접수에서 넘긴 사용자 요청]' 머리글과
  '[최근 대화 — 참고만]' 이후를 뺀 사용자 발화 부분).
기획이 작업 세션에 쓴 지시(orchestrator)·작업 보고(report)·시스템 Run 은 사용자 발화가 아니므로 근거가 되지 않는다.
키워드로 의도를 추론하지 않는다. 인용이 실제 사용자 발화에 부분 문자열로 있는지만 확인한다.
"""
from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import Run, RunOrigin

# 이 Run(포함)과 그 이전 사용자·접수 Run 중 최근 몇 개에서 인용을 찾을지.
LOOKBACK_RUNS = 20
MIN_QUOTE_CHARS = 2
DESK_HEADER = "[접수에서 넘긴 사용자 요청]"
DESK_HISTORY_MARKER = "[최근 대화 — 참고만]"
EXCERPT_CHARS = 1000


@dataclass(frozen=True)
class QuoteMatch:
    run: Run
    source: str
    excerpt: str


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def user_utterance(run: Run) -> tuple[str, str] | None:
    """Run 에서 사용자가 실제로 한 말과 출처 이름. 사용자 발화가 아닌 Run 은 None."""
    if run.origin == RunOrigin.user:
        return run.prompt, "message"
    if run.origin == RunOrigin.desk:
        body = run.prompt.removeprefix(DESK_HEADER)
        # 첫 표지에서 자른다. 사용자 본문에 표지가 들어 있으면 그 앞까지만 인정하는 보수적인 방향이다.
        body = body.split(DESK_HISTORY_MARKER, 1)[0]
        return body, "run_prompt"
    return None


async def find_user_quote(db: AsyncSession, session_id: uuid.UUID, anchor: Run, quote: str) -> QuoteMatch | None:
    """anchor Run 이 속한 세션에서 quote 를 담은 사용자 발화를 찾는다. 같은 세션 밖은 보지 않는다."""
    needle = normalize(quote)
    if len(needle.replace(" ", "")) < MIN_QUOTE_CHARS:
        return None
    rows = await db.scalars(
        select(Run).where(
            Run.session_id == session_id, Run.origin.in_([RunOrigin.user, RunOrigin.desk]),
            Run.created_at <= anchor.created_at)
        .order_by(Run.created_at.desc(), Run.id.desc()).limit(LOOKBACK_RUNS))
    for run in rows:
        said = user_utterance(run)
        if said is None:
            continue
        text, source = said
        if needle in normalize(text):
            return QuoteMatch(run=run, source=source, excerpt=normalize(text)[:EXCERPT_CHARS])
    return None


def build_evidence(match: QuoteMatch, *, session_id: uuid.UUID, run: Run, quote: str, kept_at: datetime) -> dict[str, Any]:
    """keep_evidence JSON. message_id 는 사용자 발화를 담은 Run(= 저장된 사용자 메시지)의 ID 이다."""
    return {
        "session_id": str(session_id),
        "run_id": str(run.id),
        "message_id": str(match.run.id),
        "message_source": match.source,
        "message_origin": str(match.run.origin),
        "message_at": match.run.created_at.isoformat(),
        "quote": normalize(quote),
        "user_message": match.excerpt,
        "requested_at": match.run.created_at.isoformat(),
        "kept_at": kept_at.isoformat(),
    }
