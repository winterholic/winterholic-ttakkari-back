"""배포 job 로그 읽기. 백엔드가 자기 홈의 파일을 읽어 마스킹·요약해서 주므로 에이전트는 보호 영역을 건드리지 않는다."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from ttakkari.oplog.normalize import parse_traceback
from ttakkari.oplog.scrub import scrub

MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_LINE_CHARS = 300
SECTION_MAX_LINES = 40
FAILED_STATUSES = {"failed", "recovery_pending"}

_ERROR_LINE = re.compile(
    r"Traceback \(most recent call last\)|^E\s{2,}|^(?:FAILED|ERROR)\b|\b(?:error|fatal)\b\s*[:\[]|npm ERR!|"
    r"\b(?:Exception|ModuleNotFoundError|ImportError|PermissionError|FileNotFoundError|TimeoutError)\b|"
    r"exit(?:ed with)? (?:code|status)|command failed|No such file|Permission denied|timed out|timeout|"
    r"^=+ .*(?:FAILURES|ERRORS|failed|error).* =+$|launchd operation failed",
    re.IGNORECASE | re.MULTILINE,
)
_FAILED_TEST = re.compile(r"^FAILED\s+\S+")
_E_LINE = re.compile(r"^E\s{2,}\S")
_STAGE_FROM_ERROR = re.compile(r"release stage (?P<stage>[\w .\-]+?) (?:failed|could not start)")
_STAGE_MARKERS = [
    ("pytest", re.compile(r"test session starts|collected \d+ items?|^FAILED tests/|\d+ (?:passed|failed)\b.* in [\d.]+s", re.MULTILINE)),
    ("migrate", re.compile(r"Running upgrade|alembic|ttakkari\.cli migrate|MIGRATION", re.IGNORECASE)),
    ("npm ci", re.compile(r"npm ERR!|npm ci|added \d+ packages")),
    ("uv sync", re.compile(r"Resolved \d+ packages|uv sync|Failed to (?:build|download)|Installed \d+ packages")),
    ("launchd", re.compile(r"launchctl|launchd operation failed", re.IGNORECASE)),
]


def _store():
    from ttakkari import deployment

    return deployment.JobStore(deployment.state_dir())


def list_jobs() -> list[dict]:
    try:
        return _store().all()
    except (OSError, ValueError):
        return []


def resolve_job(ref: str | None) -> dict | None:
    """job id(전체·8자 이상 접두사)·SHA 접두사(7자 이상)·latest·latest_failed 를 job 하나로 푼다. 경로는 입력으로 받지 않는다."""
    jobs = list_jobs()
    if not jobs:
        return None
    ref = (ref or "latest").strip().lower()
    if ref == "latest":
        return jobs[-1]
    if ref == "latest_failed":
        failed = [j for j in jobs if j.get("status") in FAILED_STATUSES]
        return failed[-1] if failed else None
    if not re.fullmatch(r"[0-9a-f-]{7,40}", ref):
        return None
    hits = [j for j in jobs if j["id"].startswith(ref)] or [j for j in jobs if j.get("revision", "").startswith(ref)]
    return hits[-1] if hits else None


def log_path(job_id: str) -> Path:
    return _store().jobs / f"{uuid.UUID(job_id)}.log"


def read_log(job_id: str) -> tuple[list[str], dict[str, Any]]:
    path = log_path(job_id)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > MAX_LOG_BYTES:
                f.seek(size - MAX_LOG_BYTES)
            raw = f.read()
    except OSError:
        return [], {"exists": False, "bytes": 0, "lines": 0}
    lines = raw.decode("utf-8", "replace").splitlines()
    return lines, {"exists": True, "bytes": size, "lines": len(lines), "clipped_head": size > MAX_LOG_BYTES}


def error_sections(lines: list[str], *, max_sections: int = 4, context: int = 3) -> list[dict[str, Any]]:
    hits = [i for i, line in enumerate(lines) if _ERROR_LINE.search(line)]
    windows: list[list[int]] = []
    for i in hits:
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])
    if len(windows) > max_sections:
        windows = windows[:2] + windows[-(max_sections - 2):]
    out = []
    for lo, hi in windows:
        chunk = [f"{n + 1}| {lines[n][:MAX_LINE_CHARS]}" for n in range(lo, hi)]
        if len(chunk) > SECTION_MAX_LINES:
            omitted = len(chunk) - SECTION_MAX_LINES
            chunk = chunk[:25] + [f"… {omitted} lines omitted (use range mode) …"] + chunk[-14:]
        out.append({"from_line": lo + 1, "to_line": hi, "text": scrub("\n".join(chunk))})
    return out


def infer_stage(job: dict, lines: list[str]) -> str:
    m = _STAGE_FROM_ERROR.search(job.get("error") or "")
    if m:
        return m["stage"].strip()
    text = "\n".join(lines[-400:])
    for name, marker in _STAGE_MARKERS:
        if marker.search(text):
            return name
    return "unknown"


def signature(job: dict, lines: list[str]) -> str:
    """같은 원인의 배포 실패가 같은 이슈로 묶이도록 오류의 핵심 한 줄을 고른다."""
    for line in lines:
        if _FAILED_TEST.match(line):
            return line.strip()
    for line in lines:
        if _E_LINE.match(line):
            return line.strip()
    trace = parse_traceback("\n".join(lines))
    if trace:
        return f"{trace.exc_type}: {trace.exc_message}"
    for line in reversed(lines):
        if _ERROR_LINE.search(line):
            return line.strip()
    return (job.get("error") or "").strip().splitlines()[0] if job.get("error") else "(no error text)"


def headline(job: dict, lines: list[str]) -> tuple[str, str, str]:
    stage = infer_stage(job, lines)
    sig = scrub(signature(job, lines))
    return stage, sig, f"deploy {stage} failed: {sig[:160]}"


def summarize(job: dict, *, with_log: bool = True) -> dict[str, Any]:
    lines, meta = read_log(job["id"]) if with_log else ([], {})
    base: dict[str, Any] = {
        "job_id": job["id"], "revision": job.get("revision"), "status": job.get("status"),
        "strategy": job.get("strategy"), "created_at": job.get("created_at"), "updated_at": job.get("updated_at"),
        "error": scrub((job.get("error") or "")[:400]) or None, "log": meta,
    }
    if job.get("status") in FAILED_STATUSES and with_log:
        stage, sig, _ = headline(job, lines)
        base["stage"], base["cause"] = stage, sig[:200]
    return base


def view(job: dict, mode: str = "summary", *, lines: int = 60, offset: int | None = None) -> dict[str, Any]:
    all_lines, _ = read_log(job["id"])
    out = summarize(job)
    n = max(1, min(lines, 200))
    if mode == "summary":
        out["sections"] = error_sections(all_lines, max_sections=3)
        if not out["sections"] and all_lines:
            out["tail"] = scrub("\n".join(f"{i + 1}| {l[:MAX_LINE_CHARS]}" for i, l in enumerate(all_lines[-15:], len(all_lines) - min(15, len(all_lines)))))
    elif mode == "errors":
        out["sections"] = error_sections(all_lines, max_sections=6)
    elif mode == "tail":
        start = max(0, len(all_lines) - n)
        out["from_line"] = start + 1
        out["text"] = scrub("\n".join(f"{i + 1}| {l[:MAX_LINE_CHARS]}" for i, l in enumerate(all_lines[start:], start)))
    elif mode == "range":
        start = max(0, (offset or 1) - 1)
        chunk = all_lines[start:start + n]
        out["from_line"] = start + 1
        out["text"] = scrub("\n".join(f"{i + 1}| {l[:MAX_LINE_CHARS]}" for i, l in enumerate(chunk, start)))
        if start + n < len(all_lines):
            out["next_offset"] = start + n + 1
    else:
        raise ValueError("mode must be summary, errors, tail or range")
    return out
