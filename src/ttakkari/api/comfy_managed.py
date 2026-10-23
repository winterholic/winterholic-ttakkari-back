from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ttakkari.api.deps import Db
from ttakkari.config import get_settings
from ttakkari.host_resource import LeaseBusy
from ttakkari.managed_comfy import (
    COMFY_ROOT,
    ComfyManagedController,
    default_controller,
)
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/comfy", tags=["managed-comfy"])
agent_router = APIRouter(prefix="/api/agent/comfy", tags=["managed-comfy-agent"])


async def agent_access(sid: OrchestratorSession, db: Db) -> str:
    row = await db.get(ChatSession, sid)
    if row is None:
        raise HTTPException(403, "세션이 없습니다.")
    if row.pool == "work":
        raise HTTPException(403, "개인 세션에서만 managed Comfy를 사용할 수 있습니다.")
    return f"agent:{sid}"


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=1000)
    seed: int = Field(ge=0, le=2**32 - 1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)


# 자동 소재 생성(creative-v2)이 만든 작업은 receipt 의 task_id 로 개인 세션이 조회할 수 있어야 한다. 읽기 전용이며
# 취소·reconcile 은 계속 소유자 일치를 요구한다.
READABLE_OWNER_PREFIXES = ("creative-v2:",)


def _controller() -> ComfyManagedController:
    return default_controller(get_settings().data_dir)


def _public(job: dict) -> dict:
    result = job.get("result") or {}
    return {
        "task_id": job["task_id"], "profile": job["profile"], "status": job["status"],
        "runner_job_id": job.get("runner_job_id"), "error": job.get("error"),
        "receipt": job.get("receipt"), "receipt_error": job.get("receipt_error"),
        "progress": job.get("progress"),
        "result": {k: result[k] for k in ("id", "status", "seconds", "failure", "files", "pending_files")
                   if k in result},
    }


def _agent_public(job: dict) -> dict:
    """Keep receipts useful while excluding absolute host paths from agent-facing results."""
    result = _public(job)
    receipt = result.get("receipt")
    if isinstance(receipt, dict):
        receipt = {k: v for k, v in receipt.items() if k != "source_path"}
        outputs = []
        for item in receipt.get("outputs", []):
            if not isinstance(item, dict):
                continue
            outputs.append({k: v for k, v in item.items() if k != "source_path"})
        if "outputs" in receipt:
            receipt["outputs"] = outputs
        result["receipt"] = receipt
        result["asset_ids"] = [
            item.get("sha256") for item in outputs if isinstance(item.get("sha256"), str)
        ]
    files = (result.get("result") or {}).get("files")
    if isinstance(files, list):
        result["result"]["files"] = [
            {k: v for k, v in item.items() if k not in {"path", "source_path"}}
            if isinstance(item, dict) else item for item in files
        ]
    if isinstance(result.get("error"), str):
        result["error"] = result["error"][:1000]
    job_result = result.get("result") or {}
    logs = []
    if result.get("error"):
        logs.append({"level": "error", "message": result["error"]})
    failure = job_result.get("failure")
    if isinstance(failure, dict):
        logs.append({"level": "error", **{k: str(v)[:500] for k, v in failure.items()
                                             if k in {"stage", "error", "error_type"}}})
    if result.get("receipt_error"):
        logs.append({"level": "warning", "message": str(result["receipt_error"])[:500]})
    result["logs"] = logs[:20]
    return result


@router.get("/profiles")
async def profiles(_: User) -> list[dict]:
    return _controller().profiles()


@router.post("/jobs", status_code=202)
async def submit(body: Submit, _: User) -> dict:
    service = _controller()
    try:
        job = service.prepare(body.profile_id, prompt=body.prompt, seed=body.seed,
                              idempotency_key=body.idempotency_key)
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


@agent_router.get("/profiles")
async def agent_profiles(_: str = Depends(agent_access)) -> list[dict]:
    return _controller().profiles()


def _host_readiness() -> dict:
    measured_at = datetime.now(UTC).isoformat()
    executable = COMFY_ROOT / "venv" / "bin" / "python"
    result = {
        "runtime": str(executable), "measured_at": measured_at, "source": "backend_host",
        "ready": False, "status": "missing_runtime", "device": None, "torch": None, "error": None,
    }
    if not executable.is_file():
        result["error"] = "configured Comfy Python runtime is missing"
        return result
    code = (
        "import json,torch; "
        "mps=bool(getattr(getattr(torch.backends,'mps',None),'is_available',lambda:False)()); "
        "cuda=bool(torch.cuda.is_available()); "
        "print(json.dumps({'torch':torch.__version__,'mps':mps,'cuda':cuda}))"
    )
    try:
        completed = subprocess.run(
            [str(executable), "-c", code], capture_output=True, text=True, timeout=8, check=False,
            cwd=str(COMFY_ROOT),
        )
    except subprocess.TimeoutExpired:
        result.update(status="probe_timeout", error="configured runtime device probe exceeded 8 seconds")
        return result
    except OSError as exc:
        result.update(status="probe_error", error=f"{type(exc).__name__}: {exc}")
        return result
    if completed.returncode != 0:
        result.update(status="probe_error", error=(completed.stderr or "runtime probe failed")[-1000:])
        return result
    try:
        measured = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result.update(status="probe_error", error="configured runtime returned invalid probe output")
        return result
    if (
        not isinstance(measured, dict) or not isinstance(measured.get("torch"), str)
        or not isinstance(measured.get("cuda"), bool) or not isinstance(measured.get("mps"), bool)
    ):
        result.update(status="probe_error", error="configured runtime returned incomplete device data")
        return result
    device = "cuda" if measured["cuda"] else "mps" if measured["mps"] else None
    result.update(
        ready=device is not None, status="ready" if device else "no_supported_device",
        device=device, torch=measured["torch"],
    )
    return result


@agent_router.get("/readiness")
async def agent_readiness(_: str = Depends(agent_access)) -> dict:
    """Run only a bounded torch device availability check in the configured Comfy Python; never load models."""
    return await asyncio.to_thread(_host_readiness)


@agent_router.get("/jobs")
async def agent_jobs(owner: str = Depends(agent_access), limit: int = Query(20, ge=1, le=100),
                     offset: int = Query(0, ge=0)) -> dict:
    page = _controller().list_jobs(owner=owner, limit=limit, offset=offset)
    page["items"] = [
        _agent_public(_controller().get(job["task_id"], owner=owner)) for job in page["items"]
    ]
    return page


@agent_router.post("/submit", status_code=202)
async def agent_submit(body: Submit, owner: str = Depends(agent_access)) -> dict:
    service = _controller()
    try:
        job = service.prepare(body.profile_id, prompt=body.prompt, seed=body.seed, owner=owner,
                              idempotency_key=body.idempotency_key)
        service.start(job["task_id"])
        return _agent_public(job)
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@agent_router.get("/jobs/{task_id}")
async def agent_status(task_id: str, owner: str = Depends(agent_access)) -> dict:
    try:
        return _agent_public(_controller().get(
            task_id, owner=owner, readable_owner_prefixes=READABLE_OWNER_PREFIXES,
        ))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@agent_router.post("/jobs/{task_id}/cancel")
async def agent_cancel(task_id: str, owner: str = Depends(agent_access)) -> dict:
    try:
        return _agent_public(await asyncio.to_thread(
            _controller().cancel, task_id, owner_task=task_id, owner=owner,
        ))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@agent_router.post("/jobs/{task_id}/reconcile")
async def agent_reconcile(task_id: str, owner: str = Depends(agent_access)) -> dict:
    try:
        return _agent_public(await asyncio.to_thread(
            _controller().reconcile, task_id, owner_task=task_id, owner=owner,
        ))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LeaseBusy as exc:
        raise HTTPException(409, str(exc)) from exc
