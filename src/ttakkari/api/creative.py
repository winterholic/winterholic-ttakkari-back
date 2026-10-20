"""Authenticated, single-owner creative studio API. No arbitrary local paths or URLs."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import Field

from ttakkari.creative.catalog import get_catalog, validate_project_spec
from ttakkari.creative.render import inspect_media
from ttakkari.creative.service import ProjectInput, Scene, Strict, default_studio
from ttakkari.security.auth import User, decode, issue_download_token

router = APIRouter(prefix="/api/creative", tags=["creative-studio"])


@router.get("/catalog")
async def catalog(_: User):
    return get_catalog()


@router.post("/validate")
async def validate_spec(body: dict, _: User):
    errors = validate_project_spec(body)
    return {"valid": len(errors) == 0, "errors": errors}


class Edit(Strict):
    revision: int = Field(ge=1)
    name: str = Field(default="", max_length=120)
    brief: str = Field(default="", max_length=6000)
    style: str = Field(default="", max_length=1000)
    aspect: Literal["16:9", "9:16", "1:1"] = "16:9"
    music_asset_id: str | None = None
    scenes: list[Scene] = Field(default_factory=list, min_length=1, max_length=60)


class RenderInput(Strict):
    retry: bool = False


class MediaInput(Strict):
    kind: Literal["asset", "render"]
    id: uuid.UUID


def perform(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409 if "다른 변경" in str(e) else 422, str(e)) from e


def _public(p):
    # Render input snapshots remain on disk for traceability; avoid returning them every poll.
    return {
        **p,
        "render_jobs": [
            {k: v for k, v in j.items() if k != "snapshot"} for j in p["render_jobs"]
        ],
    }


@router.get("/projects")
async def projects(_: User):
    return [_public(p) for p in default_studio().list()]


@router.post("/projects", status_code=201)
async def create(body: ProjectInput, _: User):
    return _public(default_studio().create(body))


@router.get("/projects/{pid}")
async def project(pid: uuid.UUID, _: User):
    return _public(perform(default_studio().get, str(pid)))


@router.patch("/projects/{pid}")
async def edit(pid: uuid.UUID, body: Edit, _: User):
    changes = body.model_dump(exclude_unset=True, exclude={"revision"})
    if "name" in changes and not changes["name"].strip():
        raise HTTPException(422, "제목을 입력해 주세요.")
    return _public(perform(default_studio().edit, str(pid), body.revision, **changes))


@router.post("/projects/{pid}/demo")
async def demo(pid: uuid.UUID, _: User):
    studio = default_studio()
    perform(studio.get, str(pid))
    return _public(await asyncio.to_thread(studio.demo, str(pid)))


@router.post("/projects/{pid}/scenes/{scene_id}/illustration")
async def illustrate(pid: uuid.UUID, scene_id: uuid.UUID, _: User):
    from ttakkari.creative.demo import generate_illustration

    studio = default_studio()
    perform(studio.get, str(pid))
    return _public(
        await asyncio.to_thread(
            perform, generate_illustration, studio, str(pid), str(scene_id)
        )
    )


def _normalize(path: Path, ext: str):
    if ext in {".png", ".jpg", ".jpeg", ".webp"}:
        try:
            with Image.open(path) as source:
                if source.width * source.height > 32_000_000:
                    raise ValueError("이미지는 3,200만 화소 이하로 올려 주세요.")
                im = ImageOps.exif_transpose(source).convert("RGB")
                im.thumbnail((4096, 4096))
                target = path.with_suffix(".png")
                im.save(target)
                return target, "image", {"width": im.width, "height": im.height}
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
            raise ValueError("이미지를 읽을 수 없습니다.") from e
    info = inspect_media(path)
    if info["duration"] > 900:
        raise ValueError("미디어 길이는 15분 이하로 올려 주세요.")
    kind = "video" if ext in {".mp4", ".mov", ".webm"} else "audio"
    if (kind == "video" and not info["has_video"]) or (
        kind == "audio" and not info["has_audio"]
    ):
        raise ValueError("파일에 필요한 음악·영상 트랙이 없습니다.")
    return path, kind, info


@router.post("/projects/{pid}/assets", status_code=201)
async def upload(pid: uuid.UUID, file: UploadFile, _: User):
    studio = default_studio()
    perform(studio.get, str(pid))
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".mp3",
        ".wav",
        ".m4a",
        ".flac",
        ".ogg",
        ".mp4",
        ".mov",
        ".webm",
    }:
        raise HTTPException(422, "PNG/JPG/WebP, 음악 파일, MP4/MOV/WebM을 올려 주세요.")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ("upload" + ext)
        size = 0
        with path.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise HTTPException(413, "파일은 100MB 이하로 올려 주세요.")
                f.write(chunk)
        normalized, kind, info = await asyncio.to_thread(perform, _normalize, path, ext)
        return studio.add_asset(
            str(pid),
            normalized,
            name=file.filename or "자산",
            kind=kind,
            provenance="user-upload",
            metadata=info,
        )


@router.post("/projects/{pid}/renders", status_code=202)
async def enqueue(pid: uuid.UUID, body: RenderInput, _: User):
    return {
        k: v
        for k, v in perform(
            default_studio().enqueue, str(pid), retry=body.retry
        ).items()
        if k != "snapshot"
    }


def _file(studio, pid: str, kind: str, ident: str):
    p = perform(studio.get, pid)
    if kind == "asset":
        a = next((a for a in p["assets"] if a["id"] == ident), None)
        if a:
            return studio.path(pid) / a["file"], a["name"]
    else:
        j = next(
            (
                j
                for j in p["render_jobs"]
                if j["id"] == ident and j["status"] == "succeeded"
            ),
            None,
        )
        if j:
            return studio.path(pid) / j["output"], p["name"] + ".mp4"
    raise HTTPException(404, "미디어가 없습니다.")


@router.post("/projects/{pid}/media-link")
async def media_link(pid: uuid.UUID, body: MediaInput, _: User):
    _file(default_studio(), str(pid), body.kind, str(body.id))
    token, exp = issue_download_token(body.id, f"creative:{pid}:{body.kind}")
    return {
        "url": f"/api/creative/projects/{pid}/media/{body.kind}/{body.id}?"
        + urlencode({"token": token}),
        "expires_at": exp.isoformat(),
    }


@router.get("/projects/{pid}/media/{kind}/{ident}")
async def media(
    pid: uuid.UUID,
    kind: Literal["asset", "render"],
    ident: uuid.UUID,
    token: str,
    download: bool = False,
):
    try:
        claims = decode(token, "download")
        if (
            claims.get("sub") != str(ident)
            or claims.get("var") != f"creative:{pid}:{kind}"
        ):
            raise jwt.InvalidTokenError()
    except jwt.PyJWTError as e:
        raise HTTPException(
            403, "재생 주소가 만료됐습니다. 미리보기를 다시 열어 주세요."
        ) from e
    path, name = _file(default_studio(), str(pid), kind, str(ident))
    return FileResponse(
        path,
        filename=name,
        content_disposition_type="attachment" if download else "inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
