"""개발·수정 worker Run 이 성공하면 같은 게임을 자동으로 빌드한다 (TASK-G17, ADR 0054).

전용 큐·훅 표를 만들지 않는다. 요청 Run 의 usage.game 표식에 `auto_build` 가 있을 때만 RunManager 종료 지점이 이 함수를 띄운다.
빌드는 기존 prepare_build/execute_build 그대로이고 실패해도 Run 의 성공 여부를 바꾸지 않는다(빌드 행이 failed 로 남는다).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.games import builds, isolation
from ttakkari.games import worker as game_worker
from ttakkari.games.adapter import resolve_project_path
from ttakkari.games.contract import GameContractError
from ttakkari.models import GameBuild, GameProject, Run, RunOrigin, RunStatus, Workspace

log = logging.getLogger(__name__)
BUILD_KINDS = frozenset({"develop", "modify"})


def _note(run: Run, **fields) -> None:
    """요청 기록(Run.usage.game)에 이 Run 이 끝난 뒤의 상태를 덧붙인다. 요청 시점 값(lock)은 바꾸지 않는다."""
    usage = dict(run.usage or {})
    usage["game"] = {**(usage.get("game") or {}), **fields}
    run.usage = usage


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


async def _repair_if_typecheck_failed(run_id: uuid.UUID, build: GameBuild) -> None:
    """타입 검사 실패는 worker 가 호스트에서 확인할 수 없는 오류라, 오류를 담은 수리 요청을 같은 게임에 자동으로 한 번 보낸다.

    수리 Run 도 자동 빌드를 하므로 반복되지 않게 repair_attempt 로 횟수를 제한한다. 접수에 실패해도 빌드 결과는 그대로다.
    """
    if build.status != "failed" or not (build.error or "").startswith(isolation.TYPECHECK_FAILED):
        return
    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        tag = ((run.usage or {}).get("game") or {}) if run else {}
        attempt = int(tag.get("repair_attempt") or 0)
        if attempt >= get_settings().game_auto_repair_attempts:
            log.info("game build %s: typecheck failed, repair attempts exhausted (%s)", build.id, attempt)
            return
        game = await db.get(GameProject, build.game_id)
        if game is None or game.archived_at is not None:
            return
        try:
            await game_worker.start_request(db, game, kind="modify", instruction=game_worker.REPAIR_INSTRUCTION, build_id=None,
                                            origin=RunOrigin.system, auto_build=True, repair_attempt=attempt + 1)
            await db.commit()
        except Exception:  # 접수 실패가 빌드 결과를 바꾸지 않는다
            await db.rollback()
            log.exception("game build %s: repair request failed", build.id)


async def after_run(run_id: uuid.UUID) -> GameBuild | None:
    """Run 종료 뒤 호출. 이유와 상관없이 예외를 밖으로 내지 않는다(Run 종료 처리와 무관해야 한다).

    게임 요청 Run 마다 종료 시점의 잠금 상태(lock_after)와 자동 빌드 결과(auto_build_result)를 요청 기록에 남긴다.
    관제탑이 "worker 가 승인된 잠금 결정을 바꿨다"와 "그래서 자동 빌드를 건너뛰었다"를 요청 단위로 보여 줄 근거다.
    """
    try:
        async with sessionmaker()() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return None
            gid = ((run.usage or {}).get("game") or {}).get("game_id")
            game = await db.get(GameProject, uuid.UUID(gid)) if gid else None
            if game is None or game.archived_at is not None:
                return None
            try:
                _note(run, lock_after=(await game_worker.load_context(db, game)).lock)
            except GameContractError:
                _note(run, lock_after="unreadable")
            await db.commit()
            if not await should_build(db, game, run):
                if ((run.usage or {}).get("game") or {}).get("auto_build"):
                    _note(run, auto_build_result={"status": "skipped", "code": "no_change" if run.status == RunStatus.succeeded else "run_not_succeeded"})
                    await db.commit()
                return None
            try:
                _, plan = await builds.prepare_build(db, game, run_id=run.id)
            except GameContractError as e:  # lock_violation·build_in_progress 등. 사용자가 관제탑에서 사유를 보고 직접 다시 시작한다
                log.info("game %s: auto build skipped (%s)", game.slug, e.code)
                _note(run, auto_build_result={"status": "skipped", "code": e.code, "message": str(e)[:300]})
                await db.commit()
                return None
        async with sessionmaker()() as db:
            build = await builds.execute_build(db, plan)
        await _record_build_result(run_id, build)
        await _repair_if_typecheck_failed(run_id, build)
        return build
    except Exception:
        log.exception("game auto build after run %s failed", run_id)
        return None


async def _record_build_result(run_id: uuid.UUID, build: GameBuild) -> None:
    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        if run is None:
            return
        failed = build.status != "succeeded"
        _note(run, auto_build_result={"status": build.status, "build_id": str(build.id), "seq": build.seq,
                                      **({"code": "typecheck_failed" if (build.error or "").startswith(isolation.TYPECHECK_FAILED) else "build_failed",
                                          "message": (build.error or "")[:300]} if failed else {})})
        await db.commit()
