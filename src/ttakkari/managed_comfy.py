"""Managed entry point for one pinned Comfy workflow on this host.

The workflow registry and Runner remain owned by the Lab adapter. This module
only binds an authenticated task to that fixed profile and the shared host
resource lease.
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import psutil

from ttakkari.host_gpu_guard import ProcState, process_state
from ttakkari.host_resource import (
    LeaseBusy,
    LeaseError,
    ResourceLeaseHandle,
    ResourceLeaseManager,
)
from ttakkari.managed_comfy_receipt import ReceiptError, build_receipt, safe_rss_gb

LAB_ROOT = Path("/Users/winterholic/development/projects/ttakkari-studio-lab")
COMFY_ROOT = LAB_ROOT / "comfyui"
ADAPTER_ROOT = COMFY_ROOT / "adapter"
REGISTRY_ROOT = COMFY_ROOT / "registry"
T2V_5B_REVISION = "a603a63d6f3a"
COMFY_PORT = 8199


@dataclass(frozen=True)
class ComfyProfile:
    profile_id: str
    workflow: str
    revision: str
    preset: str
    required_gb: float
    models: tuple[str, ...]


PROFILES = {
    "wan22-5b-draft": ComfyProfile(
        profile_id="wan22-5b-draft", workflow="t2v-wan22-5b", revision=T2V_5B_REVISION,
        preset="draft", required_gb=16, models=("wan2.2_ti2v_5B_fp16.safetensors",
                                                  "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                                                  "wan2.2_vae.safetensors"),
    ),
}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


class ManagedClient:
    """Narrow lease-aware wrapper; ordinary Comfy methods remain adapter-owned."""

    def __init__(self, client, queue_item_owned: Callable[[dict, dict, dict], bool],
                 manager: ResourceLeaseManager, lease: ResourceLeaseHandle, server) -> None:
        self._client = client
        self._queue_item_owned = queue_item_owned
        self._manager = manager
        self._lease = lease
        self._server = server

    def __getattr__(self, key):
        return getattr(self._client, key)

    def interrupt_owned(self, job: dict, attempt: dict) -> None:
        record = self._manager.current()
        proc = self._server.proc
        if (not record or record.get("lease_id") != self._lease.lease_id
                or record.get("generation_token") != self._lease.generation_token
                or record.get("runtime_instance") != self._lease.runtime_instance
                or record.get("phase") != "cancel_requested" or not self._server.owned
                or proc is None or proc.poll() is not None):
            raise LeaseError("running cancel lacks the live exclusive runtime lease")
        queue = self._client.queue()
        running = queue.get("queue_running", [])
        if (len(running) != 1
                or not self._queue_item_owned(running[0], job, attempt)):
            raise LeaseError("running prompt is not the sole prompt owned by this runtime")
        self._client._req("POST", "/interrupt", {"prompt_id": attempt["external_prompt_id"]})


class ComfyManagedController:
    def __init__(self, *, data_dir: Path,
                 runtime_factory: Callable | None = None,
                 spawn: Callable | None = None, port: int = COMFY_PORT) -> None:
        self.data_dir = Path(data_dir)
        self.port = port
        self.manager = ResourceLeaseManager(data_dir=self.data_dir)
        self.jobs_dir = self.data_dir / "managed-comfy" / "tasks"
        self.runtime_factory = runtime_factory or self._runtime_factory
        self.spawn = spawn or self._spawn
        self._active: dict[str, dict[str, Any]] = {}
        self._started: set[str] = set()
        self._lock = threading.RLock()

    def _path(self, task_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise FileNotFoundError("managed Comfy task not found")
        return self.jobs_dir / f"{task_id}.json"

    def _load(self, task_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError("managed Comfy task not found") from exc

    def _save(self, job: dict[str, Any]) -> None:
        _write_json(self._path(job["task_id"]), job)

    def profiles(self) -> list[dict[str, Any]]:
        return [{
            "id": p.profile_id, "workflow": p.workflow, "revision": p.revision, "preset": p.preset,
            "capabilities": ["gen.video_t2v"], "required_gb": p.required_gb, "models": list(p.models),
        } for p in PROFILES.values()]

    def prepare(self, profile_id: str, *, prompt: str, seed: int, negative_prompt: str | None = None,
                owner: str = "owner",
                idempotency_key: str | None = None, lease_wait_seconds: float = 0,
                **extra: Any) -> dict[str, Any]:
        if extra:
            raise ValueError("only registered profile parameters prompt, negative_prompt, and seed are accepted")
        profile = PROFILES.get(profile_id)
        if profile is None:
            raise ValueError("unknown registered profile")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1000:
            raise ValueError("prompt must contain 1 to 1000 characters")
        if negative_prompt is not None and (
            not isinstance(negative_prompt, str) or not negative_prompt.strip() or len(negative_prompt) > 1000
        ):
            raise ValueError("negative_prompt must contain 1 to 1000 characters when provided")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise ValueError("seed must be a 32-bit unsigned integer")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 120
        ):
            raise ValueError("idempotency_key must contain 8 to 120 characters")
        task_id = (
            uuid.uuid5(uuid.NAMESPACE_URL, f"managed-comfy:{owner}:{idempotency_key}").hex
            if idempotency_key else uuid.uuid4().hex
        )
        if idempotency_key:
            try:
                existing = self._load(task_id)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if (existing.get("profile"), existing.get("prompt"), existing.get("negative_prompt"),
                        existing.get("seed")) != (
                    profile_id, prompt, negative_prompt, seed,
                ):
                    raise ValueError("idempotency key was already used with different task arguments")
                return existing
        attempt_id = uuid.uuid4().hex
        runtime_instance = uuid.uuid4().hex
        lease = self.manager.acquire(
            owner_task=task_id, owner_attempt=attempt_id, runtime_instance=runtime_instance,
            resource_family="comfy", claim={"profile": profile.profile_id, "workflow": profile.workflow,
                                             "revision": profile.revision, "models": list(profile.models)},
            ttl_seconds=45, wait_seconds=lease_wait_seconds,
        )
        job = {"task_id": task_id, "owner": owner, "owner_attempt": attempt_id,
               "runtime_instance": runtime_instance, "lease_id": lease.lease_id,
               "generation_token": lease.generation_token, "profile": profile.profile_id,
               "prompt": prompt, "negative_prompt": negative_prompt, "seed": seed,
               "status": "starting", "runner_job_id": None,
               "result": None, "error": None, "created_at": time.time()}
        try:
            self._save(job)
        except BaseException:
            lease.release(proof={"runtime_status": "stopped", "evidence": {
                "runtime_instance": runtime_instance, "runtime_spawned": False,
            }})
            raise
        with self._lock:
            active = {"lease": lease, "server": None, "runner": None, "client": None,
                      "progress": None,
                      "profile": profile, "heartbeat_error": None, "lease_guard": threading.RLock(),
                      "heartbeat_stop": threading.Event(), "cancel_stop": threading.Event(),
                      "cancel_guard": threading.Lock(), "cancel_interrupt_sent": False,
                      "cancel_result": None, "stop_result": None, "stop_proc": None}
            self._active[task_id] = active
            active["heartbeat_thread"] = threading.Thread(
                target=self._heartbeat, args=(active,), daemon=True, name="comfy-lease-heartbeat")
            active["heartbeat_thread"].start()
        return dict(job)

    @staticmethod
    def _heartbeat(active: dict[str, Any]) -> None:
        lease: ResourceLeaseHandle = active["lease"]
        while not active["heartbeat_stop"].wait(10):
            try:
                with active["lease_guard"]:
                    lease.heartbeat(ttl_seconds=45)
            except Exception as exc:  # noqa: BLE001 -- 어떤 실패에도 lease를 조기 해제하지 않고 reconcile 상태로 남긴다
                active["heartbeat_error"] = f"{type(exc).__name__}: {exc}"
                return

    @staticmethod
    def _spawn(fn: Callable, *args: Any) -> None:
        threading.Thread(target=fn, args=args, daemon=True,
                         name="managed-comfy").start()

    def start(self, task_id: str) -> dict[str, Any]:
        job = self._load(task_id)
        with self._lock:
            if job["status"] != "starting" or task_id in self._started:
                return job
            self._started.add(task_id)
        try:
            self.spawn(self.execute, task_id)
        except BaseException:
            with self._lock:
                self._started.discard(task_id)
            raise
        return job

    def _runtime_factory(self, profile: ComfyProfile, runtime_instance: str,
                         data_dir: Path, lease: ResourceLeaseHandle, _controller):
        if str(ADAPTER_ROOT) not in sys.path:
            sys.path.insert(0, str(ADAPTER_ROOT))
        server_module = importlib.import_module("ttk_comfy.server")
        client_module = importlib.import_module("ttk_comfy.client")
        runner_module = importlib.import_module("ttk_comfy.runner")
        workflow_module = importlib.import_module("ttk_comfy.workflow")
        registry = workflow_module.Registry(root=REGISTRY_ROOT)
        api, _meta, _hash = registry.load(profile.workflow, profile.revision)
        if workflow_module.graph_hash(api) != profile.revision:
            raise ValueError("registered workflow revision does not match the managed profile")
        server = server_module.ComfyServer(
            port=_controller.port,
            output_dir=data_dir / "managed-comfy" / "runtime-output",
            log_path=data_dir / "managed-comfy" / "runtime.log",
            env={"TTAKKARI_COMFY_RUNTIME_INSTANCE": runtime_instance,
                 "TTAKKARI_COMFY_PROFILE": profile.profile_id},
        )
        def queue_item_owned(entry, saved_job, attempt):
            return runner_module.Runner._queue_item_owned(entry, saved_job, attempt)

        return {"server": server, "runner_cls": runner_module.Runner,
                "registry_cls": workflow_module.Registry, "client_cls": client_module.ComfyClient,
                "queue_item_owned": queue_item_owned}

    def execute(self, task_id: str) -> dict[str, Any]:
        job = self._load(task_id)
        with self._lock:
            active = self._active.get(task_id)
        if not active:
            return job
        lease: ResourceLeaseHandle = active["lease"]
        profile: ComfyProfile = active["profile"]
        if active.get("heartbeat_error"):
            job["status"] = "needs_reconcile"
            job["error"] = active["heartbeat_error"]
            self._save(job)
            return job
        server = None
        try:
            runtime = self.runtime_factory(profile, lease.runtime_instance, self.data_dir, lease, self)
            runtime_is_real = not isinstance(runtime, tuple)
            if isinstance(runtime, tuple):
                server, runner = runtime
                client = getattr(server, "client", None)
            else:
                server = runtime["server"]
                runner_cls = runtime["runner_cls"]
                registry_cls = runtime["registry_cls"]
                client_cls = runtime["client_cls"]
                queue_item_owned = runtime["queue_item_owned"]
                client = client_cls(f"http://127.0.0.1:{self.port}")
                runner = None
                active["queue_item_owned"] = queue_item_owned
                active["runner_cls"] = runner_cls
                active["registry_cls"] = registry_cls
            active.update({"server": server, "client": client, "runner": runner})
            if client is not None and client.alive():
                raise LeaseBusy("an unmanaged Comfy server already owns the fixed runtime port")
            server.start(required_gb=profile.required_gb)
            if not server.owned:
                raise LeaseBusy("Comfy server was not started by this managed runtime")
            proc = server.proc
            if proc is None:
                raise LeaseBusy("managed Comfy process identity is missing")
            if runtime_is_real:
                job["runtime_pid"] = proc.pid
                job["runtime_created_at"] = psutil.Process(proc.pid).create_time()
                self._save(job)
            queue = client.queue()
            if queue.get("queue_running") or queue.get("queue_pending"):
                raise LeaseBusy("newly managed Comfy runtime queue is not empty")
            if runner is None:
                managed_client = ManagedClient(client, active["queue_item_owned"], self.manager, lease, server)
                server.client = managed_client
                runner = active["runner_cls"](
                    managed_client, active["registry_cls"](root=REGISTRY_ROOT),
                    jobs_dir=self.data_dir / "managed-comfy" / "jobs",
                    rss_probe=getattr(server, "rss_gb", None),
                )
                active["client"] = managed_client
                active["runner"] = runner
            checked = runner.check(profile.workflow, version=profile.revision)
            if not checked.get("ok"):
                raise ValueError(f"registered profile failed Comfy validation: {checked}")
            if self.manager.current().get("phase") == "cancel_requested":
                job["status"] = "cancelled"
                self._save(job)
                return self._stop_release(task_id, job, active)
            with active["lease_guard"]:
                lease.mark_running()
            active["cancel_thread"] = threading.Thread(
                target=self._watch_cancel, args=(task_id, active), daemon=True, name="comfy-cancel-watch")
            active["cancel_thread"].start()
            job["status"] = "running"
            self._save(job)
            def on_event(event: dict[str, Any]) -> None:
                phase = event.get("phase")
                active["progress"] = {
                    key: event[key]
                    for key in ("phase", "node", "class", "value", "max", "pct", "t")
                    if key in event
                }
                if phase == "done":
                    active["progress"].update(phase="complete", pct=100)
                self._maybe_cancel(task_id, active)

            params = {"prompt": job["prompt"], "seed": job["seed"]}
            if job.get("negative_prompt") is not None:
                params["negative"] = job["negative_prompt"]
            result = runner.run(name=profile.workflow, version=profile.revision,
                                preset=profile.preset, params=params,
                                retries=0, tag=task_id,
                                on_event=on_event)
            if active.get("released"):
                return self.get(task_id)
            job["runner_job_id"] = result.get("id")
            job["result"] = result
            job["status"] = result.get("status", "needs_reconcile")
            if job["status"] == "succeeded":
                active["progress"] = {"phase": "complete", "pct": 100}
            if (active.get("cancel_result") or {}).get("status") == "cancelled":
                job["status"] = "cancelled"
            elif active.get("cancel_interrupt_sent") and self._interrupted(result):
                # HTTP 폴링 관측은 interrupt를 execution_error로 올리므로, 우리가 보낸 interrupt의
                # execution_interrupted 증거가 있을 때만 취소로 본다.
                job["status"] = "cancelled"
            if job["status"] == "failed" and (result.get("failure") or {}).get("stage") == "download":
                job["status"] = "needs_reconcile"
                job["error"] = "download_incomplete; retry collection through Runner.reconcile"
            self._attach_receipt(job, active)
            self._save(job)
            if job["status"] == "needs_reconcile":
                return job
            return self._stop_release(task_id, job, active)
        except Exception as exc:  # noqa: BLE001 -- 어떤 실패에도 lease를 조기 해제하지 않고 reconcile 상태로 남긴다
            # Only release if we can prove that this attempt never spawned a live runtime.
            if server is None:
                self._release_never_started(task_id, job, active)
                job["status"] = "failed"
                job["error"] = f"{type(exc).__name__}: {exc}"
                self._save(job)
                return job
            if not getattr(server, "owned", False):
                if getattr(server, "proc", None) is None and self._is_comfy_port_alive() is False:
                    self._release_never_started(task_id, job, active)
                    job["status"] = "failed"
                    job["error"] = f"{type(exc).__name__}: {exc}"
                    self._save(job)
                    return job
                job["status"] = "needs_reconcile"
                job["error"] = f"{type(exc).__name__}: {exc}"
                self._save(job)
                return job
            job["status"] = "needs_reconcile"
            job["error"] = f"{type(exc).__name__}: {exc}; owned runtime state requires reconciliation"
            self._save(job)
            return job
        finally:
            if active.get("heartbeat_error"):
                job = self._load(task_id)
                job["status"] = "needs_reconcile"
                job["error"] = active["heartbeat_error"]
                self._save(job)

    def _attach_receipt(self, job: dict[str, Any], active: dict[str, Any]) -> None:
        """성공한 실행에만 receipt 를 붙인다. receipt 실패는 job 결과를 바꾸지 않고 사유만 남긴다."""
        if job.get("status") != "succeeded" or job.get("receipt"):
            return
        try:
            server = active.get("server")
            job["receipt"] = build_receipt(
                job=job, adapter_job=job["result"], profile=active["profile"], comfy_root=COMFY_ROOT,
                jobs_root=self.data_dir / "managed-comfy" / "jobs",
                rss_at_finish_gb=safe_rss_gb(getattr(server, "rss_gb", None)))
            job.pop("receipt_error", None)
        except (ReceiptError, OSError, KeyError, TypeError, ValueError) as exc:
            job["receipt_error"] = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _interrupted(result: dict[str, Any]) -> bool:
        messages = (result.get("failure") or {}).get("messages") or []
        return any(isinstance(m, (list, tuple)) and m and m[0] == "execution_interrupted"
                   for m in messages)

    def _stop_release(self, task_id: str, job: dict[str, Any], active: dict[str, Any]) -> dict[str, Any]:
        if active.get("released"):
            return self.get(task_id)
        server = active["server"]
        lease: ResourceLeaseHandle = active["lease"]
        if active.get("stop_result"):
            stopped = active["stop_result"]
            proc = active.get("stop_proc")
        else:
            if server is None or not server.owned:
                raise LeaseBusy("managed runtime ownership cannot be confirmed")
            proc = server.proc
            if proc is None or proc.poll() is not None:
                raise LeaseBusy("owned runtime process already exited; process group stop is unverified")
            stopped = server.stop()
            active["stop_result"] = stopped
            active["stop_proc"] = proc
        if not stopped.get("stopped") or active["client"].alive():
            raise LeaseBusy("managed Comfy runtime stop was not confirmed")
        proof = {"runtime_status": "stopped", "evidence": {
            "runtime_instance": lease.runtime_instance, "pid": stopped.get("pid"),
            "stop_result": stopped, "server_health_after_stop": False,
            "process_exit_code": getattr(proc, "returncode", None),
        }}
        with active["lease_guard"]:
            lease.release(proof=proof)
        active["released"] = True
        active["cancel_stop"].set()
        cancel_thread = active.get("cancel_thread")
        if cancel_thread:
            cancel_thread.join(timeout=1)
        active["heartbeat_stop"].set()
        active["heartbeat_thread"].join(timeout=1)
        job["status"] = job.get("status") if job.get("status") != "running" else "failed"
        job["lease_release_proof"] = proof
        job["released_at"] = time.time()
        self._save(job)
        with self._lock:
            self._active.pop(task_id, None)
        return job

    def _release_never_started(self, task_id: str, job: dict[str, Any], active: dict[str, Any]) -> None:
        lease: ResourceLeaseHandle = active["lease"]
        proof = {"runtime_status": "stopped", "evidence": {
            "runtime_instance": lease.runtime_instance, "runtime_spawned": False,
            "fixed_port_health": False,
        }}
        with active["lease_guard"]:
            lease.release(proof=proof)
        active["released"] = True
        active["heartbeat_stop"].set()
        active["heartbeat_thread"].join(timeout=1)
        job["lease_release_proof"] = proof
        job["released_at"] = time.time()
        with self._lock:
            self._active.pop(task_id, None)

    def _is_comfy_port_alive(self) -> bool | None:
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/system_stats", timeout=1):
                return True
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) == errno.ECONNREFUSED:
                return False
            return None
        except OSError as exc:
            return False if exc.errno == errno.ECONNREFUSED else None

    def _owned_runtime_absent(self, job: dict[str, Any]) -> bool | None:
        pid, created = job.get("runtime_pid"), job.get("runtime_created_at")
        if not isinstance(pid, int) or not isinstance(created, (int, float)):
            return None
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - float(created)) > 0.01:
                absent = True
            else:
                if process.status() == psutil.STATUS_ZOMBIE:
                    return self._is_comfy_port_alive() is False
                env = process.environ()
                argv = process.cmdline()
                cwd = Path(process.cwd())
                if (env.get("TTAKKARI_COMFY_RUNTIME_INSTANCE") != job["runtime_instance"]
                        or env.get("TTAKKARI_COMFY_PROFILE") != job["profile"]
                        or cwd != COMFY_ROOT / "ComfyUI" or len(argv) < 2 or argv[1] != "main.py"
                        or Path(process.exe()) != COMFY_ROOT / "venv" / "bin" / "python"):
                    return None
                return False
        except psutil.NoSuchProcess:
            absent = True
        except (psutil.AccessDenied, OSError):
            return None
        return True if absent and self._is_comfy_port_alive() is False else (None if absent else False)

    def list_jobs(self, *, owner: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        if isinstance(limit, bool) or not 1 <= limit <= 100 or isinstance(offset, bool) or offset < 0:
            raise ValueError("limit must be between 1 and 100 and offset must be non-negative")
        if not self.jobs_dir.exists():
            return {"items": [], "total_count": 0, "offset": offset, "has_more": False, "next_offset": None}
        rows = []
        for path in sorted(self.jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("owner") == owner:
                rows.append(job)
        page = rows[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(rows) else None
        return {"items": page, "total_count": len(rows), "offset": offset,
                "has_more": next_offset is not None, "next_offset": next_offset}

    def get(self, task_id: str, *, owner: str | None = None,
            readable_owner_prefixes: tuple[str, ...] = ()) -> dict[str, Any]:
        """owner 가 일치하거나, 읽기 전용으로 공개한 소유자 접두사(예: 자동 생성 작업)에 속하면 돌려준다."""
        job = self._load(task_id)
        if owner is not None and job.get("owner") != owner and not any(
            isinstance(job.get("owner"), str) and job["owner"].startswith(prefix)
            for prefix in readable_owner_prefixes
        ):
            raise FileNotFoundError("managed Comfy task not found")
        with self._lock:
            active = self._active.get(task_id)
            if active and active.get("progress") is not None:
                job["progress"] = dict(active["progress"])
        return job

    def cancel(self, task_id: str, *, owner_task: str | None = None,
               owner: str | None = None) -> dict[str, Any]:
        job = self._load(task_id)
        if owner is not None and job.get("owner") != owner:
            raise FileNotFoundError("managed Comfy task not found")
        if owner_task is not None and owner_task != task_id:
            raise PermissionError("task owner does not match")
        with self._lock:
            active = self._active.get(task_id)
        if not active:
            current = self.manager.current()
            if not self._lease_record_matches(job, current):
                raise PermissionError("task owner does not match active lease")
            self.manager.request_cancel(current["lease_id"], current["generation_token"])
            job["status"] = "cancel_requested"
            self._save(job)
            return job
        lease: ResourceLeaseHandle = active["lease"]
        with active["lease_guard"]:
            lease.request_cancel()
        if active.get("runner") is None:
            job["status"] = "cancel_requested"
            self._save(job)
            return job
        result = self._maybe_cancel(task_id, active)
        if result is None:
            job["status"] = "cancel_requested"
            self._save(job)
            return job
        job["result"] = result
        job["status"] = result.get("status", "cancel_requested")
        if result.get("status") == "cancelled":
            return self._stop_release(task_id, job, active)
        self._save(job)
        return job

    def _watch_cancel(self, task_id: str, active: dict[str, Any]) -> None:
        while not active["cancel_stop"].wait(1):
            try:
                current = self.manager.current()
                if (current and current.get("lease_id") == active["lease"].lease_id
                        and current.get("generation_token") == active["lease"].generation_token
                        and current.get("phase") == "cancel_requested"):
                    self._maybe_cancel(task_id, active)
            except Exception as exc:  # noqa: BLE001 -- 어떤 실패에도 lease를 조기 해제하지 않고 reconcile 상태로 남긴다
                active["cancel_error"] = f"{type(exc).__name__}: {exc}"

    def _maybe_cancel(self, task_id: str, active: dict[str, Any]) -> dict[str, Any] | None:
        if active.get("cancel_interrupt_sent") or active.get("cancel_result"):
            return active.get("cancel_result")
        current = self.manager.current()
        lease: ResourceLeaseHandle = active["lease"]
        if (not current or current.get("lease_id") != lease.lease_id
                or current.get("owner_task") != lease.owner_task
                or current.get("owner_attempt") != lease.owner_attempt
                or current.get("generation_token") != lease.generation_token
                or current.get("phase") != "cancel_requested"):
            return None
        with active["cancel_guard"]:
            if active.get("cancel_interrupt_sent") or active.get("cancel_result"):
                return active.get("cancel_result")
            runner = active.get("runner")
            if runner is None:
                return None
            job = self._load(task_id)
            job_id = job.get("runner_job_id") or self._find_runner_job_id(task_id)
            if not job_id:
                return None
            result = runner.cancel(job_id)
            if result.get("status") == "cancel_requested":
                attempt = (result.get("attempts") or [{}])[-1]
                active["client"].interrupt_owned(result, attempt)
                active["cancel_interrupt_sent"] = True
            elif result.get("status") == "cancelled":
                server = active.get("server")
                proc = server.proc if server else None
                if not server or not server.owned or proc is None or proc.poll() is not None:
                    raise LeaseBusy("queued cancel lacks an owned live runtime")
                active["stop_proc"] = proc
                active["stop_result"] = server.stop()
                active["cancel_result"] = result
            return result

    def _find_runner_job_id(self, task_id: str) -> str | None:
        jobs_root = self.data_dir / "managed-comfy" / "jobs"
        for job_path in jobs_root.glob("*/job.json"):
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("tag") == task_id and job.get("id") == job_path.parent.name:
                return job["id"]
        return None

    @staticmethod
    def _lease_record_matches(job: dict[str, Any], current: dict[str, Any] | None) -> bool:
        return bool(current and current.get("owner_task") == job.get("task_id")
                    and current.get("owner_attempt") == job.get("owner_attempt")
                    and current.get("lease_id") == job.get("lease_id")
                    and current.get("generation_token") == job.get("generation_token")
                    and current.get("runtime_instance") == job.get("runtime_instance"))

    def reconcile(self, task_id: str, *, owner_task: str | None = None,
                  owner: str | None = None) -> dict[str, Any]:
        job = self._load(task_id)
        if owner is not None and job.get("owner") != owner:
            raise FileNotFoundError("managed Comfy task not found")
        if owner_task is not None and owner_task != task_id:
            raise PermissionError("task owner does not match")
        with self._lock:
            active = self._active.get(task_id)
        if not active:
            current = self.manager.current()
            if not self._lease_record_matches(job, current):
                raise PermissionError("task owner does not match active lease")
            stopped = self._owned_runtime_absent(job)
            if stopped is True:
                proof = {"runtime_status": "stopped", "evidence": {
                    "runtime_instance": job["runtime_instance"], "pid": job.get("runtime_pid"),
                    "create_time": job.get("runtime_created_at"),
                    "process_identity": "exact owner process absent or PID identity changed",
                    "fixed_port_health": False,
                }}
                self.manager.reconcile(job["lease_id"], job["generation_token"], proof=proof)
                job["status"] = "needs_reconcile"
                job["error"] = "runtime stop verified; adapter result still needs collection reconciliation"
                job["lease_release_proof"] = proof
                job["released_at"] = time.time()
                self._save(job)
                return job
            job["status"] = "needs_reconcile"
            job["error"] = "runtime owner is alive or its identity is indeterminate; lease retained"
            self._save(job)
            return job
        if not active.get("runner") or not job.get("runner_job_id"):
            job["status"] = "needs_reconcile"
            job["error"] = "adapter job identity is not persisted; lease retained"
            self._save(job)
            return job
        try:
            adapter_job = json.loads((self.data_dir / "managed-comfy" / "jobs"
                                      / job["runner_job_id"] / "job.json").read_text())
            if adapter_job.get("status") == "failed" and (adapter_job.get("failure") or {}).get("stage") == "download":
                adapter_job["status"] = "needs_reconcile"
                if adapter_job.get("attempts"):
                    adapter_job["attempts"][-1]["state"] = "needs_reconcile"
                _write_json(self.data_dir / "managed-comfy" / "jobs"
                            / job["runner_job_id"] / "job.json", adapter_job)
            result = active["runner"].reconcile(job["runner_job_id"])
        except Exception as exc:  # noqa: BLE001 -- 어떤 실패에도 lease를 조기 해제하지 않고 reconcile 상태로 남긴다
            job["status"] = "needs_reconcile"
            job["error"] = f"{type(exc).__name__}: {exc}"
            self._save(job)
            return job
        job["result"] = result
        job["status"] = result.get("status", "needs_reconcile")
        if job["status"] == "failed" and (result.get("failure") or {}).get("stage") == "download":
            job["status"] = "needs_reconcile"
            job["error"] = "download remains incomplete; lease retained for collection retry"
        self._attach_receipt(job, active)
        self._save(job)
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return self._stop_release(task_id, job, active)
        return job


_default: ComfyManagedController | None = None
_default_lock = threading.Lock()


def default_controller(data_dir: Path) -> ComfyManagedController:
    global _default
    with _default_lock:
        if _default is None:
            _default = ComfyManagedController(data_dir=data_dir)
        elif _default.data_dir != Path(data_dir):
            raise RuntimeError("managed Comfy controller data directory cannot change while the server is running")
        return _default


def _port_connection_refused(port: int) -> bool:
    """고정 Comfy 포트가 명시적으로 연결을 거부할 때만 True. 응답·타임아웃·기타 오류는 부재 증거가 아니다."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == errno.ECONNREFUSED


def force_release_evidence(data_dir: Path, record: dict[str, Any], *, port: int | None = None,
                           allow_unrecorded_pid: bool = False) -> dict[str, Any]:
    """운영자 force-release 전에 managed Comfy runtime 이 없음을 확인한다. 확인하지 못하면 ValueError(거부 사유).

    controller job 기록(task id·lease id·generation token·runtime instance 일치), 기록된 runtime pid+시작 시각이
    ABSENT, 고정 포트의 연결 거부를 모두 요구한다. pid 가 한 번도 기록되지 않았다면 운영자가 직접 확인했다는 명시
    플래그가 있을 때만 포트 거부만으로 진행한다.
    """
    port = COMFY_PORT if port is None else port
    task_id = str(record.get("owner_task"))
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("lease owner is not a managed Comfy task id")
    job_path = Path(data_dir) / "managed-comfy" / "tasks" / f"{task_id}.json"
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"controller job record is missing or unreadable ({type(exc).__name__}); "
                         "the Comfy runtime cannot be verified") from exc
    for key, expected in (("task_id", task_id), ("lease_id", record.get("lease_id")),
                          ("generation_token", record.get("generation_token")),
                          ("runtime_instance", record.get("runtime_instance"))):
        if not isinstance(job, dict) or job.get(key) != expected:
            raise ValueError(f"controller job record does not match the lease ({key})")
    pid, start = job.get("runtime_pid"), job.get("runtime_created_at")
    evidence: dict[str, Any] = {"consumer": "managed_comfy", "task_id": task_id, "runtime_pid": pid,
                                "runtime_created_at": start}
    if pid is None:
        if not allow_unrecorded_pid:
            raise ValueError("runtime pid was never recorded; verify no Comfy process exists and pass "
                             "--accept-unrecorded-comfy-runtime")
        evidence["runtime_process_state"] = "never_recorded_operator_accepted"
    else:
        observed = process_state(pid, start)
        if observed is not ProcState.ABSENT:
            raise ValueError(f"runtime pid {pid} is not confirmed absent (observed {observed.value})")
        evidence["runtime_process_state"] = observed.value
    if not _port_connection_refused(port):
        raise ValueError(f"fixed Comfy port {port} answers or its state is unknown")
    evidence.update(port=port, port_connection_refused=True)
    return evidence
