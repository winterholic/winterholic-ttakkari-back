"""Bounded operator commands: no model invocation, no source modification."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path

from ttakkari import db


def read_bounded(path: Path, max_bytes: int) -> bytes:
    from ttakkari.broker.policy import is_sensitive

    if path.is_symlink() or is_sensitive(path):
        raise ValueError("disallowed_input")
    with path.open("rb") as f:
        before = os.fstat(f.fileno())
        if before.st_size > max_bytes:
            raise ValueError("input_too_large")
        raw = f.read(max_bytes + 1)
        after = os.fstat(f.fileno())
    if len(raw) > max_bytes or (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("input_changed_or_too_large")
    return raw


def _deadline(_signum, _frame):
    raise TimeoutError("knowledge_command_deadline")


def run(args) -> int:
    """CLI owns its timer. Partial/unknown results exit nonzero; reports never include source text."""
    from sqlalchemy.exc import SQLAlchemyError

    from ttakkari.knowledge import chatgpt, evaluation

    async def execute():
        db.init_engine()
        try:
            async with db.sessionmaker()() as session:
                if args.command == "knowledge-import-chatgpt":
                    path = Path(args.source).expanduser()
                    raw = read_bounded(path, chatgpt.MAX_BYTES)
                    result = await chatgpt.import_shard(
                        session,
                        raw,
                        source_key=f"chatgpt:{args.namespace}:{path.name}",
                        locator=str(path),
                        scope=args.scope,
                    )
                    await session.commit()
                    return {
                        "status": "partial"
                        if result["accounting"]["errors"]
                        else "succeeded",
                        **result,
                        "semantic_extraction": "not_run",
                        "coverage_scope": "specified_shard_only",
                    }
                if args.command == "knowledge-backfill":
                    return await backfill_registered(
                        session, limit=args.limit, offset=args.offset
                    )
                if args.command == "knowledge-eval":
                    data = json.loads(read_bounded(Path(args.gold), 4 * 1024 * 1024))
                    evaluation.validate_split(data["dev"], data["holdout"])
                    cases = data[args.split]
                    result = await evaluation.compare(
                        session, cases, scope=args.scope, k=args.k
                    )
                    return {
                        "status": "succeeded",
                        "split": args.split,
                        "label_authority": data.get("label_authority", "unverified"),
                        **result,
                    }
                raise ValueError("unknown_knowledge_command")
        finally:
            await db.dispose_engine()

    if args.timeout <= 0:
        raise ValueError("invalid_timeout")
    previous = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("process_timer_already_active")
    signal.signal(signal.SIGALRM, _deadline)
    try:
        signal.setitimer(signal.ITIMER_REAL, args.timeout)
        result = asyncio.run(execute())
    except TimeoutError:
        result = {
            "status": "unknown",
            "error": "knowledge_command_deadline",
            "counts": {},
        }
    except (ValueError, TypeError, OSError, KeyError, SQLAlchemyError):
        result = {
            "status": "failed",
            "error": "input_validation_or_access_failed",
            "counts": {},
        }
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    if args.output:
        with Path(args.output).expanduser().open("x") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("predictions", "metrics")},
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "succeeded" else 2


async def backfill_registered(session, *, limit: int = 100, offset: int = 0) -> dict:
    """Capture only registered document paths. No folder scan, deletion, inference or promotion."""
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("invalid_page")
    from sqlalchemy import select

    from ttakkari.context.indexer import MAX_FILE_BYTES
    from ttakkari.knowledge import store
    from ttakkari.models import CtxDocument, CtxSource

    rows = (
        await session.execute(
            select(CtxDocument, CtxSource)
            .join(CtxSource, CtxSource.id == CtxDocument.source_id)
            .where(CtxSource.enabled.is_(True))
            .order_by(CtxDocument.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    result = {
        "attempted": len(rows),
        "captured": 0,
        "errors": [],
        "next_offset": offset + len(rows),
        "status": "succeeded",
        "coverage_scope": "registered_documents_only",
        "semantic_extraction": "not_run",
    }
    # Snapshot scalar values: rollback expires ORM rows, including later rows in this page.
    inputs = [(d.id, d.path, s.scope, s.id, s.category) for d, s in rows]
    for doc_id, path, scope, source_id, category in inputs:
        try:
            raw = read_bounded(Path(path), MAX_FILE_BYTES)
            await store.import_text(
                session,
                source_key=f"indexed-document:{doc_id}",
                body=raw.decode("utf-8"),
                scope=scope,
                source_id=source_id,
                locator=path,
                origin="indexed_document",
                sensitivity="sensitive" if category == "health" else "normal",
            )
            await session.commit()
            result["captured"] += 1
        except (OSError, ValueError):
            await session.rollback()
            result["errors"].append(
                {"document_id": str(doc_id), "code": "source_unavailable_or_invalid"}
            )
    if result["errors"]:
        result["status"] = "partial"
    return result
