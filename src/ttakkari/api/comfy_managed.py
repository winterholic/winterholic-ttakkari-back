from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ttakkari.config import get_settings
from ttakkari.host_resource import LeaseBusy
from ttakkari.managed_comfy import ComfyManagedController, default_controller
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/comfy", tags=["managed-comfy"])


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=1000)
    seed: int = Field(ge=0, le=2**32 - 1)


def _controller() -> ComfyManagedController:
    return default_controller(get_settings().data_dir)


def _public(job: dict) -> dict:
    result = job.get("result") or {}
    return {
        "task_id": job["task_id"], "profile": job["profile"], "status": job["status"],
        "runner_job_id": job.get("runner_job_id"), "error": job.get("error"),
        "result": {k: result[k] for k in ("id", "status", "seconds", "failure", "files", "pending_files")
                   if k in result},
    }


@router.get("/profiles")
async def profiles(_: User) -> list[dict]:
    return _controller().profiles()


@router.post("/jobs", status_code=202)
async def submit(body: Submit, _: User) -> dict:
    service = _controller()
    try:
        job = service.prepare(body.profile_id, prompt=body.prompt, seed=body.seed)
        service.start(job["task_id"])
        return _public(job)
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
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
        return _public(await asyncio.to_thread(_controller().cancel, task_id, owner_task=task_id))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/jobs/{task_id}/reconcile")
async def reconcile(task_id: str, _: User) -> dict:
    try:
        return _public(await asyncio.to_thread(_controller().reconcile, task_id, owner_task=task_id))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc
