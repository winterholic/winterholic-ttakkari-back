"""버전 이력 탐색·비교의 읽기 전용 조회 (TASK-G32, ADR 0061).

새 표를 만들지 않는다. 버전(빌드)이 어떤 요청에서 나왔는지는 GameBuild.run_id → Run.usage["game"] 표식과 Run.prompt 에서,
버전 사이의 소스 변경은 게임 저장소의 git 객체(두 commit 의 diff·log)에서 읽는다. 어느 쪽도 쓰지 않는다.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.games.adapter import run_git
from ttakkari.games.builds import COMMIT_RE
from ttakkari.games.contract import GameContractError
from ttakkari.games.worker import USAGE_KEY
from ttakkari.models import GameBuild, GameProject, Run

REQUEST_MARKER = "[사용자 요청]\n"  # games/worker.py compose_prompt 가 사용자 요청 앞에 붙이는 머리말
INSTRUCTION_CHARS = 200
RUN_SCAN_LIMIT = 500  # 한 게임의 요청 Run 을 최근 순으로 이만큼만 훑는다
MAX_COMMITS = 30
MAX_FILES = 40
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str, limit: int) -> str:
    one = " ".join(_CONTROL.sub("", text).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def instruction_of(prompt: str) -> str:
    """worker prompt 에서 사용자가 적은 요청 본문. 머리말이 없으면 빈 문자열(표시용이라 길이를 줄인다)."""
    _, marker, tail = prompt.rpartition(REQUEST_MARKER)
    return _clean(tail, INSTRUCTION_CHARS) if marker else ""


@dataclass
class RequestInfo:
    run_id: uuid.UUID
    session_id: uuid.UUID
    status: str
    kind: str
    instruction: str
    base_build_id: uuid.UUID | None
    feedback: dict | None
    repair_attempt: int
    lock_after: str | None
    created_at: datetime


@dataclass
class VersionRow:
    build: GameBuild
    is_active: bool
    produced_by: RequestInfo | None
    requests_on: int = 0
    feedback_requests_on: int = 0
    base_seq: int | None = None


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def list_versions(db: AsyncSession, game: GameProject, active_id: uuid.UUID | None, limit: int) -> list[VersionRow]:
    builds = list(await db.scalars(select(GameBuild).where(GameBuild.game_id == game.id).order_by(GameBuild.seq.desc()).limit(limit)))
    seq_of = {b.id: b.seq for b in builds}
    rows = (await db.execute(
        select(Run.id, Run.session_id, Run.status, Run.usage, Run.prompt, Run.created_at)
        .where(Run.workspace_id == game.workspace_id, Run.usage[USAGE_KEY]["game_id"].astext == str(game.id))
        .order_by(Run.created_at.desc()).limit(RUN_SCAN_LIMIT))).all()
    infos: dict[uuid.UUID, RequestInfo] = {}
    on_build: dict[uuid.UUID, list[RequestInfo]] = {}
    for run_id, session_id, status, usage, prompt, created_at in rows:
        tag = (usage or {}).get(USAGE_KEY) or {}
        fb = tag.get("feedback") if isinstance(tag.get("feedback"), dict) else None
        info = RequestInfo(
            run_id=run_id, session_id=session_id, status=str(status), kind=str(tag.get("kind", "")), instruction=instruction_of(prompt or ""),
            base_build_id=_uuid_or_none(tag.get("build_id")), feedback=fb, repair_attempt=int(tag.get("repair_attempt") or 0),
            lock_after=tag.get("lock_after"), created_at=created_at)
        infos[run_id] = info
        if info.base_build_id is not None:
            on_build.setdefault(info.base_build_id, []).append(info)
    out: list[VersionRow] = []
    for b in builds:
        produced = infos.get(b.run_id) if b.run_id else None
        mine = on_build.get(b.id, [])
        out.append(VersionRow(
            build=b, is_active=b.id == active_id, produced_by=produced, requests_on=len(mine),
            feedback_requests_on=sum(1 for r in mine if r.feedback is not None),
            base_seq=seq_of.get(produced.base_build_id) if produced and produced.base_build_id else None))
    return out


@dataclass
class SourceDiff:
    available: bool
    reason: str = ""
    same_commit: bool = False
    files_total: int = 0
    insertions: int = 0
    deletions: int = 0
    files: list[dict] = field(default_factory=list)
    commits: list[dict] = field(default_factory=list)
    commits_truncated: bool = False


def _parse_numstat(raw: str) -> list[tuple[str, int | None, int | None]]:
    out: list[tuple[str, int | None, int | None]] = []
    for entry in raw.split("\x00"):
        parts = entry.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        # 바이너리 파일은 "-" 로 온다
        out.append((_clean(path, 200), None if added == "-" else int(added), None if deleted == "-" else int(deleted)))
    return out


async def source_diff(root: Path, base_commit: str | None, head_commit: str | None) -> SourceDiff:
    """두 빌드의 source commit 사이 변경. git 객체만 읽는다(작업 트리·인덱스는 건드리지 않는다). 읽을 수 없으면 사유와 함께 available=False."""
    if not base_commit or not head_commit:
        return SourceDiff(False, "한쪽 빌드에 커밋 기록이 없어 소스 변경을 비교할 수 없어요.")
    if not (COMMIT_RE.fullmatch(base_commit) and COMMIT_RE.fullmatch(head_commit)):
        return SourceDiff(False, "커밋 기록 형식이 올바르지 않아요.")
    if base_commit == head_commit:
        return SourceDiff(True, same_commit=True)
    try:
        for c in (base_commit, head_commit):
            await run_git(root, "cat-file", "-e", f"{c}^{{commit}}")
        raw = await run_git(root, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base_commit, head_commit, "--")
        log = await run_git(root, "log", "--no-color", "--format=%h%x1f%s%x1e", f"-n{MAX_COMMITS + 1}", f"{base_commit}..{head_commit}", "--")
    except GameContractError:
        return SourceDiff(False, "저장소에서 두 커밋을 읽지 못했어요(저장소가 다시 만들어졌거나 정리됐을 수 있어요).")
    stats = _parse_numstat(raw)
    stats.sort(key=lambda s: -((s[1] or 0) + (s[2] or 0)))
    commits = []
    for rec in log.split("\x1e"):
        rec = rec.strip("\n")
        if "\x1f" in rec:
            sha, subject = rec.split("\x1f", 1)
            commits.append({"sha": _clean(sha, 12), "subject": _clean(subject, 100)})
    return SourceDiff(
        True, files_total=len(stats), insertions=sum(s[1] or 0 for s in stats), deletions=sum(s[2] or 0 for s in stats),
        files=[{"path": p, "added": a, "deleted": d} for p, a, d in stats[:MAX_FILES]],
        commits=commits[:MAX_COMMITS], commits_truncated=len(commits) > MAX_COMMITS)
