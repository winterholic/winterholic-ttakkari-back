"""Exact text adapter: every node/part survives accounting, including inactive branches.

Revision SHA is the original shard's bytes, not a normalized or selected document.
Nontext payloads are explicitly unsupported; original files are never modified.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select, update

from ttakkari.context.inventory import (
    _active_nodes,
    _finite_float,
    _invalid_constant,
    _pointer,
    _unique_object,
)
from ttakkari.knowledge import store
from ttakkari.knowledge_models import KnowledgeRevision as R
from ttakkari.knowledge_models import KnowledgeUnit as U

PARSER = "chatgpt-exact-parts-v1"
MAX_BYTES = 64 * 1024 * 1024


def parse_shard(raw: bytes, locator: str) -> tuple[list[dict], dict]:
    if len(raw) > MAX_BYTES:
        raise ValueError("shard_too_large")
    data = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )
    if not isinstance(data, list):
        raise TypeError("expected_conversation_array")
    units, errors, seen = [], [], set()
    counts = Counter(conversations=len(data), nodes=0, messages=0)
    for ci, conversation in enumerate(data):
        pointer = f"/{ci}"
        if not isinstance(conversation, dict) or not isinstance(
            conversation.get("mapping"), dict
        ):
            raise TypeError(
                "invalid_conversation"
            )  # No success report for discarded records.
        cid = conversation.get("id") or conversation.get("conversation_id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError("missing_or_duplicate_conversation_identity")
        seen.add(cid)
        mapping = conversation["mapping"]
        active, branch_error = _active_nodes(conversation, mapping)
        if branch_error:
            errors.append({"pointer": pointer, "code": branch_error})
        for nid, node in mapping.items():
            counts["nodes"] += 1
            np = pointer + "/mapping/" + _pointer(nid)
            message = node.get("message") if isinstance(node, dict) else None
            role, event_time = "unknown", None
            parts, outcome, reason = [], "invalid", "invalid_node"
            if isinstance(node, dict) and "message" in node:
                if message is None:
                    outcome, reason = "no_message", "structural_node"
                elif isinstance(message, dict):
                    counts["messages"] += 1
                    author = message.get("author")
                    role = (
                        author.get("role", "unknown")
                        if isinstance(author, dict)
                        else "unknown"
                    )
                    if role not in ("user", "assistant", "system", "tool", "developer"):
                        role = "unknown"
                    timestamp = message.get("create_time")
                    if (
                        isinstance(timestamp, (float, int))
                        and not isinstance(timestamp, bool)
                        and math.isfinite(timestamp)
                    ):
                        try:
                            event_time = datetime.fromtimestamp(timestamp, UTC)
                        except (OverflowError, OSError, ValueError):
                            errors.append({"pointer": np, "code": "invalid_event_time"})
                    content = message.get("content")
                    parts = content.get("parts") if isinstance(content, dict) else None
                    if not isinstance(parts, list):
                        parts, outcome, reason = (
                            [],
                            "unsupported_content",
                            "content_adapter_required",
                        )
                    elif not parts:
                        outcome, reason = "no_message", "empty_parts"
            entries = [
                (np + f"/message/content/parts/{pi}", part)
                for pi, part in enumerate(parts)
            ]
            if not entries:
                entries = [
                    (
                        np + "/message"
                        if isinstance(node, dict) and "message" in node
                        else np,
                        None,
                    )
                ]
            metadata = message.get("metadata") if isinstance(message, dict) else None
            attachments = (
                metadata.get("attachments") if isinstance(metadata, dict) else None
            )
            if isinstance(attachments, list):
                entries += [
                    (np + f"/message/metadata/attachments/{ai}", attachment)
                    for ai, attachment in enumerate(attachments)
                ]
            for part_pointer, part in entries:
                text_part = isinstance(part, str) and "/content/parts/" in part_pointer
                body = part if text_part else ""
                if "\0" in body:
                    raise ValueError("unsupported_nul_in_source")
                is_payload = (
                    "/content/parts/" in part_pointer
                    or "/metadata/attachments/" in part_pointer
                )
                state = (
                    "pending_extraction"
                    if body
                    else "no_message"
                    if text_part
                    else "unsupported_content"
                    if is_payload
                    else outcome
                )
                why = (
                    None
                    if body
                    else "empty_text"
                    if text_part
                    else "nontext_part"
                    if is_payload
                    else reason
                )
                identity = part_pointer[len(np) :]
                for offset in range(0, max(len(body), 1), 1600):
                    chunk = body[offset : offset + 1600]
                    external = store.digest(
                        json.dumps([cid, nid, identity, offset], ensure_ascii=False)
                    )
                    units.append(
                        {
                            "external_key": external,
                            "locator": f"{locator}#{part_pointer}",
                            "body": chunk,
                            "sha256": store.digest(chunk),
                            "source_offset": len(units),
                            "speaker": role,
                            "active_branch": nid in active
                            if active is not None
                            else None,
                            "event_time": event_time,
                            "outcome": state,
                            "reason": why,
                            "provenance": {
                                "conversation_id": cid,
                                "node_id": nid,
                                "parent_node_id": node.get("parent")
                                if isinstance(node, dict)
                                else None,
                                "message_id": message.get("id")
                                if isinstance(message, dict)
                                else None,
                                "json_pointer": part_pointer,
                                "char_offset": offset,
                            },
                        }
                    )
    counts["units"] = len(units)
    return units, {
        **dict(counts),
        "outcomes": dict(Counter(u["outcome"] for u in units)),
        "errors": errors,
    }


async def import_shard(
    db, raw: bytes, *, source_key: str, locator: str, scope: str = "personal"
) -> dict:
    units, summary = parse_shard(raw, locator)
    sha = hashlib.sha256(raw).hexdigest()
    await store._source_lock(db, source_key)
    previous = await db.scalar(select(R).where(R.source_key == source_key).limit(1))
    if previous and (previous.scope != scope or previous.origin != "chatgpt_export"):
        raise store.KnowledgeError("source_identity_conflict", 409)
    existing = await db.scalar(
        select(R).where(
            R.source_key == source_key, R.sha256 == sha, R.parser_version == PARSER
        )
    )
    if existing:
        return {
            "revision": store.revision_out(existing),
            "replayed": True,
            "accounting": summary,
        }
    await db.execute(
        update(R)
        .where(R.source_key == source_key, R.active.is_(True))
        .values(active=False)
    )
    revision = R(
        source_key=source_key,
        sha256=sha,
        parser_version=PARSER,
        scope=scope,
        sensitivity="sensitive",
        locator=locator,
        origin="chatgpt_export",
    )
    db.add(revision)
    await db.flush()
    for unit in units:
        db.add(
            U(
                revision_id=revision.id,
                stable_key=store.digest(source_key + "\0" + unit["external_key"]),
                **unit,
            )
        )
    await db.flush()
    return {
        "revision": store.revision_out(revision),
        "replayed": False,
        "accounting": summary,
    }
