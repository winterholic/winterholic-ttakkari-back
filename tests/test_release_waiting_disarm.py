"""release_waiting with a generation runtime must also drop this process's armed wakers.

The executor arms a waker for every quota-waiting Run it sees. When release_waiting later
clears resume_after in the DB, dispatch() still treated the Run as armed and left it queued
until the original wake time (observed as a flaky idle-executor-restart test).
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from test_generations import generations

from ttakkari.db import sessionmaker
from ttakkari.generation import activate
from ttakkari.models import Run, utcnow
from ttakkari.runs.manager import manager


async def test_release_waiting_disarms_wakers_so_dispatch_starts_the_run(tmp_path, new_session, monkeypatch):
    ws, sess = await new_session()
    async with generations(tmp_path) as (a, _b):
        await activate(a.id, expected=None)
        monkeypatch.setattr(manager, "_runtime", a)
        started: list[uuid.UUID] = []
        monkeypatch.setattr(manager, "start", lambda run_id, lane="workers": started.append(run_id) or True)
        until = utcnow() + timedelta(hours=1)
        async with sessionmaker()() as db:
            run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                      prompt="waiting", engine="claude", resume_after=until)
            db.add(run)
            await db.commit()
            run_id = run.id
        try:
            manager._arm(run_id, run.session_id, until.timestamp())
            assert manager.is_waiting(run_id)
            assert await manager.dispatch(run.session_id) is None  # genuinely waiting

            await manager.release_waiting()

            assert not manager.is_waiting(run_id)
            assert await manager.dispatch(run.session_id) == run_id
            assert started == [run_id]
        finally:
            manager._disarm(run_id)
