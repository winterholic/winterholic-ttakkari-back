"""개발·수정 worker Run 이 성공하면 같은 게임을 자동으로 빌드한다 (TASK-G17, ADR 0054).

전용 큐·훅 표를 만들지 않는다. 요청 Run 의 usage.game 표식에 `auto_build` 가 있을 때만 RunManager 종료 지점이 이 함수를 띄운다.
빌드는 기존 prepare_build/execute_build 그대로이고 실패해도 Run 의 성공 여부를 바꾸지 않는다(빌드 행이 failed 로 남는다).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.games import builds
from ttakkari.games.adapter import resolve_project_path
from ttakkari.games.contract import GameContractError
from ttakkari.models import GameBuild, GameProject, Run, RunStatus, Workspace

log = logging.getLogger(__name__)
BUILD_KINDS = frozenset({"develop", "modify"})


async def should_build(db, game: GameProject, run: Run) -> bool:
    """성공한 develop/modify 요청이고 마지막 빌드 이후 소스가 바뀐 경우에만 빌드한다. 같은 소스를 다시 빌드하지 않는다."""
    tag = (run.usage or {}).get("game") or {}
    if run.status != RunStatus.succeeded or not tag.get("auto_build") or tag.get("kind") not in BUILD_KINDS:
        return False
    ws = await db.get(Workspace, game.workspace_id)
    if ws is None:
        return False
    head, dirty, _ = await builds.git_state(resolve_project_path(Path(ws.root_path)))
    last = await db.scalar(select(GameBuild).where(GameBuild.game_id == game.id, GameBuild.status.in_(("succeeded", "building")))
                           .order_by(GameBuild.seq.desc()).limit(1))
    return last is None or dirty or head != last.source_commit


async def after_run(run_id: uuid.UUID) -> GameBuild | None:
    """Run 종료 뒤 호출. 이유와 상관없이 예외를 밖으로 내지 않는다(Run 종료 처리와 무관해야 한다)."""
    try:
        async with sessionmaker()() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return None
            gid = ((run.usage or {}).get("game") or {}).get("game_id")
            game = await db.get(GameProject, uuid.UUID(gid)) if gid else None
            if game is None or game.archived_at is not None or not await should_build(db, game, run):
                return None
            try:
                _, plan = await builds.prepare_build(db, game, run_id=run.id)
            except GameContractError as e:  # lock_violation·build_in_progress 등. 사용자가 관제탑에서 사유를 보고 직접 다시 시작한다
                log.info("game %s: auto build skipped (%s)", game.slug, e.code)
                return None
        async with sessionmaker()() as db:
            return await builds.execute_build(db, plan)
    except Exception:
        log.exception("game auto build after run %s failed", run_id)
        return None
