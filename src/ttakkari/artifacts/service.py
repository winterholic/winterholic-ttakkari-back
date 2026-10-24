from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.artifacts.kinds import detect, detect_category, needs_server_preview
from ttakkari.broker.policy import (
    BrokerError,
    ResolvedPath,
    ensure_regular_file,
    is_sensitive,
)
from ttakkari.config import get_settings
from ttakkari.models import (
    Artifact,
    ArtifactArea,
    ArtifactKeepReason,
    ArtifactOrigin,
    ExportPolicy,
    PreviewStatus,
    default_artifact_area,
    utcnow,
)

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


_DEFAULT = object()


async def register_file(
    db: AsyncSession,
    resolved: ResolvedPath,
    *,
    source: str,
    area: str | None = None,
    origin: str | None = None,
    category: str | None = None,
    keep_reason: Any = _DEFAULT,
    keep_evidence: dict | None = None,
    workspace_id: uuid.UUID | None,
    session_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    rel_path: str | None = None,
    export_allowed: bool = True,
    force_copy: bool = False,
    keep_in_place: bool = False,
) -> Artifact:
    """검증된 경로를 Artifact 로 등록한다.

    작은 파일은 관리 영역으로 복사해 원본이 나중에 바뀌어도 결과물을 보존한다.
    큰 파일은 참조만 하고 sha256 으로 변경 여부를 판별한다.
    """
    s = get_settings()
    st = ensure_regular_file(resolved.path)
    mime, kind = detect(resolved.path)
    cat = category or detect_category(resolved.path, mime, kind)
    sha = await asyncio.to_thread(_sha256, resolved.path)

    art_id = uuid.uuid4()
    sensitive = is_sensitive(resolved.path)
    # 업로드 파일은 이미 관리 영역에 있으니 다시 복사하지 않는다.
    copy = not keep_in_place and (force_copy or st.st_size <= s.copy_max_bytes)
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

    if origin is None:
        if source == "upload" or source == "user_registered":
            origin = ArtifactOrigin.user
        elif source in ("agent_output", "agent_modified"):
            origin = ArtifactOrigin.agent
        else:
            origin = ArtifactOrigin.unknown

    if keep_reason is _DEFAULT:
        if source == "upload":
            keep_reason = ArtifactKeepReason.direct_upload
        elif source == "user_registered":
            keep_reason = ArtifactKeepReason.direct_register
        else:
            keep_reason = None

    if area is None:
        if keep_reason is not None:
            area = ArtifactArea.files
        else:
            area = default_artifact_area(source)

    art = Artifact(
        id=art_id,
        workspace_id=workspace_id,
        session_id=session_id,
        run_id=run_id,
        filename=_safe_filename(resolved.path.name),
        mime_type=mime,
        kind=kind,
        category=cat,
        source=source,
        origin=origin,
        area=area,
        keep_reason=keep_reason,
        keep_evidence=keep_evidence,
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


async def refresh_from_original(art: Artifact, real: Path) -> bool:
    """이미 등록된 행의 원본이 바뀌었으면 보관 사본·메타데이터를 갱신한다.

    수집 뒤 원본을 고치고 share 로 넘기면 다운로드가 옛날 사본을 내보낸다.
    sha·크기가 같으면 손대지 않고 False 를 돌려준다(미리보기 상태 유지).
    """
    st = ensure_regular_file(real)
    # 해시가 같아 일찍 돌아오는 경우에도 보관 위치가 관리 영역 안인지 먼저 확인한다.
    dest: Path | None = None
    if art.storage_mode == "copy":
        store = get_settings().artifact_store.resolve()
        dest = Path(art.stored_path).resolve(strict=False)
        if store not in dest.parents:
            raise BrokerError("관리 영역 밖의 파일입니다.", "outside_root")
    sha = await asyncio.to_thread(_sha256, real)
    if sha == art.sha256 and st.st_size == art.size_bytes:
        return False
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, real, dest)
    mime, kind = detect(real)
    art.size_bytes = st.st_size
    art.sha256 = sha
    art.mime_type = mime
    art.kind = kind
    art.category = detect_category(real, mime, kind)
    art.preview_status = PreviewStatus.pending if needs_server_preview(kind) else PreviewStatus.not_required
    art.preview_path = None
    art.preview_error = None
    return True


_LINE_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?){1,2}$")
_LINE_FRAGMENT = re.compile(r"#L?\d+(?:[-:]L?\d+)*$")


def parse_link_path(raw: str) -> str:
    """메시지 링크 대상을 파일 경로 문자열로 정리한다. 파일 시스템은 건드리지 않는다.

    `<경로>` 꺾쇠, `file://` URL, `~/`, 끝의 `:줄`·`:줄:열`·`#L줄` 을 걷어낸다.
    """
    p = raw.strip()
    if p.startswith("<") and p.endswith(">"):
        p = p[1:-1].strip()
    if p.lower().startswith("file:"):
        u = urlparse(p)
        if u.netloc not in ("", "localhost"):
            raise BrokerError("이 기기의 파일 링크만 열 수 있습니다.", "bad_path")
        p = unquote(u.path)
    p = _LINE_FRAGMENT.sub("", p)
    p = _LINE_SUFFIX.sub("", p)
    if "\x00" in p or not p:
        raise BrokerError("잘못된 경로입니다.", "bad_path")
    if p == "~" or p.startswith("~/"):
        p = str(Path.home() / p[2:])
    return p


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
    s = get_settings()
    if art.storage_mode == "copy":
        shutil.rmtree(s.artifact_store / str(art.id), ignore_errors=True)
    elif art.source == "upload":
        d = Path(art.original_path).parent
        if d.parent == s.uploads_root.resolve():
            shutil.rmtree(d, ignore_errors=True)
