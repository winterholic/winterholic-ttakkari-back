"""게임 프로젝트 개발·수정·검증 요청을 기존 세션·Run·worker routing 으로 넘긴다 (TASK-G08, docs/design/game-studio.md §6.1).

전용 큐를 두지 않는다. 요청 1건은 게임 workspace 의 worker ChatSession 과 그 Run 하나이고, 상태는 기존
`/api/sessions`·`/api/runs`·MCP get_worker 로 읽는다. 이 모듈은 self-contained prompt 를 만들고 Run 에 게임 연결 표식을 남기는 일만 한다.
잠금 결정은 prompt 로 알리지만 강제는 build/적용 쪽(builds.prepare_build, activation)이 승인 해시로 거부한다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.games.adapter import resolve_project_path
from ttakkari.games.contract import (
    MANIFEST_FILE,
    GameContractError,
    ProjectManifest,
    read_manifest,
    verify_lock,
)
from ttakkari.models import (
    ChatSession,
    GameBuild,
    GameProject,
    Run,
    RunOrigin,
    SessionKind,
    Workspace,
)

RequestKind = Literal["develop", "modify", "verify"]
# 라우팅은 기존 TaskType 만 쓴다(worker_routing.ROUTES). 개발·수정은 general, 독립 검증은 verification.
TASK_TYPE: dict[str, str] = {"develop": "general", "modify": "general", "verify": "verification"}
LockState = Literal["verified", "tampered", "unapproved", "unreadable"]
MAX_INSTRUCTION_CHARS = 20_000
USAGE_KEY = "game"


@dataclass
class GameRequestContext:
    game: GameProject
    workspace: Workspace
    manifest: ProjectManifest | None
    lock: LockState
    build: GameBuild | None


def lock_state(game: GameProject, manifest: ProjectManifest | None) -> LockState:
    if manifest is None:
        return "unreadable"
    if game.locked_decisions_sha256 is None:
        return "unapproved"
    return "verified" if verify_lock(game, manifest) else "tampered"


async def load_context(db: AsyncSession, game: GameProject, build_id: uuid.UUID | None = None) -> GameRequestContext:
    ws = await db.get(Workspace, game.workspace_id)
    if ws is None or ws.archived_at is not None:
        raise GameContractError("게임의 workspace 를 사용할 수 없습니다.", "no_workspace")
    root = resolve_project_path(Path(ws.root_path))
    if root.name != game.slug:
        raise GameContractError("workspace 경로가 게임 slug 와 다릅니다.", "bad_path")
    try:
        manifest, _ = read_manifest(root)
    except GameContractError:
        manifest = None
    build = None
    if build_id is not None:
        build = await db.get(GameBuild, build_id)
        if build is None or build.game_id != game.id:
            raise GameContractError("빌드를 찾을 수 없습니다.", "no_build")
    else:
        build = await db.scalar(select(GameBuild).where(GameBuild.game_id == game.id, GameBuild.status == "succeeded")
                                .order_by(GameBuild.seq.desc()).limit(1))
    return GameRequestContext(game, ws, manifest, lock_state(game, manifest), build)


def _lock_section(ctx: GameRequestContext) -> str:
    decisions = "\n".join(f"- {d}" for d in ctx.manifest.locked_decisions) if ctx.manifest and ctx.manifest.locked_decisions else "- (없음)"
    if ctx.lock == "verified":
        return (f"사용자가 승인한 잠금 결정(리비전 {ctx.game.locked_revision}). 어떤 작업도 이를 어기거나 바꾸면 안 된다:\n{decisions}")
    if ctx.lock == "unapproved":
        return f"아직 사용자가 승인하지 않은 초안 결정이다. 존중하되 {MANIFEST_FILE} 의 locked_decisions 는 고치지 않는다:\n{decisions}"
    if ctx.lock == "tampered":
        return (f"경고: 저장소의 {MANIFEST_FILE} locked_decisions 가 사용자가 승인한 내용과 다르다(현재 값은 신뢰할 수 없다). "
                f"먼저 `git diff -- {MANIFEST_FILE}` 로 변경을 확인하고 locked_decisions 를 승인된 내용으로 되돌려라"
                f"(`git checkout -- {MANIFEST_FILE}` 또는 이전 커밋 복원). 되돌리기 전에는 빌드와 적용이 거부된다. 현재 저장소의 값(참고용):\n{decisions}")
    return f"경고: {MANIFEST_FILE} 를 읽을 수 없다. 잠금 결정을 확인할 수 없으니 먼저 파일을 복구하고 결과에 보고하라."


def compose_prompt(ctx: GameRequestContext, kind: RequestKind, instruction: str) -> str:
    g, ws = ctx.game, ctx.workspace
    build = ("아직 성공한 빌드가 없다." if ctx.build is None else
             f"대상 빌드 v{ctx.build.seq} (id={ctx.build.id}, 상태={ctx.build.status}, source_commit={ctx.build.source_commit or '없음'}, "
             f"번들 sha256={(ctx.build.bundle_sha256 or '없음')[:16]}, 잠금 검증={'통과' if ctx.build.lock_verified else '미통과'}).")
    common = [
        f"[게임 프로젝트] {g.name} (slug={g.slug}, 엔진={g.engine_id}, game_id={g.id})",
        f"[저장소] {ws.root_path} — 이 디렉터리의 git 저장소가 소스 정본이다. 이 밖의 파일은 읽거나 쓰지 않는다.",
        f"[빌드] {build}",
        f"[잠금 결정]\n{_lock_section(ctx)}",
    ]
    rules = [
        f"{MANIFEST_FILE} 의 id·engine·locked_decisions 를 바꾸지 않는다. 사용자가 승인한 잠금 결정이 바뀌면 플랫폼이 빌드와 적용을 거부한다.",
        "호스트에서 npm install·빌드 스크립트를 직접 실행하지 않는다. 빌드는 따까리의 격리 러너가 수행하며 결과는 빌드 상태·로그로 확인한다.",
        "이미 만들어진 빌드 산출물(data_dir/game-builds)과 다른 게임·다른 저장소의 파일은 건드리지 않는다.",
        "에셋은 출처와 라이선스를 저장소 안 문서에 함께 기록한다. 출처를 확인할 수 없으면 쓰지 않는다.",
    ]
    if kind == "verify":
        task = ("독립 검증만 수행한다. 앞선 구현자의 결론을 믿지 말고 저장소의 실제 소스와 빌드 기록을 직접 대조한다. 소스는 고치지 않는다. "
                "결과는 통과/실패와 근거(확인한 파일·명령·출력)로 보고한다.")
    elif kind == "develop":
        task = "아래 요청대로 게임을 개발한다. 동작하는 소스를 저장소에 직접 작성하고 의미 있는 단위로 git commit 한다. 설계 문서만 쓰고 끝내지 않는다."
    else:
        task = "아래 요청대로 기존 게임을 수정한다. 변경은 요청 범위로 한정하고 의미 있는 단위로 git commit 한다. 기존 동작을 깨지 않았는지 소스와 빌드 기록으로 확인한다."
    return "\n\n".join([*common, "[작업 지침]\n" + task + "\n" + "\n".join(f"- {r}" for r in rules),
                        "[사용자 요청]\n" + instruction.strip()]) + "\n"


def run_tag(ctx: GameRequestContext, kind: RequestKind) -> dict:
    return {"game_id": str(ctx.game.id), "kind": kind, "build_id": str(ctx.build.id) if ctx.build else None, "lock": ctx.lock}


async def start_request(db: AsyncSession, game: GameProject, *, kind: RequestKind, instruction: str, build_id: uuid.UUID | None = None,
                        ip: str | None = None, origin: RunOrigin = RunOrigin.user) -> tuple[ChatSession, Run]:
    """게임 workspace 에 worker 세션을 만들어 기존 queue_run 으로 접수한다. 세션 engine 은 auto 이고 모델·계정은 worker routing 이 고른다."""
    from ttakkari.api.sessions import queue_run

    instruction = instruction.strip()
    if not instruction or len(instruction) > MAX_INSTRUCTION_CHARS:
        raise GameContractError(f"요청은 1~{MAX_INSTRUCTION_CHARS}자여야 합니다.", "bad_request")
    ctx = await load_context(db, game, build_id)
    sess = ChatSession(workspace_id=ctx.workspace.id, title=f"[{game.slug}] {instruction.splitlines()[0]}"[:80], engine="auto",
                       kind=SessionKind.worker, pool="personal", worker_task_type=TASK_TYPE[kind])
    db.add(sess)
    await db.flush()
    prompt = compose_prompt(ctx, kind, instruction)

    def tag(run: Run) -> None:
        run.usage = {**(run.usage or {}), USAGE_KEY: run_tag(ctx, kind)}

    run = await queue_run(db, sess, prompt, origin=origin, on_queued=tag, ip=ip)
    return sess, run
