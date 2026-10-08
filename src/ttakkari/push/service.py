"""Run 종료 알림. 구현은 Web Push 담당이 채운다. 실패해도 Run 기록에는 영향을 주지 않는다."""

from __future__ import annotations

from ttakkari.models import Run


async def notify_run_finished(run: Run, session_title: str | None) -> None:
    return None
