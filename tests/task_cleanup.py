"""Shared async fixture-task cleanup contract; deliberately independent of DB setup."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping, MutableSet
from typing import Any


async def drain_registered_tasks(
    handles: MutableMapping[Any, Any],
    background: MutableSet[asyncio.Task],
    cancel_handle: Callable[[Any], Awaitable[Any]],
    *,
    max_passes: int = 20,
) -> None:
    """Cancel and await registered Run/background tasks, or fail visibly if they keep respawning."""
    for _ in range(max_passes):
        if not handles and not background:
            return

        handle_tasks = [handle.task for handle in list(handles.values())]
        for run_id in list(handles):
            await cancel_handle(run_id)
        for task in handle_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        for task in list(background):
            try:
                await task
            except asyncio.CancelledError:
                pass

    if handles or background:
        raise AssertionError(
            "fixture teardown could not drain registered tasks after "
            f"{max_passes} passes: handles={list(handles)}, background={len(background)}"
        )


def is_postgres_deadlock(exc: BaseException) -> bool:
    """Only PostgreSQL's deadlock SQLSTATE is retryable in fixture TRUNCATE cleanup."""
    return getattr(getattr(exc, "orig", None), "sqlstate", None) == "40P01"
