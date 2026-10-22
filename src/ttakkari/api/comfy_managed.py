from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ttakkari.config import get_settings
from ttakkari.creative.comfy_inputs import load_input_image
from ttakkari.creative.director import default_director_store
from ttakkari.host_resource import LeaseBusy
from ttakkari.managed_comfy import ComfyManagedController, default_controller
from ttakkari.managed_comfy_input import InputImageError
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/comfy", tags=["managed-comfy"])


class ImageRef(BaseModel):
    """소유한 제작 자산을 id(이름)와 sha256 쌍으로 가리킨다. 경로·URL 필드는 없다."""

    model_config = ConfigDict(extra="forbid", strict=True)
    production_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    asset_id: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=1000)
    seed: int = Field(ge=0, le=2**32 - 1)
    image: ImageRef | None = None


def _controller() -> ComfyManagedController:
    return default_controller(get_settings().data_dir)


def _public(job: dict) -> dict:
    result = job.get("result") or {}
    view = {
        "task_id": job["task_id"],
        "profile": job["profile"],
        "status": job["status"],
        "runner_job_id": job.get("runner_job_id"),
        "error": job.get("error"),
        "receipt": job.get("receipt"),
        "receipt_error": job.get("receipt_error"),
        "result": {
            k: result[k]
            for k in ("id", "status", "seconds", "failure", "files", "pending_files")
            if k in result
        },
    }
    if job.get(
        "input_image"
    ):  # I2V 작업에만 보인다. 컨트롤러 내부 경로(staged_path)는 내보내지 않는다.
        view["input_image"] = {
            k: v for k, v in job["input_image"].items() if k != "staged_path"
        }
        view["input_upload"] = job.get("input_upload")
    return view


@router.get("/profiles")
async def profiles(_: User) -> list[dict]:
    return _controller().profiles()


@router.post("/jobs", status_code=202)
async def submit(body: Submit, _: User) -> dict:
    service = _controller()
    try:
        if body.image is None:
            job = service.prepare(body.profile_id, prompt=body.prompt, seed=body.seed)
        else:
            image = await asyncio.to_thread(
                load_input_image,
                default_director_store(),
                body.image.production_id,
                body.image.asset_id,
                body.image.sha256,
            )
            job = service.prepare(
                body.profile_id, prompt=body.prompt, seed=body.seed, image=image
            )
        service.start(job["task_id"])
        return _public(job)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except (InputImageError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/jobs/{task_id}")
async def status(task_id: str, _: User) -> dict:
    try:
        return _public(_controller().get(task_id))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/jobs/{task_id}/cancel")
async def cancel(task_id: str, _: User) -> dict:
    try:
        return _public(
            await asyncio.to_thread(_controller().cancel, task_id, owner_task=task_id)
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/jobs/{task_id}/reconcile")
async def reconcile(task_id: str, _: User) -> dict:
    try:
        return _public(
            await asyncio.to_thread(
                _controller().reconcile, task_id, owner_task=task_id
            )
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc
