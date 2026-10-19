"""아이디어 한 줄에서 게임 프로젝트를 만든다 (TASK-G15, docs/design/game-studio.md §8 Phase 3, ADR 0053).

흐름: slug 결정 → 매니페스트 → 엔진 어댑터 scaffold(게임 저장소 git 첫 커밋) → Workspace·PMS·game_projects 등록
→ 사용자가 적은 잠금 결정이 있으면 승인 해시로 고정 → (선택) 기존 worker 경로로 첫 개발 요청.
새 큐·새 표를 만들지 않는다. 등록 전 단계가 실패하면 이번 호출이 만든 디렉터리만 지운다.
"""

from __future__ import annotations

import re
import secrets
import shutil
import unicodedata
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.games.adapter import get_adapter, resolve_project_path
from ttakkari.games.contract import (
    SLUG_RE,
    GameContractError,
    ProjectManifest,
    approve_lock,
    game_dir,
    register_game_project,
)
from ttakkari.models import GameProject

DEFAULT_ENGINE = "web-phaser"
DEFAULT_VERSION = "0.1.0"
MAX_NAME_CHARS = 60
SLUG_BASE_MAX = 30


def derive_name(idea: str) -> str:
    """이름을 따로 받지 않았을 때 아이디어 첫 줄을 이름으로 쓴다."""
    first = " ".join(idea.strip().splitlines()[0].split()) if idea.strip() else ""
    return first[:MAX_NAME_CHARS].rstrip() or "새 게임"


def derive_slug(name: str) -> str:
    """이름에서 ASCII 글자·숫자만 남겨 slug 후보를 만든다. 한글처럼 남는 글자가 없으면 무작위 접미사만 쓴다."""
    ascii_text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:SLUG_BASE_MAX].strip("-")
    if len(base) < 3:
        return f"game-{secrets.token_hex(4)}"
    return base


async def slug_taken(db: AsyncSession, slug: str) -> bool:
    """DB 행(보관된 게임 포함)이나 디스크의 어떤 항목이든 있으면 이미 쓰는 slug 다."""
    if await db.scalar(select(GameProject.id).where(GameProject.slug == slug)) is not None:
        return True
    path = game_dir(slug)
    return path.exists() or path.is_symlink()


async def choose_slug(db: AsyncSession, name: str, requested: str | None) -> str:
    if requested:
        if not SLUG_RE.fullmatch(requested):
            raise GameContractError("게임 slug 형식이 올바르지 않습니다(소문자·숫자·하이픈 3~40자).", "bad_slug")
        if await slug_taken(db, requested):
            raise GameContractError("이미 쓰는 slug 입니다.", "exists")
        return requested
    base = derive_slug(name)
    for attempt in range(8):
        candidate = base if attempt == 0 else f"{base}-{secrets.token_hex(2)}"
        if SLUG_RE.fullmatch(candidate) and not await slug_taken(db, candidate):
            return candidate
    raise GameContractError("사용할 수 있는 slug 를 만들지 못했습니다. 이름을 바꿔 다시 시도하세요.", "exists")


def develop_instruction(idea: str, name: str, decisions: list[str]) -> str:
    """첫 개발 요청 문장. 템플릿 구조(bridge)를 지키라는 지침은 compose_prompt 가 아니라 여기서 게임 생성 맥락으로 덧붙인다."""
    lines = [
        f"새 게임 '{name}'을 처음부터 만든다. 사용자의 아이디어(한 줄):",
        f"> {idea.strip()}",
        "",
        "지침:",
        ("- 저장소는 scaffold 직후 상태다(src/main.ts 의 빈 MainScene, src/bridge.ts). bridge.ts 와 main.ts 의 installBridge 호출·"
         "render.preserveDrawingBuffer 설정은 관제탑의 캡처·일시정지·계측이 쓰므로 유지한다."),
        "- 점수·체력·위치·게임 상태 같은 규칙 값은 publishState() 로 게시해 자동 검증이 읽을 수 있게 한다.",
        ("- 모바일 세로 화면에서 터치로 플레이할 수 있어야 하고 데스크톱에서는 키보드로도 된다. "
         "시작→플레이→종료/재시작 흐름이 끝까지 돌아야 한다."),
        "- 에셋은 외부 파일을 받지 말고 코드로 그리거나 합성한다(출처 불명 파일 금지).",
        "- 마지막에 규칙 요약과 조작법을 저장소 README.md 에 적고 커밋한다.",
    ]
    if decisions:
        lines += ["", "사용자가 정한 잠금 결정(어기면 빌드가 거부된다):", *[f"- {d}" for d in decisions]]
    return "\n".join(lines)


async def create_game(
    db: AsyncSession, *, idea: str, name: str | None, slug: str | None, genre: str, decisions: list[str],
    engine_id: str = DEFAULT_ENGINE, create_pms: bool = True, scaffold_adapter=None,
) -> tuple[GameProject, bool]:
    """게임 저장소를 scaffold 하고 등록한다. 돌려주는 bool 은 잠금 결정을 승인 해시로 고정했는지다.

    호출자가 db.commit 한다. 이 함수가 실패하면 만든 저장소 디렉터리를 지우고 DB 변경은 flush 만 된 상태라 호출자가 rollback 한다.
    """
    idea = idea.strip()
    if not idea:
        raise GameContractError("아이디어를 한 줄 이상 적어 주세요.", "bad_request")
    adapter = scaffold_adapter or get_adapter(engine_id)  # 지원하지 않는 엔진은 여기서 unsupported_engine
    title = (name or "").strip() or derive_name(idea)
    final_slug = await choose_slug(db, title, (slug or "").strip() or None)
    manifest = ProjectManifest(
        id=final_slug, name=title, genre=genre.strip(), engine=adapter.engine_id, version=DEFAULT_VERSION,
        entrypoint="index.html", locked_decisions=tuple(d.strip() for d in decisions if d.strip()))
    root = resolve_project_path(game_dir(final_slug))
    try:
        await adapter.scaffold_project(root, manifest)
    except GameContractError as e:
        if e.code != "exists":  # 동시에 같은 slug 로 만든 다른 호출의 디렉터리를 지우지 않는다
            _remove_created(root)
        raise
    except BaseException:
        _remove_created(root)
        raise
    try:
        game = await register_game_project(db, manifest, create_pms=create_pms)
        approved = False
        if manifest.locked_decisions:
            await approve_lock(db, game, manifest)
            approved = True
    except BaseException:
        _remove_created(root)
        raise
    return game, approved


def _remove_created(root: Path) -> None:
    """이번 호출이 방금 만든 게임 디렉터리만 지운다. 경로가 games_root/<slug> 와 정확히 같고 심볼릭 링크가 아닐 때만 삭제한다."""
    try:
        if root == resolve_project_path(root) and root.is_dir() and not root.is_symlink():
            shutil.rmtree(root)
    except (GameContractError, OSError):
        pass
