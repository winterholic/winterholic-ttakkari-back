"""창작 감독의 LLM 호출 경로.

새 모델 클라이언트를 만들지 않고 기존 따까리 작업 세션 경로(ChatSession worker → Run → 라우팅)를 그대로 쓴다. 회의록 분석(meetings.pipeline)이
같은 방식이다. 같은 요청은 `request_key`로 멱등이므로 서버가 재시작돼도 같은 Run을 이어 받는다. 테스트는 `FixedResponseLlm`을 쓴다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Protocol

DIRECTOR_EFFORT = "high"
POLL_SECONDS = 1.0


class DirectorLlmError(RuntimeError):
    pass


class DirectorLlm(Protocol):
    async def complete(self, *, purpose: str, prompt: str, request_key: str) -> str: ...


def extract_json(text: str) -> object:
    """응답에서 ```json 블록 또는 첫 JSON 객체를 꺼낸다. 없으면 DirectorLlmError."""
    block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = block.group(1) if block else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise DirectorLlmError("응답에서 JSON 객체를 찾지 못했습니다.")
        candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except ValueError as exc:
        raise DirectorLlmError(f"응답 JSON을 해석하지 못했습니다: {exc}") from exc


class FixedResponseLlm:
    """고정 응답 fake. purpose별 응답 큐를 순서대로 소비하고, 받은 프롬프트를 기록한다."""

    def __init__(self, responses: dict[str, list[str] | str]) -> None:
        self.responses = {
            k: ([v] if isinstance(v, str) else list(v)) for k, v in responses.items()
        }
        self.calls: list[dict[str, str]] = []

    async def complete(self, *, purpose: str, prompt: str, request_key: str) -> str:
        self.calls.append(
            {"purpose": purpose, "prompt": prompt, "request_key": request_key}
        )
        queue = self.responses.get(purpose)
        if not queue:
            raise DirectorLlmError(f"{purpose}에 대한 고정 응답이 없습니다.")
        return queue.pop(0) if len(queue) > 1 else queue[0]


class SessionRunDirectorLlm:
    """기존 worker 세션·Run 경로로 감독 질문을 보내고 Run 결과 텍스트를 돌려준다."""

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep

    async def _start(self, purpose: str, prompt: str, digest: str):
        from sqlalchemy import select

        from ttakkari.api.sessions import queue_run
        from ttakkari.api.workspaces import ensure_default_workspace
        from ttakkari.db import sessionmaker
        from ttakkari.models import ChatSession, Run, SessionKind, utcnow

        async with sessionmaker()() as db:
            # 재시작 뒤 같은 질문이면 새 세션을 만들지 않고 이미 만든 Run을 이어 받는다.
            prior = await db.scalar(select(Run.id).where(Run.request_key == digest))
            if prior is not None:
                return prior
            workspace = await ensure_default_workspace(db)
            session = ChatSession(
                workspace_id=workspace.id,
                title=f"창작 감독 · {purpose}"[:300],
                engine="auto",
                kind=SessionKind.worker,
                effort=DIRECTOR_EFFORT,
                pool="personal",
                archived_at=utcnow(),
            )
            db.add(session)
            await db.flush()
            run = await queue_run(db, session, prompt, request_key=digest)
            return run.id

    async def complete(self, *, purpose: str, prompt: str, request_key: str) -> str:
        from ttakkari.config import get_settings
        from ttakkari.db import sessionmaker
        from ttakkari.models import TERMINAL_RUN_STATUSES, Run, RunStatus

        digest = hashlib.sha256(request_key.encode()).hexdigest()[:40]
        run_id = await self._start(purpose, prompt, digest)
        deadline = time.monotonic() + (
            self.timeout_seconds or get_settings().run_timeout_seconds + 600
        )
        while time.monotonic() < deadline:
            async with sessionmaker()() as db:
                found = await db.get(Run, run_id)
                if found is not None and found.status in TERMINAL_RUN_STATUSES:
                    if found.status != RunStatus.succeeded:
                        raise DirectorLlmError(
                            f"감독 Run이 {found.status}로 끝났습니다: {(found.error or '')[:300]}"
                        )
                    if not (found.result_text or "").strip():
                        raise DirectorLlmError("감독 Run이 빈 응답을 돌려줬습니다.")
                    return found.result_text
            await self._sleep(POLL_SECONDS)
        raise DirectorLlmError("감독 Run이 제한 시간 안에 끝나지 않았습니다.")
