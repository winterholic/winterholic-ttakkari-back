"""게임 프로젝트 데이터 계약 (TASK-G02, docs/design/game-studio.md §4).

게임 1개 = 게임 저장소 디렉터리(Workspace) + 선택적 PMS 프로젝트 + `game_projects` 연결 행.
작업은 기존 세션·Run, 파일은 Artifact, 소스 버전은 게임 저장소 git 이 맡는다. 전용 큐·버전관리를 두지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.broker.policy import BrokerError, is_within, validate_workspace_root
from ttakkari.config import get_settings
from ttakkari.models import GameProject, PmsProject, Workspace

MANIFEST_FILE = "ttakkari-game.json"
MANIFEST_MAX_BYTES = 64 * 1024
MANIFEST_SCHEMA = 1
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
ENGINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,38}$")
MAX_DECISIONS = 50
MAX_DECISION_CHARS = 500


class GameContractError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProjectManifest:
    """docs/design/game-studio.md §3.1 의 ProjectManifest. 게임 저장소 루트의 ttakkari-game.json 이 정본 파일이다."""

    id: str
    name: str
    genre: str
    engine: str
    version: str
    entrypoint: str
    locked_decisions: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict:
        return {"schema": MANIFEST_SCHEMA, "id": self.id, "name": self.name, "genre": self.genre, "engine": self.engine,
                "version": self.version, "entrypoint": self.entrypoint, "locked_decisions": list(self.locked_decisions)}


def decisions_sha256(decisions: tuple[str, ...] | list[str]) -> str:
    """잠금 결정 해시. version 같은 다른 필드를 올려도 잠금은 유지되도록 결정 목록만 정규화해 해시한다."""
    canonical = json.dumps(list(decisions), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _text(data: dict, key: str, *, max_len: int, required: bool = True) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or "\x00" in value:
        raise GameContractError(f"매니페스트 {key} 는 문자열이어야 합니다.", "bad_manifest")
    value = value.strip()
    if required and not value:
        raise GameContractError(f"매니페스트 {key} 가 비어 있습니다.", "bad_manifest")
    if len(value) > max_len:
        raise GameContractError(f"매니페스트 {key} 가 너무 깁니다.", "bad_manifest")
    return value


def parse_manifest(raw: bytes) -> ProjectManifest:
    if len(raw) > MANIFEST_MAX_BYTES:
        raise GameContractError("매니페스트가 너무 큽니다.", "bad_manifest")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise GameContractError("매니페스트가 올바른 JSON 이 아닙니다.", "bad_manifest") from e
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise GameContractError(f"지원하지 않는 매니페스트 schema 입니다(필요: {MANIFEST_SCHEMA}).", "bad_manifest")
    game_id = _text(data, "id", max_len=40)
    if not SLUG_RE.fullmatch(game_id):
        raise GameContractError("매니페스트 id 는 소문자·숫자·하이픈 3~40자여야 합니다.", "bad_manifest")
    engine = _text(data, "engine", max_len=40)
    if not ENGINE_ID_RE.fullmatch(engine):
        raise GameContractError("매니페스트 engine 형식이 올바르지 않습니다.", "bad_manifest")
    entrypoint = _text(data, "entrypoint", max_len=200)
    if entrypoint.startswith("/") or "\\" in entrypoint or ".." in entrypoint.split("/"):
        raise GameContractError("매니페스트 entrypoint 는 프로젝트 안의 상대 경로여야 합니다.", "bad_manifest")
    decisions = data.get("locked_decisions", [])
    if not isinstance(decisions, list) or len(decisions) > MAX_DECISIONS:
        raise GameContractError("locked_decisions 는 최대 50개의 문자열 목록이어야 합니다.", "bad_manifest")
    clean: list[str] = []
    for item in decisions:
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_DECISION_CHARS or "\x00" in item:
            raise GameContractError("locked_decisions 항목은 1~500자 문자열이어야 합니다.", "bad_manifest")
        clean.append(item.strip())
    return ProjectManifest(
        id=game_id, name=_text(data, "name", max_len=200), genre=_text(data, "genre", max_len=60, required=False),
        engine=engine, version=_text(data, "version", max_len=40), entrypoint=entrypoint, locked_decisions=tuple(clean),
    )


def read_manifest(project_root: Path) -> tuple[ProjectManifest, str]:
    """프로젝트 루트의 매니페스트를 읽는다. 루트 직속 일반 파일만 허용하고(심볼릭 링크 거부) (매니페스트, 파일 sha256)을 돌려준다."""
    path = project_root / MANIFEST_FILE
    try:
        st = path.lstat()
    except FileNotFoundError as e:
        raise GameContractError(f"{MANIFEST_FILE} 가 없습니다.", "no_manifest") from e
    if not stat.S_ISREG(st.st_mode):
        raise GameContractError(f"{MANIFEST_FILE} 는 일반 파일이어야 합니다.", "bad_manifest")
    raw = path.read_bytes()
    return parse_manifest(raw), hashlib.sha256(raw).hexdigest()


def game_dir(slug: str) -> Path:
    """게임 저장소 위치: games_root/<slug>. games_root 는 allowed_roots 안이어야 하며 slug 로 바깥에 나갈 수 없다."""
    if not SLUG_RE.fullmatch(slug):
        raise GameContractError("게임 slug 형식이 올바르지 않습니다(소문자·숫자·하이픈 3~40자).", "bad_slug")
    root = get_settings().games_root.expanduser().resolve()
    if not any(is_within(root, allowed) for allowed in get_settings().allowed_roots):
        raise GameContractError("games_root 가 허용된 상위 경로(allowed_roots) 밖입니다.", "outside_root")
    return root / slug


def verify_lock(game: GameProject, manifest: ProjectManifest) -> bool:
    """저장소의 매니페스트 잠금 결정이 사용자가 승인한 해시와 같은지. 승인 전(None)이면 False."""
    return game.locked_decisions_sha256 is not None and game.locked_decisions_sha256 == decisions_sha256(manifest.locked_decisions)


async def register_game_project(db: AsyncSession, manifest: ProjectManifest, *, create_pms: bool = True) -> GameProject:
    """게임 저장소(game_dir(manifest.id)) 를 따까리 Workspace + PMS 프로젝트에 연결한다. 이미 연결된 저장소는 거부한다."""
    s = get_settings()
    path = game_dir(manifest.id)
    try:
        real = validate_workspace_root(str(path), s.allowed_roots)
    except BrokerError as e:
        raise GameContractError(str(e), e.code) from e
    if real != path:  # games_root/<slug> 자체가 다른 곳을 가리키는 심볼릭 링크면 거부
        raise GameContractError("게임 디렉터리가 심볼릭 링크이거나 예상 경로와 다릅니다.", "bad_path")
    if await db.scalar(select(GameProject.id).where(GameProject.slug == manifest.id)) is not None:
        raise GameContractError("이미 등록된 게임입니다.", "exists")
    ws = await db.scalar(select(Workspace).where(Workspace.root_path == str(real)))
    if ws is None:
        ws = Workspace(name=f"게임: {manifest.name}", root_path=str(real), is_git=(real / ".git").exists())
        db.add(ws)
        await db.flush()
    pms_id = None
    if create_pms:
        pms = PmsProject(name=manifest.name, description=f"게임 스튜디오 프로젝트 ({manifest.engine}). 저장소: {manifest.id}", status="active")
        db.add(pms)
        await db.flush()
        pms_id = pms.id
    game = GameProject(slug=manifest.id, name=manifest.name, engine_id=manifest.engine, workspace_id=ws.id, pms_project_id=pms_id)
    db.add(game)
    try:
        await db.flush()
    except IntegrityError as e:
        raise GameContractError("이미 다른 게임에 연결된 저장소입니다.", "exists") from e
    return game


async def approve_lock(db: AsyncSession, game: GameProject, manifest: ProjectManifest) -> GameProject:
    """사용자가 승인한 잠금 결정 목록을 DB 에 해시로 고정한다. 에이전트는 저장소 파일을 고칠 수 있어도 이 값은 못 바꾼다."""
    new = decisions_sha256(manifest.locked_decisions)
    if game.locked_decisions_sha256 != new:
        game.locked_decisions_sha256 = new
        game.locked_revision += 1
    await db.flush()
    return game
