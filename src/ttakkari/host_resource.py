"""Exclusive host-resource lease shared by managed model and media runtimes.

The lock path intentionally reuses ``data_dir/host-gpu/lock``: legacy llama
supervisors already hold this flock for their full lifetime. A consumer that
does not adopt this manager therefore still blocks new managed leases.
"""
from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

from ttakkari.host_gpu_guard import proc_start


class LeaseError(RuntimeError):
    pass


class LeaseBusy(LeaseError):
    pass


class LeaseOwnershipError(LeaseError):
    pass


def _valid_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{name} must be a non-empty string of at most 200 characters")
    return value


def _read(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise LeaseBusy(f"resource lease state is unreadable; refusing acquisition: {exc}") from exc
    if not isinstance(payload, dict):
        raise LeaseBusy("resource lease state is invalid; refusing acquisition")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".lease-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


class ResourceLeaseHandle:
    """A held cross-process lease. Closing the process leaves a stale record."""

    def __init__(self, manager: ResourceLeaseManager, fd, record: dict[str, Any]) -> None:
        self.manager = manager
        self._lock_fd = fd
        self._record = record

    @property
    def lease_id(self) -> str:
        return self._record["lease_id"]

    @property
    def generation_token(self) -> str:
        return self._record["generation_token"]

    @property
    def owner_task(self) -> str:
        return self._record["owner_task"]

    @property
    def owner_attempt(self) -> str:
        return self._record["owner_attempt"]

    @property
    def runtime_instance(self) -> str:
        return self._record["runtime_instance"]

    @property
    def resource_family(self) -> str:
        return self._record["resource_family"]

    def heartbeat(self, *, ttl_seconds: float = 30, now: float | None = None) -> dict[str, Any]:
        self._record = self.manager._heartbeat(self._record, ttl_seconds=ttl_seconds, now=now)
        return dict(self._record)

    def mark_running(self, *, now: float | None = None) -> dict[str, Any]:
        self._record = self.manager._mark_running(self._record, now=now)
        return dict(self._record)

    def request_cancel(self, *, now: float | None = None) -> dict[str, Any]:
        self._record = self.manager._request_cancel(self._record, now=now)
        return dict(self._record)

    def release(self, *, proof: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        released = self.manager._release(self._record, proof=proof, now=now)
        self._record = released
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        self._lock_fd.close()
        return dict(released)


class ResourceLeaseManager:
    """Single-host exclusive lease with owner-bound heartbeat and release."""

    LIVE_PHASES: ClassVar[set[str]] = {"starting", "running", "cancel_requested"}

    def __init__(self, *, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.lock_path = self.data_dir / "host-gpu" / "lock"
        self.record_path = self.data_dir / "host-resource" / "lease.json"

    def current(self) -> dict[str, Any] | None:
        record = _read(self.record_path)
        if record and record.get("phase") in self.LIVE_PHASES:
            return record
        return None

    def recorded_phase(self, lease_id: str, generation_token: str) -> str | None:
        """기록된 lease 가 이 id·세대와 일치할 때만 그 phase 를 돌려준다(이미 released 인지 구분하는 용도)."""
        record = _read(self.record_path)
        if record and record.get("lease_id") == lease_id and record.get("generation_token") == generation_token:
            return record.get("phase")
        return None

    def is_locked(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return True
                raise
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False

    def acquire(
        self,
        *,
        owner_task: str,
        owner_attempt: str,
        runtime_instance: str,
        resource_family: str,
        claim: dict[str, Any],
        ttl_seconds: float = 30,
        now: float | None = None,
    ) -> ResourceLeaseHandle:
        owner_task = _valid_text(owner_task, "owner_task")
        owner_attempt = _valid_text(owner_attempt, "owner_attempt")
        runtime_instance = _valid_text(runtime_instance, "runtime_instance")
        resource_family = _valid_text(resource_family, "resource_family")
        _validate_ttl(ttl_seconds)
        try:
            claim = json.loads(json.dumps(claim, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("claim must contain finite JSON values") from exc
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = self.lock_path.open("a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fd.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LeaseBusy("host GPU resource is owned by another runtime") from exc
            raise

        current = _read(self.record_path)
        if current and current.get("phase") in self.LIVE_PHASES:
            fd.close()
            if float(current.get("expires_at", 0)) <= (time.time() if now is None else now):
                raise LeaseBusy("host resource lease expired; runtime must be reconciled before reuse")
            raise LeaseBusy("host GPU resource has an active lease")

        # Before this manager is deployed, host_gpu wrote only state.json and
        # held the same flock. A live legacy record without a lease token is
        # ambiguous after a restart, so it must never be treated as free.
        legacy = _read(self.data_dir / "host-gpu" / "state.json")
        if legacy and legacy.get("phase") in {"starting", "ready"}:
            fd.close()
            raise LeaseBusy("legacy host_gpu runtime needs owner reconciliation")

        started = time.time() if now is None else float(now)
        record = {
            "lease_id": uuid.uuid4().hex,
            "owner_task": owner_task,
            "owner_attempt": owner_attempt,
            "runtime_instance": runtime_instance,
            "generation_token": uuid.uuid4().hex,
            "resource_family": resource_family,
            "claim": claim,
            "phase": "starting",
            "heartbeat_at": started,
            "expires_at": started + float(ttl_seconds),
            "created_at": started,
            # 소유 프로세스 신원: 소유자가 죽어 state 도 없을 때 회수 가능 여부(ABSENT 확인)를 판단하는 유일한 근거다.
            "owner_pid": os.getpid(),
            "owner_start": proc_start(os.getpid()),
            "cancel_requested_at": None,
            "release_proof": None,
        }
        try:
            _write(self.record_path, record)
        except BaseException:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
            raise
        return ResourceLeaseHandle(self, fd, record)

    def heartbeat(self, lease_id: str, generation_token: str, *, ttl_seconds: float = 30,
                  now: float | None = None) -> dict[str, Any]:
        record = self._owned_record(lease_id, generation_token)
        return self._heartbeat(record, ttl_seconds=ttl_seconds, now=now)

    def request_cancel(self, lease_id: str, generation_token: str, *, now: float | None = None) -> dict[str, Any]:
        record = self._owned_record(lease_id, generation_token)
        return self._request_cancel(record, now=now)

    def release(self, lease_id: str, generation_token: str, *, proof: dict[str, Any],
                now: float | None = None) -> dict[str, Any]:
        record = self._owned_record(lease_id, generation_token)
        return self._release(record, proof=proof, now=now)

    def reconcile(self, lease_id: str, generation_token: str, *, proof: dict[str, Any],
                  now: float | None = None) -> dict[str, Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise LeaseBusy("runtime still holds the shared host resource lock") from exc
                raise
            record = self._owned_record(lease_id, generation_token)
            _validate_release_proof(proof, expected_runtime_instance=record["runtime_instance"])
            stamp = time.time() if now is None else float(now)
            released = {**record, "phase": "released", "released_at": stamp, "release_proof": proof}
            _write(self.record_path, released)
            return released

    def _owned_record(self, lease_id: str, generation_token: str) -> dict[str, Any]:
        record = _read(self.record_path)
        if (not record or record.get("phase") not in self.LIVE_PHASES
                or record.get("lease_id") != lease_id or record.get("generation_token") != generation_token):
            raise LeaseOwnershipError("lease owner or generation token does not match the active lease")
        return record

    def _heartbeat(self, record: dict[str, Any], *, ttl_seconds: float, now: float | None) -> dict[str, Any]:
        _validate_ttl(ttl_seconds)
        current = self._owned_record(record["lease_id"], record["generation_token"])
        stamp = time.time() if now is None else float(now)
        updated = {**current, "heartbeat_at": stamp, "expires_at": stamp + float(ttl_seconds)}
        _write(self.record_path, updated)
        return updated

    def _mark_running(self, record: dict[str, Any], *, now: float | None) -> dict[str, Any]:
        current = self._owned_record(record["lease_id"], record["generation_token"])
        if current["phase"] != "starting":
            raise LeaseError(f"cannot mark runtime running from phase {current['phase']}")
        stamp = time.time() if now is None else float(now)
        updated = {**current, "phase": "running", "heartbeat_at": stamp}
        _write(self.record_path, updated)
        return updated

    def _request_cancel(self, record: dict[str, Any], *, now: float | None) -> dict[str, Any]:
        current = self._owned_record(record["lease_id"], record["generation_token"])
        stamp = time.time() if now is None else float(now)
        updated = {**current, "phase": "cancel_requested", "cancel_requested_at": stamp,
                   "heartbeat_at": stamp}
        _write(self.record_path, updated)
        return updated

    def _release(self, record: dict[str, Any], *, proof: dict[str, Any], now: float | None) -> dict[str, Any]:
        current = self._owned_record(record["lease_id"], record["generation_token"])
        _validate_release_proof(proof, expected_runtime_instance=current["runtime_instance"])
        stamp = time.time() if now is None else float(now)
        released = {**current, "phase": "released", "released_at": stamp, "release_proof": proof}
        _write(self.record_path, released)
        return released


def _validate_ttl(ttl_seconds: float) -> None:
    if isinstance(ttl_seconds, bool) or not math.isfinite(float(ttl_seconds)) or not 1 <= float(ttl_seconds) <= 3600:
        raise ValueError("ttl_seconds must be between 1 and 3600")


def _validate_release_proof(proof: dict[str, Any], *, expected_runtime_instance: str) -> None:
    if not isinstance(proof, dict) or proof.get("runtime_status") != "stopped":
        raise ValueError("release requires observed runtime_status='stopped'")
    evidence = proof.get("evidence")
    if not isinstance(evidence, dict) or not evidence or set(evidence) <= {"runtime_instance"}:
        raise ValueError("release requires non-empty stop evidence")
    if evidence.get("runtime_instance") != expected_runtime_instance:
        raise ValueError("release evidence must identify the leased runtime instance")

