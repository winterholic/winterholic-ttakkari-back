"""Authenticated, single-owner creative studio API. No arbitrary local paths or URLs."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import Field

from ttakkari.creative.catalog import get_catalog, validate_project_spec
from ttakkari.creative.director import ConflictError, default_director_store
from ttakkari.creative.records import MEDIA_ID_PATTERN, RecordsService
from ttakkari.creative.render import inspect_media
from ttakkari.creative.service import ProjectInput, Scene, Strict, default_studio
from ttakkari.security.auth import User, decode, issue_download_token

router = APIRouter(prefix="/api/creative", tags=["creative-studio"])


@router.get("/catalog")
async def catalog(_: User):
    return get_catalog()


@router.post("/productions", status_code=201)
async def create_production(body: ProductionInput, _: User):
    return perform(default_director_store().create_production, body.brief)


@router.get("/productions/{pid}")
async def get_production(pid: uuid.UUID, _: User):
    return perform(default_director_store().get_production, str(pid))


@router.post("/productions/{pid}/artifacts", status_code=201)
async def upload_production_artifact(pid: uuid.UUID, file: UploadFile, _: User):
    store = default_director_store()
    perform(store.get_production, str(pid))
    name = Path(file.filename or "").name
    content = await file.read(100 * 1024 * 1024 + 1)
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(413, "파일은 100MB 이하로 올려 주세요.")
    return perform(
        store.put_artifact,
        str(pid),
        name,
        content,
        media_type=file.content_type or "application/octet-stream",
    )


@router.post("/productions/{pid}/attempts", status_code=202)
async def execute_cpu_attempt(pid: uuid.UUID, body: AttemptInput, _: User):
    store = default_director_store()
    attempt = perform(
        store.submit_attempt,
        str(pid),
        body.shot_id,
        input_sha256=body.input_sha256,
        idempotency_key=body.idempotency_key,
    )
    # CPU adapter uses an existing immutable media asset; real engines enter through this receipt later.
    return perform(
        store.execute_attempt,
        str(pid),
        attempt["attempt_id"],
        renderer=lambda source: [source],
    )


@router.post("/productions/{pid}/attempts/{attempt_id}/cancel")
async def cancel_cpu_attempt(pid: uuid.UUID, attempt_id: uuid.UUID, _: User):
    return perform(default_director_store().cancel_attempt, str(pid), str(attempt_id))


@router.get("/records")
async def list_records(
    _: User,
    source: Literal["g0", "v1", "lab"] | None = None,
    kind: str | None = Query(default=None, max_length=40),
    engine: str | None = Query(default=None, max_length=40),
    q: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=100, ge=1, le=200),
):
    """제작 기록 목록. g0/v1/lab 세 원천을 하나의 읽기 모델로 합친다."""
    result = await asyncio.to_thread(
        _records().list, source=source, kind=kind, engine=engine, q=q, limit=limit
    )
    for item in result["items"]:
        if item.thumbnail_media_id:
            item.thumbnail_url = _record_media_url(item.id, item.thumbnail_media_id)
    return result


@router.get("/records/{rid}")
async def get_record(rid: RecordId, _: User):
    record = await asyncio.to_thread(perform, _records().get, rid)
    for item in record.media:
        item.url = _record_media_url(rid, item.id) if item.available else None
    return record


@router.get("/records/{rid}/media/{media_id}")
async def record_media(
    rid: RecordId,
    media_id: str = PathParam(pattern=MEDIA_ID_PATTERN),
    token: str = Query(max_length=2000),
    download: bool = False,
):
    try:
        claims = decode(token, "download")
        if (
            claims.get("sub") != str(_record_media_ident(rid, media_id))
            or claims.get("var") != f"creative-record:{rid}"
        ):
            raise jwt.InvalidTokenError()
    except jwt.PyJWTError as e:
        raise HTTPException(
            403, "재생 주소가 만료됐습니다. 제작 기록을 다시 열어 주세요."
        ) from e
    file = perform(_records().media_file, rid, media_id)
    return FileResponse(
        file.path,
        media_type=file.mime,
        filename=file.filename,
        content_disposition_type="attachment" if download else "inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.post("/validate")
async def validate_spec(body: dict, _: User):
    errors = validate_project_spec(body)
    return {"valid": len(errors) == 0, "errors": errors}


RecordId = Annotated[
    str,
    PathParam(
        pattern=r"^(?:(?:g0|v1)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        r"|lab-[a-z0-9][a-z0-9-]{1,62})$"
    ),
]


def _records() -> RecordsService:
    from ttakkari.config import get_settings

    return RecordsService(
        director=default_director_store(),
        studio=default_studio(),
        lab_root=get_settings().studio_lab_root,
    )


def _record_media_ident(rid: str, media_id: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"ttakkari-creative-record:{rid}:{media_id}")


def _record_media_url(rid: str, media_id: str) -> str:
    token, _ = issue_download_token(
        _record_media_ident(rid, media_id), f"creative-record:{rid}"
    )
    return f"/api/creative/records/{rid}/media/{media_id}?" + urlencode(
        {"token": token}
    )


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


class ProductionInput(Strict):
    brief: str = Field(min_length=1, max_length=6000)


class AttemptInput(Strict):
    shot_id: str = Field(min_length=1, max_length=80)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=160)


def perform(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ConflictError as e:
        raise HTTPException(409, str(e)) from e
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
