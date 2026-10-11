"""폰 → 컴퓨터 파일 보내기. 조각 업로드(끊겨도 이어 올리기) + 마무리(사진 손질·이름 정리·목적지 이동·보관함 등록)."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.artifacts import preview
from ttakkari.artifacts.kinds import needs_server_preview
from ttakkari.artifacts.service import register_file
from ttakkari.broker.policy import BrokerError, ResolvedPath, is_sensitive, is_within
from ttakkari.config import get_settings
from ttakkari.models import Artifact, Workspace
from ttakkari.transfer import media

STALE_SECONDS = 24 * 3600
MAX_NAME = 200


@dataclass
class Meta:
    id: str
    filename: str
    size: int
    dest_dir: str
    convert_heic: bool = True
    strip_location: bool = True
    keep_original: bool = False
    created: float = 0.0


def staging_root() -> Path:
    return get_settings().data_dir / "incoming"


def write_scope(ws: Workspace) -> list[Path]:
    """보낼 수 있는 곳 = 기본 워크스페이스의 실행 위치 + 권한 폴더(에이전트가 쓸 수 있는 곳과 같다)."""
    return [Path(ws.root_path).resolve(), *[Path(p).resolve() for p in ws.extra_writable_roots]]


def check_dest(raw: str, scope: list[Path], *, create: bool = False) -> Path:
    if "\x00" in raw:
        raise BrokerError("잘못된 경로입니다.", "bad_path")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise BrokerError("폴더는 절대 경로여야 합니다.", "bad_path")
    # 아직 없는 폴더도 받되, 실제 경로(심볼릭 링크 풀린)로 범위를 본다.
    real = p.resolve()
    if not any(is_within(real, r) for r in scope):
        raise BrokerError("권한 폴더 밖이에요. 설정 > 권한 폴더에 먼저 추가해 주세요.", "outside_root")
    if is_sensitive(real) or is_within(real, get_settings().home):
        raise BrokerError("이 폴더에는 보낼 수 없어요.", "sensitive")
    if real.exists() and not real.is_dir():
        raise BrokerError("폴더가 아니에요.", "not_a_dir")
    if create:
        real.mkdir(parents=True, exist_ok=True)
    return real


def safe_name(raw: str) -> str:
    name = Path(raw.replace("\\", "/")).name.strip().replace("\x00", "")
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"|?*')
    if not name or name in {".", ".."}:
        name = "upload"
    if name.startswith("."):
        name = "_" + name
    stem, ext = Path(name).stem, Path(name).suffix
    return (stem[: MAX_NAME - len(ext)] + ext) if len(name) > MAX_NAME else name


def _dir(tid: str) -> Path:
    uuid.UUID(tid)  # 경로 조작 방지
    return staging_root() / tid


def create(meta: Meta) -> Meta:
    d = _dir(meta.id)
    d.mkdir(parents=True, mode=0o700)
    (d / "data").touch()
    meta.created = time.time()
    save(meta)
    return meta


def save(meta: Meta) -> None:
    (_dir(meta.id) / "meta.json").write_text(json.dumps(asdict(meta)))


def load(tid: str) -> Meta:
    try:
        return Meta(**json.loads((_dir(tid) / "meta.json").read_text()))
    except (OSError, ValueError, TypeError) as e:
        raise BrokerError("올리는 중인 파일을 찾을 수 없어요.", "not_found") from e


def received(tid: str) -> int:
    return (_dir(tid) / "data").stat().st_size


def append(tid: str, offset: int, chunk: bytes) -> int:
    """offset 이 지금까지 받은 크기와 같을 때만 붙인다. 다르면 현재 크기를 알려 이어 올리게 한다."""
    meta = load(tid)
    data = _dir(tid) / "data"
    cur = data.stat().st_size
    if offset != cur:
        raise OffsetMismatch(cur)
    if cur + len(chunk) > meta.size:
        raise BrokerError("선언한 크기보다 많이 보냈어요.", "too_large")
    with data.open("ab") as f:
        f.write(chunk)
    return cur + len(chunk)


class OffsetMismatch(Exception):
    def __init__(self, received: int) -> None:
        super().__init__(received)
        self.received = received


def cancel(tid: str) -> None:
    shutil.rmtree(_dir(tid), ignore_errors=True)


@dataclass
class Delivered:
    path: str
    name: str
    size: int
    artifact_id: str | None


async def finalize(db: AsyncSession, ws: Workspace, tid: str) -> tuple[list[Delivered], list[str]]:
    """다 받은 파일을 손질해 목적지로 옮기고 보관함에 등록한다."""
    meta = load(tid)
    data = _dir(tid) / "data"
    if data.stat().st_size != meta.size:
        raise BrokerError("아직 다 받지 못했어요.", "incomplete")
    dest = check_dest(meta.dest_dir, write_scope(ws), create=True)
    work = _dir(tid) / "work"
    work.mkdir(exist_ok=True)
    src = work / safe_name(meta.filename)
    data.rename(src)
    done = await asyncio.to_thread(media.process_image, src, src.name, convert_heic=meta.convert_heic,
                                   strip_location=meta.strip_location, keep_original=meta.keep_original)
    out: list[Delivered] = []
    for p in [done.path, *done.extra]:
        target = media.unique_dest(dest, p.name)
        await asyncio.to_thread(shutil.move, str(p), str(target))
        art = await _register(db, ws, target, dest)
        out.append(Delivered(path=str(target), name=target.name, size=target.stat().st_size,
                             artifact_id=str(art.id) if art else None))
    cancel(tid)
    return out, done.notes


async def _register(db: AsyncSession, ws: Workspace, path: Path, root: Path) -> Artifact | None:
    try:
        art = await register_file(db, ResolvedPath(root=root, path=path.resolve()), source="upload", workspace_id=ws.id,
                                  rel_path=path.name, export_allowed=ws.export_allowed)
    except (BrokerError, OSError):
        # 보관함 등록이 안 돼도 파일은 이미 목적지에 있다.
        return None
    await db.flush()
    if needs_server_preview(art.kind):
        # 커밋 뒤에 변환 워커가 읽도록 호출자가 커밋한 다음 넣는다.
        _pending_preview.append(art.id)
    return art


_pending_preview: list[uuid.UUID] = []


def flush_previews() -> None:
    while _pending_preview:
        preview.enqueue(_pending_preview.pop())


def sweep_stale() -> int:
    """하루 넘게 마무리 안 된 업로드 조각을 지운다."""
    root = staging_root()
    if not root.exists():
        return 0
    n = 0
    for d in root.iterdir():
        try:
            if time.time() - d.stat().st_mtime > STALE_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n
