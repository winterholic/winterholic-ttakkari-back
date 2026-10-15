"""Opt-in, in-memory observer plumbing for same-input Qwen shadow preparation.

The module deliberately has no model client, worker, database, filesystem, or
network side effect. A staging caller may enqueue a redacted immutable snapshot;
this preparation stage does not start a consumer.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ttakkari.experiments.qwen35_feedback_export import redact_message, scan_export_text
from ttakkari.experiments.qwen35_shadow import CandidateSnapshot

MAX_TEXT_CHARS = 6000
QUEUE_CAPACITY = 4
QUEUE_TTL_SECONDS = 60.0
MAX_SAMPLE_RATE = 0.05
LABEL_STATUS = "policyverified/manualpending"
RETENTION_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class ShadowObservation:
    """Only the explicit, redacted allowlist needed for a later paired replay."""

    request_text: str
    context_text: str
    policy_version: str
    scope: str
    candidates: CandidateSnapshot
    redaction_counts: tuple[str, ...]
    label_status: str = LABEL_STATUS
    regression_candidate: bool = False


@dataclass(frozen=True)
class _QueuedObservation:
    observation: ShadowObservation
    enqueued_at: float


class ShadowObservationQueue:
    """Small process-local queue. No persistence or implicit consumer is started."""

    def __init__(self, maxsize: int = QUEUE_CAPACITY, ttl_seconds: float = QUEUE_TTL_SECONDS) -> None:
        if maxsize < 1 or ttl_seconds <= 0:
            raise ValueError("invalid_queue_bounds")
        self._items: deque[_QueuedObservation] = deque()
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self.dropped: Counter[str] = Counter()

    def _expire(self) -> None:
        now = time.monotonic()
        while self._items and now - self._items[0].enqueued_at > self.ttl_seconds:
            self._items.popleft()
            self.dropped["expired"] += 1

    @property
    def qsize(self) -> int:
        self._expire()
        return len(self._items)

    def submit(self, observation: ShadowObservation) -> bool:
        self._expire()
        if len(self._items) >= self.maxsize:
            self.dropped["queue_full"] += 1
            return False
        self._items.append(_QueuedObservation(observation, time.monotonic()))
        return True

    def get_nowait(self) -> ShadowObservation:
        self._expire()
        if not self._items:
            raise IndexError("shadow_observation_queue_empty")
        return self._items.popleft().observation

    def close(self) -> int:
        """Drop pending observations on shutdown; never block waiting for a consumer."""
        count = len(self._items)
        self._items.clear()
        self.dropped["shutdown_drop"] += count
        return count


_queue: ShadowObservationQueue | None = None
_drops: Counter[str] = Counter()


def _get_queue() -> ShadowObservationQueue:
    global _queue
    if _queue is None:
        _queue = ShadowObservationQueue()
    return _queue


def drop_stats() -> dict[str, int]:
    """Return counters only; never include sample text or identifiers."""
    result = dict(_drops)
    if _queue is not None:
        for reason, count in _queue.dropped.items():
            result[reason] = result.get(reason, 0) + count
    return result


def record_drop(reason: str) -> None:
    """Count a failure class only; callers must not pass user content here."""
    safe_reason = reason if reason in {
        "observer_error", "redaction_or_size_rejected", "unknown_privacy_scope",
        "invalid_policy_version", "sample_rate_out_of_bounds",
    } else "observer_error"
    if _queue is None:
        _drops[safe_reason] += 1
    else:
        _queue.dropped[safe_reason] += 1


def _storage_record(observation: ShadowObservation) -> dict[str, object]:
    if observation.scope not in {"personal", "work"}:
        raise ValueError("invalid_storage_scope")
    textual_fields = {
        "request_text": observation.request_text,
        "context_text": observation.context_text,
        "policy_version": observation.policy_version,
    }
    if any(scan_export_text(value) for value in textual_fields.values()):
        raise ValueError("sensitive content rejected")
    return {
        **textual_fields,
        "scope": observation.scope,
        "policy_candidate_snapshot": {
            "lead_ids": list(observation.candidates.lead_ids),
            "model_ids": list(observation.candidates.model_ids),
            "tool_ids": list(observation.candidates.tool_ids),
            "permission_ids": list(observation.candidates.permission_ids),
        },
        "redaction_counts": list(observation.redaction_counts),
        "label_status": observation.label_status,
        "regression_candidate": observation.regression_candidate,
    }


def prune_expired_observations(scope_dir: Path, *, now: float | None = None) -> int:
    """Remove only expired observer-owned sample files from one scope directory."""
    if not scope_dir.exists() or scope_dir.is_symlink():
        return 0
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    removed = 0
    for path in scope_dir.glob("observation-*.json"):
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def persist_observation(observation: ShadowObservation, root: Path) -> Path:
    """Explicit future sink: scope-separated 0700 dirs and atomic 0600 JSON files.

    No application startup or route code calls this function in this preparation
    stage. The caller must explicitly select a local destination after approval.
    """
    if root.is_symlink():
        raise ValueError("storage_root_symlink_rejected")
    record = _storage_record(observation)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    scope_dir = root / observation.scope
    if scope_dir.is_symlink():
        raise ValueError("storage_scope_symlink_rejected")
    scope_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(scope_dir, 0o700)
    prune_expired_observations(scope_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = scope_dir / f"observation-{stamp}-{uuid4().hex}.json"
    fd, temp_name = tempfile.mkstemp(prefix=".observer-", suffix=".tmp", dir=scope_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination)
        os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return destination


def _sampled(sample_key: str, sample_rate: float) -> bool:
    if not 0 <= sample_rate <= MAX_SAMPLE_RATE:
        raise ValueError("sample_rate_out_of_bounds")
    if sample_rate == 0:
        return False
    bucket = int.from_bytes(hashlib.sha256(sample_key.encode("utf-8")).digest()[:8], "big") / 2**64
    return bucket < sample_rate


def _redact(value: str) -> tuple[str, tuple[str, ...]]:
    redacted = redact_message(value)
    if len(redacted) > MAX_TEXT_CHARS or scan_export_text(redacted):
        raise ValueError("redaction_or_size_rejected")
    # Counts expose which classes changed, not the original values.
    counts: list[str] = []
    for category, marker in (
        ("secret", "[비밀값 제거]"),
        ("email", "[이메일 제거]"),
        ("url", "[URL 제거]"),
        ("phone", "[전화번호 제거]"),
        ("identifier", "[식별자 제거]"),
        ("network_address", "[네트워크 주소 제거]"),
        ("path", "[경로 제거]"),
        ("token", "[토큰 제거]"),
        ("code", "[코드 블록 생략]"),
    ):
        count = redacted.count(marker)
        if count:
            counts.append(f"{category}:{count}")
    return redacted, tuple(counts)


def observe_if_enabled(
    *,
    enabled: bool,
    environment: str,
    request_text: str,
    context_text: str,
    policy_version: str,
    scope: str,
    candidates: CandidateSnapshot,
    sample_key: str,
    queue: ShadowObservationQueue | None = None,
    sample_rate: float = 0.0,
) -> bool:
    """Enqueue a redacted immutable copy, failing closed and never raising."""
    if not enabled or environment.strip().casefold() in {"prod", "production"}:
        return False
    try:
        if scope not in {"personal", "work"}:
            raise ValueError("unknown_privacy_scope")
        if not policy_version.strip() or len(policy_version) > 160:
            raise ValueError("invalid_policy_version")
        if not _sampled(sample_key, sample_rate):
            return False
        request, request_changes = _redact(request_text)
        context, context_changes = _redact(context_text)
        # Reconstructing the snapshot copies every candidate tuple at this boundary.
        fixed_candidates = CandidateSnapshot(
            tuple(candidates.lead_ids), tuple(candidates.model_ids),
            tuple(candidates.tool_ids), tuple(candidates.permission_ids),
        )
        observation = ShadowObservation(
            request_text=request,
            context_text=context,
            policy_version=policy_version.strip(),
            scope=scope,
            candidates=fixed_candidates,
            redaction_counts=tuple(
                [f"request:{value}" for value in request_changes]
                + [f"context:{value}" for value in context_changes]
            ),
        )
        return (queue if queue is not None else _get_queue()).submit(observation)
    except Exception as exc:  # noqa: BLE001 -- shadow observation cannot affect the caller.
        reason = str(exc) if str(exc) in {
            "unknown_privacy_scope", "invalid_policy_version", "sample_rate_out_of_bounds",
            "redaction_or_size_rejected", "invalid_queue_bounds",
        } else "observer_error"
        target = queue if queue is not None else _queue
        if target is None:
            _drops[reason] += 1
        else:
            target.dropped[reason] += 1
        return False
