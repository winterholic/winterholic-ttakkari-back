"""Local-only, allowlist-based export of desk feedback for router evaluation.

The database reader is a separate CLI so the ordinary router never touches real
feedback. Raw messages must stay in-process and must never be printed/logged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "[비밀값 제거]"),
    (re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|secret|password)\s*[:=]\s*[^\s,;]+"), "[비밀값 제거]"),
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE), "[이메일 제거]"),
    (re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE), "[URL 제거]"),
    (re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{7,}\d)(?!\w)"), "[전화번호 제거]"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE), "[식별자 제거]"),
    (re.compile(r"(?<!\w)(?:\d{1,3}\.){3}\d{1,3}(?!\w)"), "[네트워크 주소 제거]"),
    (re.compile(r"(?<!\w)(?:~?/|[A-Za-z]:\\)[^\s\]\[(){}<>\"']+"), "[경로 제거]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9_]{20,})\b"), "[토큰 제거]"),
    (re.compile(r"```[\s\S]*?```"), "[코드 블록 생략]"),
)

_SENSITIVE_PATTERNS = tuple(pattern for pattern, _ in _REDACTIONS)


def redact_message(message: str) -> str:
    """Deterministically remove common direct identifiers and secret patterns."""
    value = message
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def scan_export_text(value: Any) -> list[str]:
    """Return pattern names still present; callers must fail closed on any hit."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return [f"sensitive_pattern_{index}" for index, pattern in enumerate(_SENSITIVE_PATTERNS) if pattern.search(text)]


def build_record(row: dict[str, Any], *, sample_id: str) -> dict[str, Any]:
    """Export only the allowlisted message and routing-label provenance."""
    decided, expected = row.get("decided"), row.get("expected")
    if decided not in {"answer", "delegate"} or expected not in {"answer", "delegate"}:
        raise ValueError("invalid_feedback_label")
    message = redact_message(str(row.get("message") or ""))
    if len(message) < 2:
        raise ValueError("empty_after_redaction")
    findings = scan_export_text(message)
    if findings:
        raise ValueError("redaction_scan_failed")
    corrected = expected != decided
    return {
        "sample_id": sample_id,
        "message": message,
        "label": expected,
        "label_status": "automatic_user_correction_unreviewed" if corrected else "user_confirmation_reference_only",
        "evidence": {"source": "desk_feedback", "decided": decided, "expected": expected},
    }


def write_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    """Write atomically with a restrictive local file mode after full-scan pass."""
    allowed = {"sample_id", "message", "label", "label_status", "evidence"}
    for record in records:
        if set(record) != allowed or scan_export_text(record):
            raise ValueError("export_validation_failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


async def export_local_feedback(destination: Path) -> dict[str, int]:
    """Read only desk_feedback columns using local socket defaults; never log row values."""
    import asyncpg

    conn = await asyncpg.connect("postgresql://localhost/ttakkari", command_timeout=15)
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(
                "SELECT message, decided, expected FROM desk_feedback ORDER BY updated_at, id"
            )
    finally:
        await conn.close()
    records = [build_record(dict(row), sample_id=f"real-{index:04d}") for index, row in enumerate(rows, 1)]
    write_jsonl(records, destination)
    return {
        "feedback_rows": len(rows),
        "corrections": sum(row["label_status"] == "automatic_user_correction_unreviewed" for row in records),
        "confirmations_reference_only": sum(row["label_status"] == "user_confirmation_reference_only" for row in records),
        "exported": len(records),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export locally redacted desk feedback; raw text is never printed.")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(export_local_feedback(args.destination))
    except Exception as exc:  # noqa: BLE001 -- suppress raw DB/message details in the local-only exporter.
        print(json.dumps({"status": "failed", "failure_type": type(exc).__name__}))
        raise SystemExit(2) from None
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
