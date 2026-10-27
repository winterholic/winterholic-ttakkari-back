"""엔진(실행 경로)별 실행 환경 능력의 저장·노출·프롬프트 주입(ADR 0037).

값은 추측이 아니라 runs/env_probe.py 의 실측 결과만 받는다. 항목마다 측정 시각을 저장하고(신선도도 항목 단위), 측정하지 않은 항목은 unknown 이다.
엔진 환경은 그 엔진의 작업 세션이 프로브를 직접 돌려 MCP 도구로 보고하고(session_probe), 호스트 기준선은 백엔드가
자식 프로세스로 돌린다(backend_probe). 저장소는 세대(프로세스)와 무관한 파일이라 어느 세대가 받아도 같은 값을 본다.

세션 보고는 Run 별 일회용 challenge 로 묶는다: 세션이 begin_environment_probe 로 challenge 를 받고(세션·Run·엔진에 귀속, 15분, 1회용),
프로브를 `--challenge` 와 함께 돌리면 보고 전체에 HMAC 서명(digest)이 붙는다. 손으로 쓴 JSON 은 challenge 도 서명도 없어 거부된다.
한계: 프로브를 돌리는 쪽은 세션 셸이므로 일부러 서명을 계산하는 세션까지 막지는 못한다. 환각·손편집·재전송·다른 세션 보고를 막는 장치다.
"""
from __future__ import annotations

import contextlib
import fcntl
import hmac
import json
import os
import re
import secrets
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
ENGINES = ("claude", "codex", "antigravity", "muse", "opencode", "copilot", "qwen-local", "host")
STATUSES = ("available", "blocked", "unknown")
ITEMS = env_probe.ITEMS
STALE_SECONDS = 24 * 3600
HOST_REFRESH_SECONDS = 3600
HOST_RETRY_SECONDS = 300  # 호스트 프로브가 실패하면 이 시간 동안은 다시 돌리지 않는다(조회마다 60초 대기 방지)
HOST_PROBE_TIMEOUT = 60
CHALLENGE_TTL_SECONDS = 900
MAX_CHALLENGES_PER_SESSION = 5
CLOCK_SKEW = 300
MAX_DETAIL = 120
MAX_PATHS = 12
# 프로브가 내는 detail 은 오류 문구·경로·장치명뿐이다. 그 밖의 문자(줄바꿈·백틱·중괄호·$ 등)는 물음표로 바꿔 지시문을 심기 어렵게 한다.
_UNSAFE = re.compile(r"[^A-Za-z0-9 _.,:;()\[\]/=+\-'\"%#@*<>!?~]")


class ReportError(ValueError):
    pass


def store_path() -> Path:
    return get_settings().data_dir / "execution-capabilities.json"


def probe_script() -> Path:
    """세션이 읽을 수 있는 프로브 경로. 릴리스 사본은 보호 영역일 수 있어 소스 저장소를 우선한다."""
    source = HOME / "development/projects/winterholic-ttakkari/back/src/ttakkari/runs/env_probe.py"
    return source if source.is_file() else Path(env_probe.__file__).resolve()


def _clean(text: Any) -> str:
    return _UNSAFE.sub("?", " ".join(str(text).split()))[:MAX_DETAIL]


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


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def validate_report(payload: Any, *, not_before: float | None = None) -> dict[str, Any]:
    """프로브 JSON 을 정해진 모양으로만 받는다. 모르는 항목·상태는 거부하고 문자열은 정제해 자른다.
    not_before 가 있으면(세션 보고) 모든 측정 시각이 challenge 발급 이후여야 한다."""
    if not isinstance(payload, dict):
        raise ReportError("report must be the JSON object printed by env_probe.py")
    if payload.get("probe_version") != env_probe.PROBE_VERSION:
        raise ReportError(f"probe_version must be {env_probe.PROBE_VERSION}")
    raw_items = payload.get("items")
    if not isinstance(raw_items, dict) or not raw_items:
        raise ReportError("report.items is required")
    measured = _number(payload.get("measured_epoch"))
    if measured is None:
        raise ReportError("report.measured_epoch is required")
    now = time.time()

    def check(epoch: float, what: str) -> float:
        # 미래 시각이나 하루보다 오래된 측정은 새 보고로 받지 않는다(재전송·시계 오류 방지).
        if epoch > now + CLOCK_SKEW or now - epoch > STALE_SECONDS:
            raise ReportError(f"{what} must be within the last 24 hours")
        if not_before is not None and epoch < not_before - CLOCK_SKEW:
            raise ReportError(f"{what} predates the challenge; run the probe again after begin_environment_probe")
        return epoch

    measured = check(measured, "measured_epoch")
    items: dict[str, Any] = {}
    for name, value in raw_items.items():
        if name not in ITEMS:
            raise ReportError(f"unknown probe item: {name}")
        if not isinstance(value, dict) or value.get("status") not in STATUSES:
            raise ReportError(f"{name}.status must be one of {', '.join(STATUSES)}")
        epoch = _number(value.get("measured_epoch"))
        item: dict[str, Any] = {"status": value["status"], "detail": _clean(value.get("detail", "")),
                                "measured_epoch": check(epoch if epoch is not None else measured, f"{name}.measured_epoch")}
        paths = value.get("paths")
        if name == "writable_paths" and isinstance(paths, dict):
            item["paths"] = {_clean(k): bool(v) for k, v in list(paths.items())[:MAX_PATHS]}
        items[name] = item
    return {"measured_epoch": measured, "items": items, "platform": _clean(payload.get("platform", ""))}


def _merge(data: dict[str, Any], engine: str, report: dict[str, Any]) -> None:
    """항목 단위로 합친다. 늦게 도착한 옛 측정이 더 새 항목을 덮지 않는다."""
    previous = data.get(engine) or {}
    base_epoch = previous.get("measured_epoch", 0)
    items = dict(previous.get("items") or {})
    for name, item in report["items"].items():
        old = items.get(name)
        if old is not None and old.get("measured_epoch", base_epoch) > item["measured_epoch"]:
            continue
        items[name] = item
    newest = max([i.get("measured_epoch", base_epoch) for i in items.values()] or [report["measured_epoch"]])
    data[engine] = {**{k: v for k, v in report.items() if k != "items"}, "items": items, "measured_epoch": newest}


def record(engine: str, payload: Any, *, source: str, session_id: str | None = None,
           run_id: str | None = None) -> dict[str, Any]:
    """검증을 마친 프로브 결과를 저장한다. 세션 보고는 record_session_report 만 거친다(challenge·서명 검증)."""
    if engine not in ENGINES:
        raise ReportError(f"unknown engine: {engine}")
    report = validate_report(payload)
    report.update(source=source, reported_epoch=time.time(), session_id=session_id, run_id=run_id)
    with _locked():
        data = _read()
        _merge(data, engine, report)
        _write(data)
    return view_engine(engine)


def issue_challenge(engine: str, session_id: str, run_id: str) -> dict[str, Any]:
    """세션의 현재 Run 에 귀속된 일회용 값을 발급한다. 이 값으로 서명한 프로브 결과만 그 세션의 보고로 받는다."""
    if engine not in ENGINES or engine == "host":
        raise ReportError(f"engine {engine} cannot report from a session")
    now = time.time()
    nonce = secrets.token_hex(16)
    with _locked():
        data = _read()
        live = {k: v for k, v in (data.get("_challenges") or {}).items() if v.get("expires_epoch", 0) > now}
        mine = sorted((k for k, v in live.items() if v.get("session_id") == session_id),
                      key=lambda k: live[k]["issued_epoch"])
        for old in mine[:max(0, len(mine) - MAX_CHALLENGES_PER_SESSION + 1)]:
            del live[old]
        live[nonce] = {"session_id": session_id, "run_id": run_id, "engine": engine, "issued_epoch": now,
                       "expires_epoch": now + CHALLENGE_TTL_SECONDS}
        data["_challenges"] = live
        _write(data)
    return {"challenge": nonce, "expires_in_seconds": CHALLENGE_TTL_SECONDS, "engine": engine,
            "command": f"python3 {probe_script()} --json --challenge {nonce}"}


def record_session_report(session_id: str, payload: Any) -> dict[str, Any]:
    """세션이 낸 프로브 결과를 받는다. challenge 가 이 세션에 발급된 유효한 값이고, 서명이 맞고, 측정이 발급 이후일 때만 저장한다.
    엔진은 challenge 에 묶인 Run 의 엔진이며 보고 내용으로 정하지 않는다."""
    if not isinstance(payload, dict):
        raise ReportError("report must be the JSON object printed by env_probe.py")
    nonce, digest = payload.get("challenge"), payload.get("digest")
    if not isinstance(nonce, str) or not isinstance(digest, str):
        raise ReportError("report has no challenge/digest: call begin_environment_probe and run the command it returns "
                          "(hand-written or unsigned reports are rejected)")
    with _locked():
        data = _read()
        challenges = data.get("_challenges") or {}
        chal = challenges.get(nonce)
        if not chal or chal.get("session_id") != session_id or chal.get("expires_epoch", 0) < time.time():
            raise ReportError("challenge is unknown, expired, already used or issued to another session")
        if not hmac.compare_digest(digest, env_probe.attest(payload, nonce)):
            raise ReportError("digest mismatch: this is not unmodified output of the probe run with that challenge")
        report = validate_report(payload, not_before=chal["issued_epoch"])
        report.update(source="session_probe", reported_epoch=time.time(), session_id=session_id, run_id=chal["run_id"])
        _merge(data, chal["engine"], report)
        del challenges[nonce]  # 1회용
        data["_challenges"] = challenges
        _write(data)
    return view_engine(chal["engine"])


def _iso(epoch: float | None) -> str | None:
    return None if not epoch else datetime.fromtimestamp(epoch, KST).isoformat(timespec="seconds")


def _view_item(raw: dict[str, Any] | None, fallback_epoch: float | None, now: float) -> dict[str, Any]:
    if not raw:
        return {"status": "unknown", "detail": "not measured", "measured_at": None, "age_seconds": None, "stale": True}
    epoch = _number(raw.get("measured_epoch")) or fallback_epoch
    age = None if epoch is None else max(0, round(now - epoch))
    out = {"status": raw["status"] if raw.get("status") in STATUSES else "unknown", "detail": _clean(raw.get("detail", "")),
           "measured_at": _iso(epoch), "age_seconds": age, "stale": age is None or age > STALE_SECONDS}
    if isinstance(raw.get("paths"), dict):
        out["paths"] = {_clean(k): bool(v) for k, v in list(raw["paths"].items())[:MAX_PATHS]}
    return out


def view_engine(engine: str, *, now: float | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """엔진 하나의 표. 측정 시각과 오래됨(stale) 판정은 항목 단위다. 엔진의 measured_at 은 가장 최근 항목,
    stale 은 측정된 항목 중 하나라도 오래됐거나 아직 아무것도 측정되지 않았을 때 true 다."""
    now = time.time() if now is None else now
    data = data if data is not None else _read()
    entry = data.get(engine) or {}
    stored = entry.get("items") or {}
    fallback = _number(entry.get("measured_epoch"))
    items = {name: _view_item(stored.get(name), fallback, now) for name in ITEMS}
    measured = [i for i in items.values() if i["measured_at"] is not None]
    stale_items = [name for name, i in items.items() if i["measured_at"] is not None and i["stale"]]
    freshest = min((i["age_seconds"] for i in measured), default=None)
    out = {
        "engine": engine,
        "measured_at": None if freshest is None else _iso(now - freshest), "age_seconds": freshest,
        "stale": not measured or bool(stale_items), "stale_items": stale_items,
        "source": entry.get("source"),
        "items": items,
        "known_limitations": [f"{name}: {i['detail']}{' (stale)' if i['stale'] else ''}"
                              for name, i in items.items() if i["status"] == "blocked"],
        "scope_note": "Measured only for this execution environment; a blocked item does not mean the host cannot do it.",
    }
    refresh = (data.get("_meta") or {}).get("host_refresh")
    if engine == "host" and refresh and refresh.get("error"):
        out["refresh_error"] = {"at": _iso(refresh.get("attempted_epoch")), "message": _clean(refresh["error"])}
    return out


def compact(engine: str, now: float | None = None) -> dict[str, Any]:
    """배정 영수증용 요약: 측정 시각과 알려진 제한만. 전체 표는 snapshot 이다."""
    view = view_engine(engine, now=now)
    return {"engine": engine, "measured_at": view["measured_at"], "stale": view["stale"],
            "stale_items": view["stale_items"], "known_limitations": view["known_limitations"],
            "scope_note": view["scope_note"]}


def snapshot(now: float | None = None) -> dict[str, Any]:
    data = _read()
    return {engine: view_engine(engine, now=now, data=data) for engine in ENGINES}


def prompt_note(now: float | None = None) -> str:
    """모든 세션 프롬프트에 넣는 짧은 안내. 항목 이름과 상태만 넣고 보고된 detail 문자열은 절대 넣지 않는다."""
    data = _read()
    lines, unmeasured = [], []
    for engine in ENGINES:
        view = view_engine(engine, now=now, data=data)
        if view["measured_at"] is None:
            unmeasured.append(engine)
            continue
        items = view["items"]
        fresh = {n: i["status"] for n, i in items.items() if not i["stale"] or i["measured_at"] is None}
        blocked = [n for n, st in fresh.items() if st == "blocked"]
        unknown = [n for n, st in fresh.items() if st == "unknown"]
        part = f"- {engine} ({datetime.fromisoformat(view['measured_at']).strftime('%m-%d %H:%M')}): "
        part += "blocked " + ", ".join(blocked) if blocked else "nothing blocked"
        if view["stale_items"]:
            part += "; stale " + ", ".join(view["stale_items"])
        if unknown:
            part += "; unknown " + ", ".join(unknown)
        lines.append(part)
    if unmeasured:
        lines.append("- not measured: " + ", ".join(unmeasured))
    return (
        "[Execution environment capabilities, probe-measured per engine]\n"
        "- A result blocked inside a sandbox holds for that environment only; never generalize it to \"the host cannot\". "
        "Name the engine, command and error you observed.\n"
        "- Known limits (full table: MCP execution_capabilities):\n" + "\n".join(lines) + "\n"
        "- Measure yours: MCP begin_environment_probe, run the command it returns, pass the JSON unchanged to "
        "report_execution_environment (personal sessions only).\n"
        "- GPU/Metal work needs no particular engine: host_gpu_start/status/bench/stop run a llama-server on the host "
        "outside any sandbox. Call its results \"measured on the host GPU path\"."
    )


def refresh_host(*, force: bool = False) -> dict[str, Any]:
    """백엔드 프로세스가 직접 프로브를 돌려 호스트 기준선을 갱신한다. 최근 값이나 최근 실패가 있으면 건너뛴다.
    실패(타임아웃 포함)는 host 항목을 바꾸지 않고 refresh_error 로 기록하며 ReportError 로 알린다."""
    data = _read()
    current = view_engine("host", data=data)
    last = (data.get("_meta") or {}).get("host_refresh") or {}
    if not force and current["age_seconds"] is not None and current["age_seconds"] < HOST_REFRESH_SECONDS:
        return current
    if not force and last.get("error") and time.time() - last.get("attempted_epoch", 0) < HOST_RETRY_SECONDS:
        return current
    s = get_settings()
    try:
        done = subprocess.run(
            [sys.executable, str(Path(env_probe.__file__).resolve()), "--json", "--path", str(s.outbox_root),
             "--llama-server", str(Path(s.host_gpu_server_path).expanduser())],
            capture_output=True, text=True, timeout=HOST_PROBE_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        _note_host_refresh(f"host probe timed out after {int(exc.timeout or HOST_PROBE_TIMEOUT)}s")
        raise ReportError("host probe timed out") from exc
    except OSError as exc:
        _note_host_refresh(f"host probe could not start: {exc}")
        raise ReportError("host probe could not start") from exc
    if done.returncode != 0:
        message = f"host probe failed: {(done.stderr or done.stdout).strip()[:200]}"
        _note_host_refresh(message)
        raise ReportError(message)
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        _note_host_refresh("host probe printed invalid JSON")
        raise ReportError("host probe printed invalid JSON") from exc
    try:
        record("host", payload, source="backend_probe")
    except ReportError as exc:
        _note_host_refresh(f"host probe result rejected: {exc}")
        raise
    _note_host_refresh(None)
    return view_engine("host")


def _note_host_refresh(error: str | None) -> None:
    with _locked():
        data = _read()
        meta = data.get("_meta") or {}
        meta["host_refresh"] = {"attempted_epoch": time.time(), "error": error}
        data["_meta"] = meta
        _write(data)
