"""게임 프로젝트·빌드 API (TASK-G06, docs/design/game-studio.md §9, ADR 0047).

인증된 사용자만 쓴다. 게임은 games_root/<slug> 의 기존 저장소를 따까리 Workspace 에 연결하는 것이고(임의 경로 입력 없음),
빌드는 격리 러너가 백그라운드로 수행하며 이 API 는 building 행을 만들어 202 로 돌려준다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.games import builds as game_builds
from ttakkari.games import create as game_create
from ttakkari.games import origin as game_origin
from ttakkari.games import worker as game_worker
from ttakkari.games.adapter import resolve_project_path
from ttakkari.games.contract import (
    GameContractError,
    approve_lock,
    decisions_sha256,
    game_dir,
    read_manifest,
    register_game_project,
    verify_lock,
)
from ttakkari.games.feedback import FeedbackIn
from ttakkari.models import GameBuild, GameProject, Run, Workspace
from ttakkari.runs.manager import _background
from ttakkari.security.auth import User, client_ip

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/games", tags=["games"])

CODE_STATUS = {
    "not_found": 404, "no_manifest": 404, "no_pms_project": 404, "no_run": 404, "no_build": 404, "no_workspace": 404,
    "exists": 409, "build_in_progress": 409, "lock_violation": 409, "snapshot_exists": 409, "snapshot_tampered": 409, "not_playable": 409,
    "origin_unconfigured": 503, "bad_request": 422, "bad_path": 422, "dirty_worktree": 409, "not_restorable": 409, "already_active": 409,
    "bad_commit": 422, "git_failed": 500,
    "outside_root": 403, "bad_slug": 422, "bad_manifest": 422, "wrong_engine": 422, "unsupported_engine": 422,
}


def _http(e: GameContractError) -> HTTPException:
    return HTTPException(CODE_STATUS.get(e.code, 400), {"code": e.code, "message": str(e)})


class GameRegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=3, max_length=40)
    pms_project_id: uuid.UUID | None = None
    create_pms: bool = True


Decision = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class GameCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idea: str = Field(min_length=1, max_length=2000)  # 사용자의 한 줄 아이디어. 첫 개발 요청의 본문이 된다
    name: str | None = Field(default=None, max_length=game_create.MAX_NAME_CHARS)  # 비우면 아이디어 첫 줄
    slug: str | None = Field(default=None, max_length=40)  # 비우면 이름에서 만든다(한글뿐이면 game-<무작위>)
    genre: str = Field(default="", max_length=60)
    engine: Literal["web-phaser"] = "web-phaser"
    locked_decisions: list[Decision] = Field(default_factory=list, max_length=20)  # 사용자가 직접 적은 결정. 생성과 함께 승인 해시로 고정된다
    start_development: bool = True  # true 면 같은 호출에서 기존 worker 경로로 첫 개발 요청을 접수한다
    create_pms: bool = True


class BuildStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID | None = None


class BuildOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    game_id: uuid.UUID
    seq: int
    status: Literal["building", "succeeded", "failed"]
    source_commit: str | None
    source_dirty: bool
    manifest_sha256: str | None
    lock_verified: bool
    bundle_sha256: str | None
    bundle_bytes: int | None
    bundle_files: int | None
    run_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    restored_from_id: uuid.UUID | None  # 롤백으로 소스를 복구해 만든 빌드이면 복구 대상이던 이전 빌드
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class BuildDetailOut(BuildOut):
    log_tail: str | None
    snapshot_verified: bool | None = None  # verify=true 로 요청했을 때만 채운다


class PlayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_id: uuid.UUID | None = None  # 비우면 활성 빌드


class PlayOut(BaseModel):
    url: str  # iframe src. 별도 origin 의 서명 토큰 경로이며 따까리 자격증명은 들어 있지 않다
    origin: str  # postMessage 의 event.origin 으로 비교할 게임 origin
    expires_at: datetime
    build_id: uuid.UUID
    build_seq: int
    bundle_sha256: str
    frame_ancestors: list[str]
    sandbox: str  # iframe sandbox 속성으로 쓸 값. allow-same-origin 은 일부러 없다


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["develop", "modify", "verify"] = "modify"
    instruction: str = Field(min_length=1, max_length=game_worker.MAX_INSTRUCTION_CHARS)
    build_id: uuid.UUID | None = None  # 비우면 가장 최근 성공 빌드(feedback 이 있으면 feedback.build_id)
    auto_build: bool = True  # develop·modify 가 성공하면 소스가 바뀐 경우 자동으로 빌드한다(ADR 0054). verify 는 항상 빌드하지 않는다
    feedback: FeedbackIn | None = None  # 스크린샷 Artifact 참조(원본·주석본)·정규화 영역·캡처 빌드. worker prompt 에 열 수 있는 파일 경로로 전달된다


class RequestOut(BaseModel):
    session_id: uuid.UUID  # 상태는 GET /api/sessions/{id}, GET /api/runs/{run_id} 와 MCP get_worker 로 읽는다
    run_id: uuid.UUID
    status: str
    kind: str
    build_id: uuid.UUID | None
    lock: str  # verified | tampered | unapproved | unreadable — 요청 시점의 잠금 결정 상태
    created_at: datetime


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_id: uuid.UUID  # 활성으로 삼을 성공 빌드. 이전 빌드로 되돌리는 것이 주 용도다
    restore_source: bool = False  # true 면 소스도 그 빌드의 commit 상태로 복구 커밋을 만들고 복구 빌드를 새로 만든다
    confirm: Literal[True]  # 되돌리기 어려운 동작이라 명시 확인(UI 의 최종 확인 대화상자)을 요구한다


class RollbackOut(BaseModel):
    game_id: uuid.UUID
    active_build_id: uuid.UUID
    previous_active_build_id: uuid.UUID | None
    restore_commit: str | None  # restore_source 일 때 소스가 놓인 commit(새 복구 커밋이거나 이미 같았다면 기존 HEAD)
    restore_build: BuildOut | None  # restore_source 일 때 백그라운드로 시작한 복구 빌드(202 처럼 building 상태)


class GameOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    engine_id: str
    genre: str = ""  # 저장소 매니페스트의 장르. 매니페스트를 읽을 수 없으면 빈 문자열
    workspace_id: uuid.UUID
    pms_project_id: uuid.UUID | None
    locked_revision: int
    active_build_id: uuid.UUID | None  # 관제탑이 여는 빌드(지정이 없으면 최근 성공 빌드)
    lock_approved: bool
    lock_matches_repo: bool | None  # 승인본과 저장소 매니페스트의 잠금 결정이 같은지. 매니페스트를 읽을 수 없으면 None
    locked_decisions: list[str]  # 저장소 매니페스트의 현재 값(승인본이 아닐 수 있다)
    created_at: datetime
    last_build: BuildOut | None = None


class GameCreateOut(BaseModel):
    game: GameOut
    request: RequestOut | None = None  # 첫 개발 요청. start_development=false 거나 접수에 실패하면 None
    request_error: dict | None = None  # 게임은 만들어졌지만 첫 요청 접수가 실패했을 때 {code, message}


async def _game(db: Db, game_id: uuid.UUID) -> GameProject:
    game = await db.get(GameProject, game_id)
    if game is None or game.archived_at is not None:
        raise HTTPException(404, {"code": "not_found", "message": "게임이 없습니다."})
    return game


def _game_out(game: GameProject, last: GameBuild | None) -> GameOut:
    decisions: list[str] = []
    matches: bool | None = None
    genre = ""
    try:
        manifest, _ = read_manifest(game_dir(game.slug))
        decisions = list(manifest.locked_decisions)
        genre = manifest.genre
        matches = verify_lock(game, manifest)
    except GameContractError:
        pass
    return GameOut(
        id=game.id, slug=game.slug, name=game.name, engine_id=game.engine_id, genre=genre, workspace_id=game.workspace_id,
        pms_project_id=game.pms_project_id, locked_revision=game.locked_revision, active_build_id=game.active_build_id,
        lock_approved=game.locked_decisions_sha256 is not None, lock_matches_repo=matches, locked_decisions=decisions,
        created_at=game.created_at, last_build=BuildOut.model_validate(last) if last else None)


async def _last_build(db: Db, game_id: uuid.UUID) -> GameBuild | None:
    return await db.scalar(select(GameBuild).where(GameBuild.game_id == game_id).order_by(GameBuild.seq.desc()).limit(1))


@router.get("", response_model=list[GameOut])
async def list_games(_: User, db: Db) -> list[GameOut]:
    games = (await db.scalars(select(GameProject).where(GameProject.archived_at.is_(None)).order_by(GameProject.created_at.desc()))).all()
    return [_game_out(g, await _last_build(db, g.id)) for g in games]


@router.post("", response_model=GameOut, status_code=201)
async def register_game(body: GameRegisterIn, _: User, db: Db, request: Request) -> GameOut:
    try:
        manifest, _sha = read_manifest(game_dir(body.slug))
        if manifest.id != body.slug:
            raise GameContractError("매니페스트 id 가 slug 와 다릅니다.", "bad_manifest")
        game = await register_game_project(db, manifest, create_pms=body.create_pms, pms_project_id=body.pms_project_id)
    except GameContractError as e:
        record(db, action="game.register", outcome="denied", target_type="game", detail={"slug": body.slug, "code": e.code}, ip=client_ip(request))
        await db.commit()
        raise _http(e) from e
    record(db, action="game.register", target_type="game", target_id=game.id, detail={"slug": game.slug}, ip=client_ip(request))
    await db.commit()
    return _game_out(game, None)


@router.post("/create", response_model=GameCreateOut, status_code=201)
async def create_game(body: GameCreateIn, _: User, db: Db, request: Request) -> GameCreateOut:
    """아이디어 한 줄에서 게임 저장소를 scaffold·등록하고 첫 개발 요청을 접수한다. 사용자 인증이 필요하고 잠금 결정은 이 호출로 승인된다."""
    ip = client_ip(request)
    try:
        game, approved = await game_create.create_game(
            db, idea=body.idea, name=body.name, slug=body.slug, genre=body.genre, decisions=list(body.locked_decisions),
            engine_id=body.engine, create_pms=body.create_pms)
    except GameContractError as e:
        await db.rollback()
        record(db, action="game.create", outcome="denied", target_type="game", detail={"code": e.code}, ip=ip)
        await db.commit()
        raise _http(e) from e
    except Exception:
        await db.rollback()
        raise
    record(db, action="game.create", target_type="game", target_id=game.id, ip=ip,
           detail={"slug": game.slug, "engine": game.engine_id, "decisions": len(body.locked_decisions)})
    if approved:
        record(db, action="game.lock.approve", target_type="game", target_id=game.id, ip=ip,
               detail={"revision": game.locked_revision, "via": "create"})
    await db.commit()
    out = GameCreateOut(game=_game_out(game, None))
    if body.start_development:
        try:
            _sess, run = await game_worker.start_request(
                db, game, kind="develop", ip=ip, auto_build=True, instruction=game_create.develop_instruction(body.idea, game.name, list(body.locked_decisions)))
            await db.refresh(run)
            record(db, action="game.request", target_type="run", target_id=run.id, ip=ip, detail={"game": game.slug, "kind": "develop"})
            await db.commit()
            out.request = _request_out(run)
        except GameContractError as e:
            await db.rollback()
            out.request_error = {"code": e.code, "message": str(e)}
        except Exception as e:  # 게임은 이미 만들어졌다. 첫 요청만 실패로 알리고 관제탑에서 다시 요청할 수 있게 한다
            log.exception("game %s: first develop request failed", game.slug)
            await db.rollback()
            out.request_error = {"code": "request_failed", "message": f"첫 개발 요청을 접수하지 못했습니다: {type(e).__name__}"}
    return out


@router.get("/{game_id}", response_model=GameOut)
async def get_game(game_id: uuid.UUID, _: User, db: Db) -> GameOut:
    game = await _game(db, game_id)
    return _game_out(game, await _last_build(db, game.id))


@router.post("/{game_id}/lock/approve", response_model=GameOut)
async def approve_game_lock(game_id: uuid.UUID, _: User, db: Db, request: Request) -> GameOut:
    """저장소 매니페스트의 현재 잠금 결정을 사용자가 승인해 DB 에 해시로 고정한다."""
    game = await _game(db, game_id)
    try:
        manifest, _sha = read_manifest(game_dir(game.slug))
        await approve_lock(db, game, manifest)
    except GameContractError as e:
        raise _http(e) from e
    record(db, action="game.lock.approve", target_type="game", target_id=game.id, ip=client_ip(request),
           detail={"revision": game.locked_revision, "sha256": decisions_sha256(manifest.locked_decisions)})
    await db.commit()
    return _game_out(game, await _last_build(db, game.id))


@router.get("/{game_id}/builds", response_model=list[BuildOut])
async def list_builds(game_id: uuid.UUID, _: User, db: Db, limit: int = Query(50, ge=1, le=200)) -> list[GameBuild]:
    game = await _game(db, game_id)
    return list(await db.scalars(select(GameBuild).where(GameBuild.game_id == game.id).order_by(GameBuild.seq.desc()).limit(limit)))


@router.get("/{game_id}/builds/{build_id}", response_model=BuildDetailOut)
async def get_build(game_id: uuid.UUID, build_id: uuid.UUID, _: User, db: Db, verify: bool = False) -> BuildDetailOut:
    game = await _game(db, game_id)
    build = await db.get(GameBuild, build_id)
    if build is None or build.game_id != game.id:
        raise HTTPException(404, {"code": "no_build", "message": "빌드가 없습니다."})
    out = BuildDetailOut.model_validate(build)
    if verify:
        out.snapshot_verified = await asyncio.to_thread(game_builds.verify_snapshot, build)
    return out


async def _run_build(plan: game_builds.BuildPlan) -> None:
    try:
        async with sessionmaker()() as db:
            await game_builds.execute_build(db, plan)
    except Exception:
        log.exception("game build %s failed unexpectedly", plan.build_id)  # 빌드 행은 execute_build 가 failed 로 닫았다


@router.post("/{game_id}/builds", response_model=BuildOut, status_code=202)
async def start_build(game_id: uuid.UUID, body: BuildStartIn, _: User, db: Db, request: Request) -> GameBuild:
    game = await _game(db, game_id)
    try:
        build, plan = await game_builds.prepare_build(db, game, run_id=body.run_id)
    except GameContractError as e:
        raise _http(e) from e
    record(db, action="game.build.start", target_type="game_build", target_id=build.id, ip=client_ip(request),
           detail={"game": game.slug, "seq": build.seq})
    await db.commit()
    task = asyncio.create_task(_run_build(plan))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return build


@router.post("/{game_id}/play", response_model=PlayOut)
async def play_game(game_id: uuid.UUID, body: PlayIn, _: User, db: Db, request: Request) -> PlayOut:
    """성공한 빌드를 별도 게임 origin 에서 열 단기 URL 을 발급한다. 번들 해시를 다시 확인한 빌드만 내준다."""
    problem = game_origin.origin_problem()
    if problem:
        raise HTTPException(503, {"code": "origin_unconfigured", "message": problem})
    game = await _game(db, game_id)
    if body.build_id is not None:
        build = await db.get(GameBuild, body.build_id)
        if build is None or build.game_id != game.id:
            raise HTTPException(404, {"code": "no_build", "message": "빌드가 없습니다."})
    else:
        build = await game_builds.effective_active_build(db, game)
        if build is None:
            raise HTTPException(409, {"code": "not_playable", "message": "플레이할 성공 빌드가 아직 없습니다."})
    if build.status != "succeeded" or not build.bundle_rel_path:
        raise HTTPException(409, {"code": "not_playable", "message": "성공한 빌드만 플레이할 수 있습니다."})
    if not await asyncio.to_thread(game_builds.verify_snapshot, build):
        record(db, action="game.play", outcome="denied", target_type="game_build", target_id=build.id, ip=client_ip(request),
               detail={"code": "snapshot_tampered"})
        await db.commit()
        raise HTTPException(409, {"code": "snapshot_tampered", "message": "빌드 스냅샷이 기록된 해시와 달라 열 수 없습니다."})
    ancestors = game_origin.frame_ancestors()
    caller = (request.headers.get("origin") or "").rstrip("/")
    token, exp = game_origin.issue_play_token(game.id, build.id, build.bundle_rel_path)
    record(db, action="game.play", target_type="game_build", target_id=build.id, ip=client_ip(request), detail={"game": game.slug, "seq": build.seq})
    await db.commit()
    return PlayOut(
        url=game_origin.play_url(token, caller if caller in ancestors else None), origin=get_settings().game_origin_url.rstrip("/"),
        expires_at=exp, build_id=build.id, build_seq=build.seq, bundle_sha256=build.bundle_sha256 or "", frame_ancestors=ancestors,
        sandbox=game_origin.SANDBOX_TOKENS)


def _request_out(run: Run) -> RequestOut:
    tag = (run.usage or {}).get(game_worker.USAGE_KEY) or {}
    return RequestOut(session_id=run.session_id, run_id=run.id, status=str(run.status), kind=tag.get("kind", ""),
                      build_id=tag.get("build_id"), lock=tag.get("lock", ""), created_at=run.created_at)


@router.post("/{game_id}/requests", response_model=RequestOut, status_code=202)
async def request_work(game_id: uuid.UUID, body: RequestIn, _: User, db: Db, request: Request) -> RequestOut:
    """게임 개발·수정·검증 요청을 기존 worker 세션·Run 으로 넘긴다. 전용 큐는 없고 모델·계정은 worker routing 이 고른다."""
    game = await _game(db, game_id)
    try:
        _sess, run = await game_worker.start_request(db, game, kind=body.kind, instruction=body.instruction, build_id=body.build_id,
                                                     feedback=body.feedback, ip=client_ip(request), auto_build=body.auto_build)
    except GameContractError as e:
        raise _http(e) from e
    await db.refresh(run)
    record(db, action="game.request", target_type="run", target_id=run.id, ip=client_ip(request), detail={"game": game.slug, "kind": body.kind})
    await db.commit()
    return _request_out(run)


@router.get("/{game_id}/requests", response_model=list[RequestOut])
async def list_requests(game_id: uuid.UUID, _: User, db: Db, limit: int = Query(30, ge=1, le=100)) -> list[RequestOut]:
    game = await _game(db, game_id)
    rows = await db.scalars(
        select(Run).where(Run.workspace_id == game.workspace_id, Run.usage["game"]["game_id"].astext == str(game.id))
        .order_by(Run.created_at.desc()).limit(limit))
    return [_request_out(r) for r in rows]


@router.post("/{game_id}/rollback", response_model=RollbackOut)
async def rollback_game(game_id: uuid.UUID, body: RollbackIn, _: User, db: Db, request: Request) -> RollbackOut:
    """활성 빌드를 선택한 성공 빌드로 바꾼다. 어떤 빌드 산출물도 수정·삭제하지 않고, 활성 포인터만 옮긴다.

    restore_source=true 면 저장소를 그 빌드의 commit 트리로 되돌리는 새 복구 커밋(fast-forward, 깨끗한 작업 트리에서만)을 만들고
    그 소스로 복구 빌드를 새로 시작한다. 승인된 잠금 결정과 다른 빌드는 어느 쪽이든 거부한다."""
    game = await _game(db, game_id)
    await db.execute(select(GameProject.id).where(GameProject.id == game.id).with_for_update())  # 동시 롤백·빌드 순번과 직렬화
    await db.refresh(game)
    target = await db.get(GameBuild, body.build_id)
    previous = game.active_build_id
    try:
        if target is None or target.game_id != game.id:
            raise GameContractError("이 게임의 빌드가 아닙니다.", "no_build")
        if body.restore_source is False and (await game_builds.effective_active_build(db, game) or target).id == target.id:
            raise GameContractError("이미 활성인 빌드입니다.", "already_active")
        await game_builds.check_activatable(db, game, target)
        restore_commit, new_build, plan = None, None, None
        if body.restore_source:
            await game_builds.assert_no_build_in_progress(db, game)
            ws = await db.get(Workspace, game.workspace_id)
            if ws is None:
                raise GameContractError("게임의 workspace 를 찾을 수 없습니다.", "no_workspace")
            restore_commit = await game_builds.restore_source(resolve_project_path(Path(ws.root_path)), target)
            game.active_build_id = target.id  # 소스를 되돌린 뒤 포인터를 옮기고, prepare_build 의 commit 에 함께 기록된다
            new_build, plan = await game_builds.prepare_build(db, game, restored_from=target.id)
        else:
            game.active_build_id = target.id
    except GameContractError as e:
        await db.rollback()
        record(db, action="game.rollback", outcome="denied", target_type="game_build", target_id=body.build_id, ip=client_ip(request),
               detail={"code": e.code, "restore_source": body.restore_source})
        await db.commit()
        raise _http(e) from e
    record(db, action="game.rollback", target_type="game", target_id=game.id, ip=client_ip(request),
           detail={"from": str(previous) if previous else None, "to": str(target.id), "seq": target.seq, "restore_source": body.restore_source,
                   "restore_commit": restore_commit, "restore_build": str(new_build.id) if new_build else None})
    await db.commit()
    if plan is not None:
        task = asyncio.create_task(_run_build(plan))
        _background.add(task)
        task.add_done_callback(_background.discard)
    return RollbackOut(game_id=game.id, active_build_id=target.id, previous_active_build_id=previous, restore_commit=restore_commit,
                       restore_build=BuildOut.model_validate(new_build) if new_build else None)
