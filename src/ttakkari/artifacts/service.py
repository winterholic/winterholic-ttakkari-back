from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.artifacts.kinds import detect, needs_server_preview
from ttakkari.broker.policy import (
    BrokerError,
    ResolvedPath,
    ensure_regular_file,
    is_sensitive,
)
from ttakkari.config import get_settings
from ttakkari.models import Artifact, ExportPolicy, PreviewStatus, utcnow

log = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
    return name[:200] or "file"


async def register_file(
    db: AsyncSession,
    resolved: ResolvedPath,
    *,
    source: str,
    workspace_id: uuid.UUID | None,
    session_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    rel_path: str | None = None,
    export_allowed: bool = True,
    force_copy: bool = False,
) -> Artifact:
    """검증된 경로를 Artifact 로 등록한다.

    작은 파일은 관리 영역으로 복사해 원본이 나중에 바뀌어도 결과물을 보존한다.
    큰 파일은 참조만 하고 sha256 으로 변경 여부를 판별한다.
    """
    s = get_settings()
    st = ensure_regular_file(resolved.path)
    mime, kind = detect(resolved.path)
    sha = await asyncio.to_thread(_sha256, resolved.path)

    art_id = uuid.uuid4()
    sensitive = is_sensitive(resolved.path)
    copy = force_copy or st.st_size <= s.copy_max_bytes
    if copy:
        dest_dir = s.artifact_store / str(art_id)
        dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = dest_dir / _safe_filename(resolved.path.name)
        await asyncio.to_thread(shutil.copy2, resolved.path, dest)
        stored, mode = dest, "copy"
    else:
        stored, mode = resolved.path, "reference"

    if sensitive:
        policy = ExportPolicy.sensitive
    elif not export_allowed:
        policy = ExportPolicy.deny
    else:
        policy = ExportPolicy.allow

    art = Artifact(
        id=art_id,
        workspace_id=workspace_id,
        session_id=session_id,
        run_id=run_id,
        filename=_safe_filename(resolved.path.name),
        mime_type=mime,
        kind=kind,
        source=source,
        original_path=str(resolved.path),
        rel_path=rel_path if rel_path is not None else resolved.rel,
        storage_mode=mode,
        stored_path=str(stored),
        size_bytes=st.st_size,
        sha256=sha,
        preview_status=PreviewStatus.pending if needs_server_preview(kind) else PreviewStatus.not_required,
        export_policy=policy,
        retention_until=utcnow() + timedelta(days=s.artifact_retention_days),
    )
    db.add(art)
    await db.flush()
    return art


def content_path(art: Artifact) -> Path:
    """다운로드·열람 직전에 다시 검증한다. 참조 모드 파일은 그 사이에 바뀌거나 치환됐을 수 있다."""
    p = Path(art.stored_path)
    if art.storage_mode == "copy":
        store = get_settings().artifact_store.resolve()
        real = p.resolve(strict=False)
        if store not in real.parents:
            raise BrokerError("관리 영역 밖의 파일입니다.", "outside_root")
        if not real.is_file():
            raise BrokerError("원본 파일이 사라졌습니다.", "gone")
        return real
    if p.is_symlink():
        raise BrokerError("심볼릭 링크로 바뀐 파일입니다.", "bad_path")
    if not p.is_file():
        raise BrokerError("원본 파일이 사라졌습니다.", "gone")
    real = p.resolve(strict=True)
    if str(real) != art.original_path:
        raise BrokerError("원본 경로가 바뀌었습니다.", "bad_path")
    return real


def preview_file(art: Artifact) -> Path | None:
    if art.preview_status != PreviewStatus.ready or not art.preview_path:
        return None
    p = Path(art.preview_path).resolve(strict=False)
    store = get_settings().artifact_store.resolve()
    if store not in p.parents or not p.is_file():
        return None
    return p


def remove_stored(art: Artifact) -> None:
    if art.storage_mode == "copy":
        d = get_settings().artifact_store / str(art.id)
        shutil.rmtree(d, ignore_errors=True)
