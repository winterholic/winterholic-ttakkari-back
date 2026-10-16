"""Fixed Luna high benchmark job for common30-v2; no caller supplied code or paths."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import statistics
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import EngineAccount
from ttakkari.runs import desk, profiles

ROOT = Path(__file__).resolve().parents[3]
BENCH = ROOT / "experiments/qwen35-router/common30-v2"
CASES = BENCH / "common30-v2.jsonl"
SYSTEM = BENCH / "system-prompt-v2.txt"
SCHEMA = BENCH / "luna-output-schema.json"
EXPECTED_CASES_SHA256 = "37b607c9679bba193f18578256a33f8477a3f36cf30e05283ae3ce32ac95b2b0"
EXPECTED_SYSTEM_SHA256 = "850eb694411d25fe0d51c3960fe778a63503a74c022becd8ef14701816b5c1bd"
REPETITIONS = 3
MODEL = "gpt-6-luna"
EFFORT = "high"
TIMEOUT_SECONDS = 180


def contract() -> dict[str, Any]:
    cases_bytes, system_bytes = CASES.read_bytes(), SYSTEM.read_bytes()
    if hashlib.sha256(cases_bytes).hexdigest() != EXPECTED_CASES_SHA256:
        raise RuntimeError("고정 common30-v2 데이터 해시가 manifest와 다릅니다.")
    if hashlib.sha256(system_bytes).hexdigest() != EXPECTED_SYSTEM_SHA256:
        raise RuntimeError("고정 v2 시스템 프롬프트 해시가 manifest와 다릅니다.")
    cases = [json.loads(line) for line in cases_bytes.decode().splitlines() if line.strip()]
    if len(cases) != 30:
        raise RuntimeError("common30-v2 문항 수가 30이 아닙니다.")
    return {
        "model": MODEL, "effort": EFFORT, "dataset_cases_n": len(cases),
        "repetitions": REPETITIONS, "requested_n": len(cases) * REPETITIONS,
        "dataset_sha256": EXPECTED_CASES_SHA256,
        "system_sha256": EXPECTED_SYSTEM_SHA256,
        "schema_sha256": hashlib.sha256(SCHEMA.read_bytes()).hexdigest(),
        "max_output_tokens": None,
        "output_limit_note": "Codex CLI 0.162.1에 지원되는 model_max_output_tokens 필드를 확인하지 못해 설정하지 않음.",
    }


def expand(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**case, "repetition": rep} for rep in range(1, REPETITIONS + 1) for case in cases]


def record_attempt(case: dict[str, Any], response: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    text, error = response.get("text"), response.get("error")
    elapsed = response.get("elapsed_ms")
    if error and "failed to initialize" in error.lower():
        row = {"case_id": case["case_id"], "repetition": case["repetition"],
               "execution_status": "initialization_failed", "scored": False,
               "valid": False, "exact_correct": None, "acceptable_set_correct": None,
               "elapsed_ms": elapsed, "error": error}
    elif error and "시간 초과" in error:
        row = {"case_id": case["case_id"], "repetition": case["repetition"],
               "execution_status": "timeout", "scored": False, "valid": False,
               "exact_correct": None, "acceptable_set_correct": None, "elapsed_ms": elapsed, "error": error}
    elif text is None:
        row = {"case_id": case["case_id"], "repetition": case["repetition"],
               "execution_status": "model_error", "scored": False, "valid": False,
               "exact_correct": None, "acceptable_set_correct": None, "elapsed_ms": elapsed, "error": error}
    else:
        row = _score(case, text, elapsed)
    rows.append(row)
    return row


def _score(case: dict[str, Any], raw: str, elapsed_ms: int | None) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
        schema = json.loads(SCHEMA.read_text())
        valid = isinstance(obj, dict) and set(obj) == set(schema["required"])
        valid = valid and obj.get("action") in {"answer", "delegate", "abstain"}
        valid = valid and isinstance(obj.get("reply"), str) and bool(obj["reply"].strip())
        valid = valid and all(obj.get(k) == [] for k in ("tool_ids", "permission_ids"))
        valid = valid and obj.get("lead_id") is None and obj.get("model_id") is None
        valid = valid and (obj.get("abstain_reason") in {"ambiguous", "insufficient_context", "unsafe"}
                           if obj.get("action") == "abstain" else obj.get("abstain_reason") is None)
    except (ValueError, KeyError, TypeError):
        obj, valid = None, False
    actual = obj.get("action") if valid else None
    expected = case["gold_status"]
    hard = expected != "hold"
    return {
        "case_id": case["case_id"], "repetition": case["repetition"], "expected_status": expected,
        "allowed_actions": case["allowed_actions"], "execution_status": "completed", "scored": True,
        "elapsed_ms": elapsed_ms, "raw_output": raw, "output": obj, "valid": valid,
        "exact_correct": actual == expected if hard else None,
        "acceptable_set_correct": actual in case["allowed_actions"] if valid else False,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [r for r in rows if r.get("execution_status") == "completed" and r.get("scored")]
    times = [r["elapsed_ms"] for r in completed if isinstance(r.get("elapsed_ms"), (int, float))]
    hard = [r for r in completed if r.get("expected_status") != "hold"]
    actions: dict[str, list[str]] = {}
    for row in completed:
        action = (row.get("output") or {}).get("action")
        if row.get("valid") and action:
            actions.setdefault(row["case_id"], []).append(action)
    analyzed = [cid for cid, values in actions.items() if len(values) == REPETITIONS]
    unstable = sorted(cid for cid in analyzed if len(set(actions[cid])) > 1)
    return {
        "status": "completed" if len(rows) == 90 else "incomplete",
        "attempted_n": len(rows), "model_response_n": len(completed),
        "valid_n": sum(bool(r.get("valid")) for r in completed),
        "hard_exact_n": len(hard),
        "hard_exact_denominator": 87,
        "hard_exact_correct": sum(r.get("exact_correct") is True for r in hard),
        "acceptable_n": len(completed), "acceptable_denominator": 90,
        "acceptable_correct": sum(r.get("acceptable_set_correct") is True for r in completed),
        "latency_p50_ms": round(statistics.median(times), 2) if times else None,
        "latency_p95_ms": sorted(times)[max(0, math.ceil(.95 * len(times)) - 1)] if times else None,
        "initialization_failures": sum(r.get("execution_status") == "initialization_failed" for r in rows),
        "timeouts": sum(r.get("execution_status") == "timeout" for r in rows),
        "model_errors": sum(r.get("execution_status") == "model_error" for r in rows),
        "unstable_case_count": len(unstable), "unstable_case_ids": unstable,
    }


@dataclass
class Job:
    id: uuid.UUID
    owner: str
    generation_id: uuid.UUID
    profile_id: uuid.UUID
    status: str = "queued"
    rows: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task | None = None
    checkpoint_path: Path | None = None
    lock_handle: Any = None

    @property
    def cancel_path(self) -> Path | None:
        return self.checkpoint_path.with_suffix(".cancel") if self.checkpoint_path else None

    def view(self, offset: int = 0, limit: int = 10) -> dict[str, Any]:
        return {"job_id": str(self.id), "status": self.status, "contract": contract(),
                "rows_offset": offset, "total_rows": len(self.rows),
                "rows": self.rows[offset:offset + limit], "summary": self.summary}

    def checkpoint(self) -> None:
        if self.checkpoint_path is None:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.checkpoint_path.parent, 0o700)
        temp = self.checkpoint_path.with_suffix(".tmp")
        value = {
            "job_id": str(self.id), "status": self.status, "contract": contract(),
            "rows_offset": 0, "total_rows": len(self.rows), "rows": self.rows,
            "summary": self.summary, "owner": self.owner,
            "generation_id": str(self.generation_id), "profile_id": str(self.profile_id),
        }
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temp, 0o600)
        temp.replace(self.checkpoint_path)


_job: Job | None = None
_lock = asyncio.Lock()


def _checkpoint_path(job_id: uuid.UUID) -> Path:
    return get_settings().home / "benchmarks" / f"luna-common30-v2-{job_id}.json"


def _acquire_singleton():
    path = get_settings().home / "runtime" / "luna-common30-v2.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    handle = path.open("a")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _release_singleton(job: Job) -> None:
    if job.lock_handle is not None:
        job.lock_handle.close()
        job.lock_handle = None


async def start(owner: str, db) -> Job:
    global _job
    async with _lock:
        if _job and _job.status in {"queued", "running"}:
            raise RuntimeError("common30-v2 Luna 평가가 이미 실행 중입니다.")
        runtime = generation.current
        if runtime is None or runtime.state != "active":
            raise RuntimeError("현재 활성 generation에서만 벤치를 시작할 수 있습니다.")
        contract()
        candidates, _ = await profiles.availability(db, "codex", "personal")
        if not candidates:
            raise RuntimeError("사용 가능한 개인 Codex 계정이 없습니다.")
        prof = candidates[0]
        handle = _acquire_singleton()
        if handle is None:
            raise RuntimeError("다른 generation에서 common30-v2 Luna 평가가 실행 중입니다.")
        job_id = uuid.uuid4()
        _job = Job(job_id, owner, runtime.id, prof.id, checkpoint_path=_checkpoint_path(job_id), lock_handle=handle)
        try:
            _job.checkpoint()
            _job.task = asyncio.create_task(_run(_job, prof))
        except BaseException:
            _job.status = "failed_start"
            try:
                _job.checkpoint()
            finally:
                _release_singleton(_job)
            raise
        return _job


async def _run(job: Job, prof: profiles.Profile) -> None:
    try:
        runtime = generation.current
        if runtime is None or runtime.id != job.generation_id:
            job.status = "cancelled_generation_change"
            job.checkpoint()
            return
        async with runtime.work(f"luna-bench-{job.id.hex}") as acquired:
            if not acquired:
                job.status = "cancelled_generation_change"
                job.checkpoint()
                return
            await _run_owned(job, prof)
    except asyncio.CancelledError:
        if job.status in {"queued", "running"}:
            job.status = "cancelled"
        raise
    except Exception:
        if job.status in {"queued", "running"}:
            job.status = "failed"
        raise
    finally:
        if job.status in {"queued", "running"}:
            job.status = "failed"
        try:
            job.checkpoint()
        finally:
            _release_singleton(job)


async def _run_owned(job: Job, prof: profiles.Profile) -> None:
    job.status = "running"
    job.checkpoint()
    fixed = contract()
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    system = SYSTEM.read_text()
    try:
        for case in expand(cases):
            runtime = generation.current
            if runtime is None or runtime.id != job.generation_id or runtime.state not in {"active", "draining"}:
                job.status = "cancelled_generation_change"
                break
            if job.cancel_path and job.cancel_path.exists():
                job.status = "cancelled"
                break
            async with sessionmaker()() as db:
                account = await db.scalar(select(EngineAccount).where(EngineAccount.id == prof.id))
            if account is None or account.engine != "codex" or account.pool != "personal" or not account.enabled:
                job.status = "cancelled_account_unavailable"
                break
            context = (f"[작업판]\n{case['context']['board']}\n[최근 대화]\n{case['context']['history']}\n"
                       f"[사용자 메시지]\n{case['message']}")
            request = {"candidates": {"lead_ids": [], "model_ids": [], "tool_ids": [], "permission_ids": []},
                       "request": context}
            prompt = (system + "\n\n분류할 입력은 다음 JSON 하나다. 도구·파일·네트워크를 사용하지 말고 라우팅만 판정해라. "
                      "출력은 지정된 JSON Schema에 맞춰라.\n" +
                      json.dumps(request, ensure_ascii=False, separators=(",", ":")))
            started = time.perf_counter()
            attempt: asyncio.Task | None = None
            try:
                attempt = asyncio.create_task(desk._ask(
                    prof, system, prompt, cfg_override={"model": MODEL, "effort": EFFORT},
                    timeout_seconds=TIMEOUT_SECONDS, use_warm=False, output_schema=SCHEMA))
                while not attempt.done():
                    await asyncio.wait({attempt}, timeout=1)
                    current = generation.current
                    if job.cancel_path and job.cancel_path.exists():
                        attempt.cancel()
                        await asyncio.gather(attempt, return_exceptions=True)
                        job.status = "cancelled"
                        job.rows.append({"case_id": case["case_id"], "repetition": case["repetition"],
                                         "execution_status": "cancelled", "scored": False,
                                         "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
                        job.checkpoint()
                        return
                    if current is None or current.id != job.generation_id or current.state not in {"active", "draining"}:
                        attempt.cancel()
                        await asyncio.gather(attempt, return_exceptions=True)
                        job.status = "cancelled_generation_change"
                        job.rows.append({"case_id": case["case_id"], "repetition": case["repetition"],
                                         "execution_status": "cancelled", "scored": False,
                                         "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
                        job.checkpoint()
                        return
                response = await attempt
                response_obj = {"text": response.text, "error": response.error,
                                "elapsed_ms": response.elapsed_ms}
            except asyncio.CancelledError:
                if attempt is not None and not attempt.done():
                    attempt.cancel()
                    await asyncio.gather(attempt, return_exceptions=True)
                job.rows.append({"case_id": case["case_id"], "repetition": case["repetition"],
                                 "execution_status": "cancelled", "scored": False,
                                 "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
                job.status = "cancelled"
                job.checkpoint()
                raise
            except Exception as exc:  # noqa: BLE001 — isolate one model attempt and checkpoint it
                response_obj = {"text": None, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
            row = record_attempt(case, response_obj, job.rows)
            row.update({"model": MODEL, "reasoning_effort": EFFORT,
                        "input_sha256": hashlib.sha256(context.encode()).hexdigest(),
                        "system_sha256": fixed["system_sha256"],
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
            job.summary = summarize(job.rows)
            job.checkpoint()
            if row["execution_status"] == "initialization_failed":
                job.status = "failed_initialization"
                break
        else:
            job.status = "completed"
        job.summary.update(summarize(job.rows))
        job.summary.update({"model": MODEL, "reasoning_effort": EFFORT, "max_output_tokens": None,
                            "dataset_sha256": fixed["dataset_sha256"], "system_sha256": fixed["system_sha256"]})
        job.checkpoint()
    finally:
        if job.status == "running":
            job.status = "failed"
        _release_singleton(job)
        if job.cancel_path:
            job.cancel_path.unlink(missing_ok=True)


def get_job(job_id: uuid.UUID, owner: str) -> Job | None:
    if _job and _job.id == job_id and _job.owner == owner:
        return _job
    path = _checkpoint_path(job_id)
    try:
        data = json.loads(path.read_text())
        if data.get("owner") != owner or data.get("job_id") != str(job_id):
            return None
        return Job(job_id, owner, uuid.UUID(data["generation_id"]), uuid.UUID(data["profile_id"]),
                   status=data["status"], rows=data.get("rows", []), summary=data.get("summary", {}),
                   checkpoint_path=path)
    except (OSError, ValueError, KeyError, TypeError):
        return None


async def cancel(job: Job) -> None:
    if job.status not in {"queued", "running"}:
        return
    if job.cancel_path:
        job.cancel_path.write_text("cancel\n")
        os.chmod(job.cancel_path, 0o600)
    if job.task and not job.task.done():
        job.task.cancel()
        try:
            await job.task
        except asyncio.CancelledError:
            pass
    else:
        job.status = "cancel_requested"
        job.checkpoint()


async def shutdown() -> None:
    if _job and _job.status in {"queued", "running"}:
        await cancel(_job)
