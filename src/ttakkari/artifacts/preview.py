"""PPTX·DOCX → PDF 미리보기 변환. LibreOffice(soffice)가 없으면 unavailable 로 두고 원본 다운로드만 제공한다."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import select

from ttakkari.artifacts.service import content_path
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import Artifact, PreviewStatus

log = logging.getLogger(__name__)
CONVERT_TIMEOUT = 180

_queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
_worker: asyncio.Task[None] | None = None


def soffice_path() -> str | None:
    s = get_settings()
    if s.soffice_bin:
        return s.soffice_bin
    for cand in ("soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        found = shutil.which(cand) or (cand if Path(cand).exists() else None)
        if found:
            return found
    return None


def enqueue(artifact_id: uuid.UUID) -> None:
    _queue.put_nowait(artifact_id)


async def _set(art_id: uuid.UUID, **fields: object) -> Artifact | None:
    async with sessionmaker()() as db:
        art = await db.get(Artifact, art_id)
        if art is None:
            return None
        for k, v in fields.items():
            setattr(art, k, v)
        await db.commit()
        return art


async def convert(art_id: uuid.UUID) -> None:
    async with sessionmaker()() as db:
        art = await db.get(Artifact, art_id)
        if art is None or art.preview_status not in (PreviewStatus.pending, PreviewStatus.failed):
            return
        try:
            src = content_path(art)
        except Exception as e:  # noqa: BLE001
            await _set(art_id, preview_status=PreviewStatus.failed, preview_error=str(e))
            return
    bin_ = soffice_path()
    if not bin_:
        await _set(art_id, preview_status=PreviewStatus.unavailable, preview_error="LibreOffice 가 설치되지 않았습니다.")
        return
    await _set(art_id, preview_status=PreviewStatus.processing, preview_error=None)
    out_dir = get_settings().artifact_store / str(art_id) / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 동시 변환 시 LibreOffice 프로필 잠금 충돌을 피하려고 변환마다 프로필을 분리한다.
    with tempfile.TemporaryDirectory() as profile:
        proc = await asyncio.create_subprocess_exec(
            bin_, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "pdf",
            "--outdir", str(out_dir), str(src),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), CONVERT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await _set(art_id, preview_status=PreviewStatus.failed, preview_error="변환 시간 초과")
            return
    pdfs = list(out_dir.glob("*.pdf"))
    if proc.returncode != 0 or not pdfs:
        await _set(art_id, preview_status=PreviewStatus.failed,
                   preview_error=(err.decode(errors="replace")[-500:] or "변환 실패"))
        return
    await _set(art_id, preview_status=PreviewStatus.ready, preview_path=str(pdfs[0]), preview_mime="application/pdf")


async def _run_worker() -> None:
    while True:
        art_id = await _queue.get()
        try:
            await convert(art_id)
        except Exception:
            log.exception("preview conversion crashed for %s", art_id)
            await _set(art_id, preview_status=PreviewStatus.failed, preview_error="내부 오류")


async def start_worker() -> None:
    global _worker
    async with sessionmaker()() as db:
        # 재시작 전에 처리 중이던 변환을 다시 큐에 넣는다.
        rows = await db.scalars(
            select(Artifact.id).where(
                Artifact.preview_status.in_([PreviewStatus.pending, PreviewStatus.processing]),
                Artifact.deleted_at.is_(None),
            )
        )
        for art_id in rows:
            await _set(art_id, preview_status=PreviewStatus.pending)
            enqueue(art_id)
    _worker = asyncio.create_task(_run_worker())


async def stop_worker() -> None:
    if _worker:
        _worker.cancel()
