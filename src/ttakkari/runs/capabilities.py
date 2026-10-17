"""엔진(실행 경로)별 실행 환경 능력의 저장·노출·프롬프트 주입(ADR 0037).

값은 추측이 아니라 runs/env_probe.py 의 실측 결과만 받는다. 측정 시각과 출처를 같이 저장하고, 측정하지 않은 항목은 unknown 이다.
엔진 환경은 그 엔진의 작업 세션이 프로브를 직접 돌려 MCP 도구로 보고하고(session_probe), 호스트 기준선은 백엔드가
자식 프로세스로 돌린다(backend_probe). 저장소는 세대(프로세스)와 무관한 파일이라 어느 세대가 받아도 같은 값을 본다.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ttakkari.config import HOME, get_settings
from ttakkari.runs import env_probe

KST = ZoneInfo("Asia/Seoul")
ENGINES = ("claude", "codex", "antigravity", "muse", "qwen-local", "host")
STATUSES = ("available", "blocked", "unknown")
ITEMS = env_probe.ITEMS
STALE_SECONDS = 24 * 3600
HOST_REFRESH_SECONDS = 3600
MAX_DETAIL = 200
MAX_PATHS = 12
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ReportError(ValueError):
    pass


def store_path() -> Path:
    return get_settings().data_dir / "execution-capabilities.json"


def probe_script() -> Path:
    """세션이 읽을 수 있는 프로브 경로. 릴리스 사본은 보호 영역일 수 있어 소스 저장소를 우선한다."""
    source = HOME / "development/projects/winterholic-ttakkari/back/src/ttakkari/runs/env_probe.py"
    return source if source.is_file() else Path(env_probe.__file__).resolve()


def _clean(text: Any) -> str:
    return _CONTROL.sub(" ", str(text))[:MAX_DETAIL]


def _read() -> dict[str, Any]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def _locked():
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write(data: dict[str, Any]) -> None:
    path = store_path()
    fd, tmp = tempfile.mkstemp(prefix=".caps-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def validate_report(payload: Any) -> dict[str, Any]:
    """프로브 JSON 을 정해진 모양으로만 받는다. 모르는 항목·상태는 거부하고 문자열은 잘라 낸다."""
    if not isinstance(payload, dict):
        raise ReportError("report must be the JSON object printed by env_probe.py")
    if payload.get("probe_version") != env_probe.PROBE_VERSION:
        raise ReportError(f"probe_version must be {env_probe.PROBE_VERSION}")
    raw_items = payload.get("items")
    if not isinstance(raw_items, dict) or not raw_items:
        raise ReportError("report.items is required")
    items: dict[str, Any] = {}
    for name, value in raw_items.items():
        if name not in ITEMS:
            raise ReportError(f"unknown probe item: {name}")
        if not isinstance(value, dict) or value.get("status") not in STATUSES:
            raise ReportError(f"{name}.status must be one of {', '.join(STATUSES)}")
        item: dict[str, Any] = {"status": value["status"], "detail": _clean(value.get("detail", ""))}
        paths = value.get("paths")
        if name == "writable_paths" and isinstance(paths, dict):
            item["paths"] = {_clean(k): bool(v) for k, v in list(paths.items())[:MAX_PATHS]}
        items[name] = item
    measured = payload.get("measured_epoch")
    if not isinstance(measured, (int, float)) or isinstance(measured, bool):
        raise ReportError("report.measured_epoch is required")
    now = time.time()
    # 미래 시각이나 하루보다 오래된 측정은 새 보고로 받지 않는다(재전송·시계 오류 방지).
    if measured > now + 300 or now - measured > STALE_SECONDS:
        raise ReportError("measured_epoch must be within the last 24 hours")
    return {"measured_epoch": float(measured), "items": items, "platform": _clean(payload.get("platform", ""))}


def record(engine: str, payload: Any, *, source: str, session_id: str | None = None,
           run_id: str | None = None) -> dict[str, Any]:
    if engine not in ENGINES:
        raise ReportError(f"unknown engine: {engine}")
    report = validate_report(payload)
    report.update(source=source, reported_epoch=time.time(), session_id=session_id, run_id=run_id)
    with _locked():
        data = _read()
        previous = data.get(engine) or {}
        # 늦게 도착한 옛 측정이 최신 값을 덮지 않게 한다. 한 번에 일부 항목만 온 경우는 항목 단위로 합친다.
        if previous.get("measured_epoch", 0) > report["measured_epoch"]:
            return view_engine(engine)
        merged = {**(previous.get("items") or {}), **report["items"]}
        data[engine] = {**report, "items": merged}
        _write(data)
    return view_engine(engine)


def _iso(epoch: float | None) -> str | None:
    return None if not epoch else datetime.fromtimestamp(epoch, KST).isoformat(timespec="seconds")


def view_engine(engine: str, *, now: float | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    entry = (data if data is not None else _read()).get(engine) or {}
    stored = entry.get("items") or {}
    items = {name: stored.get(name) or {"status": "unknown", "detail": "not measured"} for name in ITEMS}
    measured = entry.get("measured_epoch")
    age = None if measured is None else max(0, round(now - measured))
    blocked = [name for name, item in items.items() if item["status"] == "blocked"]
    return {
        "engine": engine,
        "measured_at": _iso(measured), "age_seconds": age,
        "stale": age is None or age > STALE_SECONDS,
        "source": entry.get("source"),
        "items": items,
        "known_limitations": [f"{name}: {items[name]['detail']}" for name in blocked],
        "scope_note": "Measured only for this execution environment; a blocked item does not mean the host cannot do it.",
    }


def compact(engine: str, now: float | None = None) -> dict[str, Any]:
    """배정 영수증용 요약: 측정 시각과 알려진 제한만. 전체 표는 snapshot 이다."""
    view = view_engine(engine, now=now)
    return {"engine": engine, "measured_at": view["measured_at"], "stale": view["stale"],
            "known_limitations": view["known_limitations"], "scope_note": view["scope_note"]}


def snapshot(now: float | None = None) -> dict[str, Any]:
    data = _read()
    return {engine: view_engine(engine, now=now, data=data) for engine in ENGINES}


def prompt_note(now: float | None = None) -> str:
    """모든 세션 프롬프트에 넣는 짧은 안내. 엔진별 표는 측정된 차단·미측정만 적는다."""
    data = _read()
    lines = []
    for engine in ENGINES:
        view = view_engine(engine, now=now, data=data)
        if view["measured_at"] is None:
            lines.append(f"- {engine}: not measured (all unknown)")
            continue
        blocked = [name for name, item in view["items"].items() if item["status"] == "blocked"]
        unknown = [name for name, item in view["items"].items() if item["status"] == "unknown"]
        part = f"- {engine} (measured {view['measured_at']}{', stale' if view['stale'] else ''}): "
        part += "blocked " + ", ".join(blocked) if blocked else "nothing blocked"
        if unknown:
            part += "; unknown " + ", ".join(unknown)
        lines.append(part)
    return (
        "[Execution environment capabilities — probe-measured per engine; unmeasured = unknown]\n"
        "- A result blocked inside a sandbox holds for that environment only. Never generalize it to \"the host cannot\"; "
        "name the engine/environment, command and error you observed.\n"
        "- Known limits by engine (full table: MCP execution_capabilities, or account_usage.execution_capabilities):\n"
        + "\n".join(lines) + "\n"
        f"- Measure your own environment: `python3 {probe_script()} --json`, then pass the JSON unchanged to "
        "report_execution_environment (it is recorded under your actual engine).\n"
        "- GPU/Metal work does not depend on your engine: use host_gpu_start/status/bench/stop (a host-side llama-server "
        "test instance outside any engine sandbox). Results from it are \"measured on the host GPU path\"; say so."
    )


def refresh_host(*, force: bool = False) -> dict[str, Any]:
    """백엔드 프로세스가 직접 프로브를 돌려 호스트 기준선을 갱신한다. 최근 값이 있으면 건너뛴다."""
    current = view_engine("host")
    if not force and current["age_seconds"] is not None and current["age_seconds"] < HOST_REFRESH_SECONDS:
        return current
    s = get_settings()
    done = subprocess.run(
        [sys.executable, str(Path(env_probe.__file__).resolve()), "--json", "--path", str(s.outbox_root),
         "--llama-server", str(Path(s.host_gpu_server_path).expanduser())],
        capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise ReportError(f"host probe failed: {(done.stderr or done.stdout).strip()[:200]}")
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise ReportError("host probe printed invalid JSON") from exc
    return record("host", payload, source="backend_probe")
