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
    errors: list[Exception] = []

    for _ in range(max_passes):
        if not handles and not background:
            break

        handle_tasks = [handle.task for handle in list(handles.values())]
        for run_id in list(handles):
            try:
                await cancel_handle(run_id)
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - teardown must collect arbitrary task failures.
                errors.append(exc)
        if errors:
            for task in handle_tasks:
                if not task.done():
                    task.cancel()
        for task in handle_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - teardown must collect arbitrary task failures.
                errors.append(exc)

        background_tasks = list(background)
        if errors:
            # An unexpected failure makes teardown abortive: stop other registered work,
            # including tasks that could otherwise wait forever, and observe its result.
            for task in {*handle_tasks, *background_tasks}:
                if not task.done():
                    task.cancel()
        if background_tasks:
            if errors:
                background_done = set()
                background_pending = set(background_tasks)
            else:
                background_done, background_pending = await asyncio.wait(
                    background_tasks, return_when=asyncio.FIRST_EXCEPTION
                )
                for task in background_done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        errors.append(exc)

            if errors:
                for task in {*handle_tasks, *background_pending}:
                    if not task.done():
                        task.cancel()
                results = await asyncio.gather(*background_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception) and result not in errors:
                        errors.append(result)

        # Done callbacks remove registry entries and may enqueue the next dispatch.
        await asyncio.sleep(0)

        if errors:
            # A task's finally block may have registered more work while it was awaited.
            # Cancel that new work on the next bounded pass as well.
            continue

    if handles or background:
        raise AssertionError(
            "fixture teardown could not drain registered tasks after "
            f"{max_passes} passes: handles={list(handles)}, background={len(background)}"
        )
    if errors:
        raise errors[0]


def is_postgres_deadlock(exc: BaseException) -> bool:
    """Only PostgreSQL's deadlock SQLSTATE is retryable in fixture TRUNCATE cleanup."""
    return getattr(getattr(exc, "orig", None), "sqlstate", None) == "40P01"
