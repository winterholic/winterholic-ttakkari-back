"""Local artifact integrity and review API skeleton; no authentication claim.

Human attestations must arrive through a future authenticated service. This
module deliberately refuses to create human_verified labels in an unattended run.
"""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .integrity import digest


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".e0-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def cache_key(identity, input_bytes):
    required = {
        "corpus",
        "gold",
        "policy",
        "build",
        "model",
        "analyzer",
        "config",
        "seed",
        "code",
        "role",
        "window",
        "quantization",
    }
    if not required <= identity.keys() or any(
        identity[k] in (None, "") for k in required
    ):
        raise ValueError("incomplete_cache_identity")
    return digest(
        canonical(
            {
                "schema": "kb-e0-cache-v1",
                "identity": identity,
                "input_sha256": digest(input_bytes),
                "input_bytes": len(input_bytes),
            }
        )
    )


def verify_replay(record, expected_key, payload):
    if record["key"] != expected_key:
        raise ValueError("cache_key_mismatch")
    if digest(canonical(record["payload"])) != record["payload_sha256"]:
        raise ValueError("cache_corrupt")
    if canonical(record["payload"]) != canonical(payload):
        raise ValueError("replay_mismatch")
    return {"status": "exact_match", "payload_sha256": record["payload_sha256"]}


def open_holdout(path, registration, *, actor, role, at):
    if role != "independent_reviewer" or not actor or actor == "developer":
        raise ValueError("independent_reviewer_required")
    # Actor/role are untrusted caller metadata, never an authenticated principal.
    # No authenticated reviewer flow exists in E0: refuse before mkdir/open/hash
    # or any mutation. In particular, a denial cannot consume the future slot.
    raise ValueError(
        "reviewer_authentication_required: holdout remains unopened; authenticated review flow unavailable"
    )


def _review_update(path, event):
    path = Path(path)
    state = (
        json.loads(path.read_bytes())
        if path.exists()
        else {"cursor": None, "labels": {}, "history": []}
    )
    if event is None:
        return state
    if event["actor_kind"] == "human":
        raise ValueError(
            "human_attestation_required: authenticated human route not implemented"
        )
    if event["actor_kind"] not in {"agent", "llm"} or event["decision"] not in {
        "approve",
        "correct",
        "discard",
    }:
        raise ValueError("invalid_review_event")
    old = state["labels"].get(event["id"], {})
    if event["label_version"] != old.get("label_version", 0):
        raise ValueError("label_version_conflict")
    label = {
        **event,
        "label_version": event["label_version"] + 1,
        # Caller-supplied actor text; no server principal backs it in E0.
        "actor_authentication": "unauthenticated_claim",
        "verification": "agent_verified"
        if event["actor_kind"] == "agent"
        else "llm_judged",
    }
    state["labels"][event["id"]] = label
    state["cursor"] = event["id"]
    state["history"].append(label)
    atomic_json(path, state)
    return state


@contextmanager
def artifact_lock(path, *, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ValueError("artifact_writer_active") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def review_update(path, event):
    with artifact_lock(str(path) + ".lock"):
        return _review_update(path, event)
