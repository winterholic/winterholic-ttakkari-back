"""ArtCraft 관제: Mac Studio 에 설치된 ArtCraft 앱 13개의 상태 조회와 제한된 조작.

디자인시스템 도메인과 무관하다. 이 모듈은 고정된 저장소(settings.artcraft_root)의 ``bin/artcraft`` 만 부른다.
- 명령은 아래 고정 목록으로만 만든다. 앱 이름은 registry.json 에 있는 값으로만 받고 셸을 거치지 않는다.
- 토큰·키·control 파일은 읽지도 응답에 담지도 않는다. registry 에서도 인증 "방식"만 꺼낸다.
- 느린 일(GitHub 조회, 스모크, 앱 기동·종료)은 백그라운드 Job 으로 돌리고 모두 제한시간이 있다.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import shlex
import signal
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ttakkari import artcraft_bugs
from ttakkari.config import get_settings

STATUS_TTL = 15.0
STATUS_TIMEOUT = 45.0
REMOTE_TIMEOUT = 90.0
REMOTE_TTL = 6 * 3600.0  # 미인증 GitHub API 는 시간당 60회, 한 번에 13회를 쓴다
REMOTE_MIN_INTERVAL = 120.0
REMOTE_RETRY_AFTER_FAIL = 900.0
GUI_START_TIMEOUT = 60.0
GUI_STOP_TIMEOUT = 20.0
MCP_CONFIG_TIMEOUT = 15.0
SMOKE_CLI_TIMEOUT = 300.0
SMOKE_FULL_TIMEOUT = 1500.0
OUTPUT_LIMIT = 1_000_000
TAIL_CHARS = 3000
JOB_KEEP = 40
NOTE_LIMIT = 500

URL_LIMIT = 300
HISTORY_KEEP = 20
SMOKE_MODES = ("cli", "full")
GUI_ACTIONS = ("start", "stop")

AUTH_LABELS = {
    "none-documented": ("none", "인증 없음", False),
    "token-file": ("token", "토큰 파일", True),
    "key-file": ("key", "키 파일", True),
    "control-file": ("file", "제어 파일(앱별 실행마다 생성)", True),
}
_APP_NAME = re.compile(r"^[a-z][a-z0-9]{1,31}$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ENV_VALUE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SECRETISH = re.compile(r"(?i)\b(token|secret|password|api[_-]?key|key)\b(\s*[:=]\s*)\S+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BUG_ID = re.compile(r"^BUG-\d{3}$")
_REPORT_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./#?=&%-]*)?$")
_OPEN_STATUSES = ("candidate", "selected", "in_progress", "reported")


class ArtcraftError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 사유를 가진 오류."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------- 저장소·registry

def root() -> Path:
    return Path(get_settings().artcraft_root).expanduser()


def _binary() -> Path:
    path = root() / "bin" / "artcraft"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ArtcraftError(503, "artcraft_unavailable", "ArtCraft 저장소의 bin/artcraft 를 실행할 수 없습니다.")
    return path


def registry() -> dict[str, dict[str, Any]]:
    """registry.json 의 앱 정의. 앱 이름 형식이 틀린 항목은 버린다."""
    try:
        raw = json.loads((root() / "registry.json").read_text("utf-8"))
        apps = raw["apps"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArtcraftError(503, "artcraft_unavailable", "ArtCraft registry.json 을 읽을 수 없습니다.") from exc
    return {k: v for k, v in apps.items() if isinstance(v, dict) and _APP_NAME.match(k)}


def known_app(app: str) -> str:
    if app not in registry():
        raise ArtcraftError(404, "unknown_app", "등록되지 않은 앱입니다.")
    return app


def _env() -> dict[str, str]:
    """자식 프로세스 환경. TTAKKARI_* 비밀을 넘기지 않고, 앱별 보정 변수(PHOTOCRAFT_NO_SYSTEM_FONTS 등)를 합친다."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TTAKKARI_")}
    paths = [p for p in env.get("PATH", "").split(":") if p]
    for extra in ("/usr/bin", "/bin", "/opt/homebrew/bin", "/usr/local/bin"):
        if extra not in paths:
            paths.append(extra)
    env["PATH"] = ":".join(paths)
    try:
        for app in registry().values():
            for key, value in (app.get("env") or {}).items():
                if _ENV_KEY.match(str(key)) and _ENV_VALUE.match(str(value)):
                    env[str(key)] = str(value)
    except ArtcraftError:
        pass
    return env


# ---------------------------------------------------------------- 명령 실행

@dataclass
class Result:
    rc: int
    out: str
    timed_out: bool = False


def clean_output(text: str) -> str:
    text = _CONTROL.sub("", _ANSI.sub("", text))
    return _SECRETISH.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


async def _exec(args: list[str], timeout: float) -> Result:
    """``bin/artcraft`` 를 고정 인자로 실행한다. args 는 이 모듈이 만든 고정 목록·검증된 앱 이름뿐이다."""
    binary = _binary()
    proc = await asyncio.create_subprocess_exec(
        str(binary), *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=_env(), cwd=str(root()), start_new_session=True)

    def kill() -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    try:
        data, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        kill()
        await proc.wait()
        return Result(124, "", True)
    except asyncio.CancelledError:
        kill()
        raise
    return Result(proc.returncode or 0, data[:OUTPUT_LIMIT].decode("utf-8", "replace"))


# ---------------------------------------------------------------- 상태 파일(버그 상태, 원격 캐시, 스모크 결과)

def _state_path() -> Path:
    return get_settings().data_dir / "artcraft" / "state.json"


@contextlib.contextmanager
def _locked():
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_state() -> dict[str, Any]:
    return _read_state(_state_path())


def update_state(mutate) -> dict[str, Any]:
    """파일 락 안에서 읽고 고쳐 원자적으로 쓴다. 세대 배포 중 프로세스가 겹쳐도 서로의 변경을 덮지 않는다."""
    with _locked() as path:
        state = _read_state(path)
        mutate(state)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, path)
        return state


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 버전·원격 최신 릴리스

def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    match = re.search(r"\d+(?:\.\d+)+", text or "")
    return tuple(int(p) for p in match.group(0).split(".")) if match else None


def _version_text(text: str | None) -> str | None:
    parts = _version_tuple(text)
    return ".".join(map(str, parts)) if parts else None


def update_available(installed: str | None, latest: str | None) -> bool | None:
    a, b = _version_tuple(installed), _version_tuple(latest)
    if a is None or b is None:
        return None
    return b > a


# ---------------------------------------------------------------- 상태 조회

_status_cache: tuple[float, list[dict[str, Any]]] | None = None
_status_error: str | None = None
_status_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _status_lock
    if _status_lock is None:
        _status_lock = asyncio.Lock()
    return _status_lock


def reset_caches() -> None:
    """테스트용."""
    global _status_cache, _status_error, _status_lock
    _status_cache, _status_error, _status_lock = None, None, None
    JOBS.clear()


def _parse_status(out: str) -> list[dict[str, Any]]:
    data = json.loads(out)
    apps = data["apps"]
    if not isinstance(apps, list):
        raise TypeError("apps")
    return [a for a in apps if isinstance(a, dict) and _APP_NAME.match(str(a.get("app", "")))]


async def raw_status(force: bool = False) -> tuple[list[dict[str, Any]], str | None, bool]:
    """(앱 상태 목록, 오류, 오래된 값 여부). 같은 때 여러 요청이 와도 명령은 하나만 돈다.

    캐시 시각은 명령을 시작한 때다. 요청한 뒤에 시작된 조회는 force 요청도 재사용한다.
    """
    global _status_cache, _status_error
    requested = time.monotonic()
    if not force and _status_cache is not None and requested - _status_cache[0] < STATUS_TTL:
        return _status_cache[1], None, False
    async with _lock():
        if _status_cache is not None and (_status_cache[0] >= requested or
                                          (not force and time.monotonic() - _status_cache[0] < STATUS_TTL)):
            return _status_cache[1], None, False
        started = time.monotonic()
        try:
            result = await _exec(["status", "--json", "--deep"], STATUS_TIMEOUT)
            if result.timed_out:
                raise ArtcraftError(504, "timeout", f"상태 조회가 {int(STATUS_TIMEOUT)}초 안에 끝나지 않았습니다.")
            _status_cache, _status_error = (started, _parse_status(result.out)), None
            return _status_cache[1], None, False
        except ArtcraftError as exc:
            error = exc.message
        except (ValueError, KeyError, TypeError):
            error = "상태 조회 결과를 해석하지 못했습니다."
        _status_error = error
        if _status_cache is not None:
            return _status_cache[1], error, True
        raise ArtcraftError(502, "status_failed", error)


def _smoke_summary(entry: dict[str, Any], ts: str | None, source: str) -> dict[str, Any]:
    tests = entry.get("tests") or []
    mcp, flow, gui = entry.get("mcp"), entry.get("mcp_flow"), entry.get("gui")
    return {
        "ts": ts, "source": source, "error": entry.get("error"),
        "passed": sum(1 for t in tests if t.get("ok")), "total": len(tests),
        "failed": [{"test": str(t.get("test")), "rc": t.get("rc"), "evidence": clean_output(str(t.get("evidence", "")))[:160]}
                   for t in tests if not t.get("ok")][:5],
        "mcp": {"ok": bool(mcp.get("ok")), "tools": mcp.get("tools")} if isinstance(mcp, dict) else None,
        "mcp_flow": {"ok": bool(flow.get("ok")), "calls": flow.get("calls")} if isinstance(flow, dict) else None,
        "gui": {"started": gui.get("started"), "inspect": gui.get("inspect"), "screenshot": gui.get("screenshot")}
        if isinstance(gui, dict) else None,
    }


def _smoke_file() -> dict[str, dict[str, Any]]:
    """artcraft 가 마지막으로 쓴 smoke/results/latest.json 과, 이 화면이 돌린 결과 중 더 새것을 앱별로 고른다."""
    result: dict[str, dict[str, Any]] = {}
    try:
        data = json.loads((root() / "smoke" / "results" / "latest.json").read_text("utf-8"))
        for app, entry in (data.get("apps") or {}).items():
            if isinstance(entry, dict):
                result[app] = _smoke_summary(entry, data.get("ts"), "artcraft")
    except (OSError, ValueError, AttributeError):
        pass
    for app, mine in (load_state().get("smoke") or {}).items():
        if isinstance(mine, dict) and (app not in result or str(mine.get("ts") or "") >= str(result[app].get("ts") or "")):
            result[app] = mine
    return result


def _known_bugs() -> dict[str, list[str]]:
    """앱 카드의 BUG 칩. BUGS.md 의 Apps 목록 기준이고, 해결·보류·제외로 정리한 항목은 뺀다."""
    try:
        bugs, _ = _read_bugs()
    except ArtcraftError:
        return {}
    saved = load_state().get("bugs") or {}
    found: dict[str, list[str]] = {}
    for bug in bugs:
        if _status_of(saved.get(bug["id"])) not in _OPEN_STATUSES:
            continue
        for app in bug["apps"]:
            found.setdefault(app, []).append(bug["id"])
    return found


def _app_view(raw: dict[str, Any], reg: dict[str, Any], remote: dict[str, Any], smoke: dict[str, Any],
              bugs: dict[str, list[str]]) -> dict[str, Any]:
    name = raw["app"]
    meta = reg.get(name, {})
    gui, cli, bundle, source, mcp = (raw.get(k) or {} for k in ("gui", "cli", "bundle", "source", "mcp"))
    auth_key = str(meta.get("control_auth") or gui.get("control_auth") or "none-documented")
    mode, label, protected = AUTH_LABELS.get(auth_key, ("unknown", "알 수 없음", False))
    port = gui.get("control_port")
    release = bundle.get("version") or _version_text(cli.get("version_line"))
    latest = (remote.get("apps") or {}).get(name) or {}
    tag = latest.get("tag")
    title = str(raw.get("title") or name)
    return {
        "app": name, "title": title, "domain": raw.get("domain"), "site_status": raw.get("site_status"),
        "installed": {
            "release": release, "cli_ok": bool(cli.get("ok")), "cli_version": cli.get("version_line"),
            "bundle_installed": bool(bundle.get("installed")),
            "source_build": {"commit": source.get("commit"), "version": source.get("built_version")}
            if source.get("built_version") or source.get("commit") else None,
        },
        "latest": {"tag": tag, "update_available": update_available(release, tag) if tag else None,
                   "checked_at": remote.get("checked_at")},
        "smoke": smoke.get(name),
        "mcp": {"ok": bool(mcp.get("ok")), "tool_count": mcp.get("tool_count"), "ms": mcp.get("ms"),
                "error": str(mcp["error"])[:200] if mcp.get("error") else None} if mcp else None,
        "gui": {"running": bool(gui.get("running")), "listening": bool(gui.get("listening")),
                "port": port if isinstance(port, int) else None,
                "transport": "tcp" if isinstance(port, int) else "file" if auth_key == "control-file" else "none"},
        "auth": {"mode": mode, "label": label, "protected": protected},
        "healthy": bool(raw.get("healthy")),
        "known_bugs": bugs.get(name, []),
    }


def remote_view() -> dict[str, Any]:
    remote = load_state().get("remote") or {}
    return {"checked_at": remote.get("checked_at"), "attempted_at": remote.get("attempted_at"),
            "error": remote.get("error"), "checking": any(j.kind == "remote" and j.active for j in JOBS.values())}


async def overview(force: bool = False) -> dict[str, Any]:
    apps, error, stale = await raw_status(force)
    reg = registry()
    state = load_state()
    remote = state.get("remote") or {}
    smoke, bugs = _smoke_file(), _known_bugs()
    # 레지스트리 순서를 따른다(status 가 이미 그 순서다)
    views = [_app_view(a, reg, remote, smoke, bugs) for a in apps if a["app"] in reg]
    maybe_auto_remote_check(remote)
    return {"generated_at": _now(), "stale": stale, "error": error, "apps": views, "remote": remote_view(),
            "jobs": [j.public() for j in list(JOBS.values())[-8:]][::-1]}


# ---------------------------------------------------------------- Job

@dataclass
class Job:
    id: str
    kind: str  # remote | smoke | gui
    action: str
    apps: list[str]
    mode: str | None = None
    state: str = "queued"  # queued | running | succeeded | failed | timeout
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    summary: str | None = None
    output: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "action": self.action, "apps": self.apps, "mode": self.mode,
                "state": self.state, "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at, "summary": self.summary, "output": self.output}


JOBS: OrderedDict[str, Job] = OrderedDict()


def _conflict(kind: str, apps: list[str], mode: str | None) -> Job | None:
    for job in JOBS.values():
        if not job.active:
            continue
        if kind == "smoke" and job.kind == "smoke":
            return job
        if kind == "gui" and job.kind == "gui" and set(apps) & set(job.apps):
            return job
        if "full" in (mode, job.mode) and {kind, job.kind} == {"smoke", "gui"}:
            return job
    return None


def _start(kind: str, action: str, apps: list[str], mode: str | None, runner) -> Job:
    other = _conflict(kind, apps, mode)
    if other is not None:
        raise ArtcraftError(409, "busy", "다른 작업이 아직 진행 중입니다. 끝난 뒤 다시 시도하세요.")
    job = Job(id=uuid.uuid4().hex[:12], kind=kind, action=action, apps=apps, mode=mode)
    JOBS[job.id] = job
    while len(JOBS) > JOB_KEEP:
        oldest = next((k for k, j in JOBS.items() if not j.active), None)
        if oldest is None:
            break
        del JOBS[oldest]

    async def run() -> None:
        job.state, job.started_at = "running", _now()
        try:
            await runner(job)
        except ArtcraftError as exc:
            job.state, job.summary = "failed", exc.message
        except asyncio.CancelledError:
            job.state, job.summary = "failed", "서버가 종료되어 중단되었습니다."
            raise
        except Exception:  # noqa: BLE001 - Job 하나의 실패가 서버를 흔들지 않게 한다
            job.state, job.summary = "failed", "예기치 않은 오류로 실패했습니다."
        finally:
            job.finished_at = _now()

    job.task = asyncio.get_running_loop().create_task(run())
    return job


def get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise ArtcraftError(404, "unknown_job", "작업 기록이 없습니다. 서버가 다시 시작되면 기록이 지워집니다.")
    return job


def _finish(job: Job, result: Result, ok_summary: str, fail_summary: str, timeout: float) -> None:
    job.output = clean_output(result.out)[-TAIL_CHARS:]
    if result.timed_out:
        job.state, job.summary = "timeout", f"{int(timeout)}초 안에 끝나지 않아 중단했습니다."
    elif result.rc == 0:
        job.state, job.summary = "succeeded", ok_summary
    else:
        job.state, job.summary = "failed", fail_summary


# -- 원격 최신 릴리스

def _parse_remote(out: str) -> dict[str, dict[str, Any]]:
    return {a["app"]: {"tag": a.get("latest_release")} for a in _parse_status(out)}


async def _run_remote(job: Job) -> None:
    started = _now()
    result = await _exec(["status", "--json", "--remote"], REMOTE_TIMEOUT)
    job.output = clean_output(result.out)[-300:] if result.rc else ""
    tags: dict[str, dict[str, Any]] = {}
    if not result.timed_out and result.rc == 0:
        with contextlib.suppress(ValueError, KeyError, TypeError):
            tags = {k: v for k, v in _parse_remote(result.out).items() if v.get("tag")}
    error = None if tags else ("GitHub 조회 시간이 초과되었습니다." if result.timed_out
                               else "GitHub 릴리스를 조회하지 못했습니다(속도 제한이거나 네트워크 문제일 수 있습니다).")

    def mutate(state: dict[str, Any]) -> None:
        remote = state.setdefault("remote", {})
        remote["attempted_at"], remote["error"] = started, error
        if tags:
            # 일부만 실패해도 이전 값을 지우지 않는다
            remote.setdefault("apps", {}).update(tags)
            remote["checked_at"] = _now()

    update_state(mutate)
    job.state = "succeeded" if tags else "failed"
    job.summary = f"{len(tags)}개 앱의 최신 릴리스를 확인했습니다." if tags else error


def start_remote_check(manual: bool) -> Job:
    running = next((j for j in JOBS.values() if j.kind == "remote" and j.active), None)
    if running is not None:
        return running
    attempted = (load_state().get("remote") or {}).get("attempted_at")
    if manual and attempted:
        wait = REMOTE_MIN_INTERVAL - (datetime.now(UTC) - datetime.fromisoformat(attempted)).total_seconds()
        if wait > 0:
            raise ArtcraftError(429, "rate_limited",
                                f"GitHub 조회 한도를 아끼려고 {int(wait) + 1}초 뒤에 다시 확인할 수 있습니다.")
    return _start("remote", "remote-check", [], None, _run_remote)


def maybe_auto_remote_check(remote: dict[str, Any]) -> None:
    if not get_settings().artcraft_auto_remote_check:
        return
    now = datetime.now(UTC)

    def age(key: str) -> float:
        value = remote.get(key)
        return (now - datetime.fromisoformat(value)).total_seconds() if value else float("inf")

    if age("checked_at") > REMOTE_TTL and age("attempted_at") > REMOTE_RETRY_AFTER_FAIL:
        with contextlib.suppress(ArtcraftError):
            start_remote_check(manual=False)


# -- 스모크

def _clean_apps(apps: list[str] | None) -> list[str]:
    reg = list(registry())
    if not apps:
        return reg
    unknown = [a for a in apps if a not in reg]
    if unknown:
        raise ArtcraftError(404, "unknown_app", "등록되지 않은 앱이 있습니다.")
    return [a for a in reg if a in set(apps)]


async def _running_apps(apps: list[str]) -> list[str]:
    status, _, _ = await raw_status(force=True)
    return [a["app"] for a in status if a["app"] in apps
            and ((a.get("gui") or {}).get("running") or (a.get("gui") or {}).get("listening"))]


async def start_smoke(apps: list[str] | None, mode: str, confirm: bool) -> Job:
    if mode not in SMOKE_MODES:
        raise ArtcraftError(422, "validation", "스모크 모드는 cli 또는 full 입니다.")
    selected = _clean_apps(apps)
    if mode == "full":
        if not confirm:
            raise ArtcraftError(428, "confirm_required",
                                "GUI 포함 스모크는 앱 창을 열고 닫습니다. 확인 후 다시 요청하세요.")
        busy = await _running_apps(selected)
        if busy:
            raise ArtcraftError(409, "app_running",
                                "실행 중인 앱은 스모크가 창을 닫아 버립니다. 먼저 종료하세요: " + ", ".join(busy))
    timeout = SMOKE_FULL_TIMEOUT if mode == "full" else SMOKE_CLI_TIMEOUT

    async def run(job: Job) -> None:
        latest = root() / "smoke" / "results" / "latest.json"
        before = latest.read_bytes() if latest.is_file() else None
        args = ["smoke", *selected] + ([] if mode == "full" else ["--no-gui"])
        try:
            result = await _exec(args, timeout)
        finally:
            _absorb_smoke(before, latest)
        _finish(job, result, "스모크를 마쳤습니다.", "일부 스모크가 실패했습니다.", timeout)
        summary = [_smoke_file().get(a) for a in selected]
        done = [s for s in summary if s]
        if done and job.state != "timeout":
            passed, total = sum(s["passed"] for s in done), sum(s["total"] for s in done)
            job.summary = f"CLI 테스트 {passed}/{total} 통과" + ("" if job.state == "succeeded" else " (실패 있음)")

    return _start("smoke", f"smoke-{mode}", selected, mode, run)


def _absorb_smoke(before: bytes | None, latest: Path) -> None:
    """스모크는 smoke/results/latest.json 전체를 덮어쓴다. 부분 실행 결과가 13앱 기준 기록을 지우지 않도록
    새 결과는 이 화면의 상태 파일로 옮기고 원래 파일 내용은 되돌린다."""
    try:
        after = latest.read_bytes()
    except OSError:
        return
    if after == before:
        return
    try:
        data = json.loads(after.decode("utf-8"))
        entries = {a: e for a, e in (data.get("apps") or {}).items() if isinstance(e, dict)}
    except (ValueError, AttributeError):
        entries = {}
    ts = data.get("ts") if entries else None

    def mutate(state: dict[str, Any]) -> None:
        store = state.setdefault("smoke", {})
        for app, entry in entries.items():
            if _APP_NAME.match(app):
                store[app] = _smoke_summary(entry, ts, "ttakkari")

    if entries:
        update_state(mutate)
    if before is not None:
        tmp = latest.with_suffix(".restore")
        tmp.write_bytes(before)
        os.replace(tmp, latest)


# -- 앱 실행·종료

async def start_gui(app: str, action: str, confirm: bool) -> Job:
    known_app(app)
    if action not in GUI_ACTIONS:
        raise ArtcraftError(422, "validation", "동작은 start 또는 stop 입니다.")
    if action == "stop" and not confirm:
        raise ArtcraftError(428, "confirm_required",
                            "앱을 종료하면 저장하지 않은 작업이 사라질 수 있습니다. 확인 후 다시 요청하세요.")
    status, _, _ = await raw_status(force=True)
    gui = next((a.get("gui") or {} for a in status if a["app"] == app), {})
    if action == "start" and gui.get("running"):
        raise ArtcraftError(409, "already_running", "이미 실행 중입니다.")
    if action == "start" and gui.get("listening"):
        raise ArtcraftError(409, "port_in_use", "제어 포트가 이미 열려 있습니다. 다른 곳에서 켠 앱일 수 있습니다.")
    if action == "stop" and not gui.get("running"):
        raise ArtcraftError(409, "not_running", "관제가 켠 앱이 아니어서 종료할 수 없습니다."
                            if gui.get("listening") else "실행 중이 아닙니다.")
    timeout = GUI_START_TIMEOUT if action == "start" else GUI_STOP_TIMEOUT

    async def run(job: Job) -> None:
        result = await _exec(["gui", action, app], timeout)
        label = "실행" if action == "start" else "종료"
        _finish(job, result, f"{label}했습니다.", f"{label}하지 못했습니다.", timeout)
        if action == "start" and job.state == "failed":
            job.summary = "창을 띄웠지만 제어 포트가 응답하지 않았습니다(화면 잠금이면 풀고 다시 시도)."
        await raw_status(force=True)  # 목록이 바로 새 상태를 보게 한다

    return _start("gui", f"gui-{action}", [app], None, run)


# ---------------------------------------------------------------- 버그 후보

def _read_bugs() -> tuple[list[dict[str, Any]], str | None]:
    path = root() / "BUGS.md"
    try:
        text = path.read_text("utf-8")
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")
    except OSError as exc:
        raise ArtcraftError(503, "artcraft_unavailable", "BUGS.md 를 읽을 수 없습니다.") from exc
    try:
        names = set(registry())
    except ArtcraftError:
        names = set()
    return artcraft_bugs.parse_bugs(text, names), modified


def _status_of(saved: Any) -> str:
    """저장된 상태. 예전 값(unreported)은 candidate 로 읽는다."""
    value = (saved or {}).get("status") if isinstance(saved, dict) else None
    value = "candidate" if value == "unreported" else value
    return value if value in artcraft_bugs.STATUSES else "candidate"


def _bug_view(bug: dict[str, Any], saved: Any) -> dict[str, Any]:
    mine = saved if isinstance(saved, dict) else {}
    out = {**bug, "status": _status_of(mine), "note": mine.get("note") or "", "url": mine.get("url") or "",
           "updated_at": mine.get("updated_at"), "history": list(mine.get("history") or [])[-HISTORY_KEEP:]}
    out["handoff"] = artcraft_bugs.handoff(bug)
    return out


def list_bugs() -> dict[str, Any]:
    bugs, modified = _read_bugs()
    saved = load_state().get("bugs") or {}
    views = [_bug_view(b, saved.get(b["id"])) for b in bugs]
    return {
        "source_modified_at": modified,
        "counts": {s: sum(1 for b in views if b["status"] == s) for s in artcraft_bugs.STATUSES},
        "apps": sorted({a for b in views for a in b["apps"]}),
        "problems": sum(1 for b in views if b["problems"]),
        "bugs": views,
    }


def set_bug(bug_id: str, status: str, note: str | None, url: str | None = None) -> dict[str, Any]:
    if status not in artcraft_bugs.STATUSES:
        raise ArtcraftError(422, "validation", "알 수 없는 상태입니다.")
    bugs, _ = _read_bugs()
    if bug_id not in {b["id"] for b in bugs}:
        raise ArtcraftError(404, "unknown_bug", "BUGS.md 에 없는 항목입니다.")
    clean_note = _CONTROL.sub("", note or "").strip()[:NOTE_LIMIT]
    clean_url = (url or "").strip()
    if clean_url and (len(clean_url) > URL_LIMIT or not _REPORT_URL.match(clean_url)):
        raise ArtcraftError(422, "validation", "링크는 https://github.com/ 주소만 저장할 수 있습니다.")

    def mutate(state: dict[str, Any]) -> None:
        store = state.setdefault("bugs", {})
        old = store.get(bug_id) if isinstance(store.get(bug_id), dict) else {}
        history = list(old.get("history") or [])
        if _status_of(old) != status or not history:
            history.append({"status": status, "at": _now()})
        store[bug_id] = {"status": status, "note": clean_note, "url": clean_url, "updated_at": _now(),
                         "history": history[-HISTORY_KEEP:]}

    update_state(mutate)
    return next(b for b in list_bugs()["bugs"] if b["id"] == bug_id)


# ---------------------------------------------------------------- 에이전트 연결 정보

def _snippets(name: str, server: dict[str, Any]) -> dict[str, str]:
    command, args = str(server["command"]), [str(a) for a in server.get("args", [])]
    key = f"artcraft-{name}"
    return {
        "json": json.dumps({"mcpServers": {key: {"command": command, "args": args, "env": {}}}}, indent=2),
        "claude": "claude mcp add " + shlex.join([key, "--", command, *args]),
        "codex": f"[mcp_servers.{key}]\ncommand = {json.dumps(command)}\nargs = {json.dumps(args)}\n",
    }


async def agent_config() -> dict[str, Any]:
    result = await _exec(["mcp-config"], MCP_CONFIG_TIMEOUT)
    try:
        servers = json.loads(result.out)["mcpServers"]
        if result.rc or result.timed_out or not isinstance(servers, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError) as exc:
        raise ArtcraftError(502, "mcp_config_failed", "MCP 설정을 만들지 못했습니다.") from exc
    reg = registry()
    tools: dict[str, Any] = {}
    if _status_cache is not None:
        tools = {a["app"]: (a.get("mcp") or {}).get("tool_count") for a in _status_cache[1]}
    items = []
    for name, meta in reg.items():
        server = servers.get(f"artcraft-{name}")
        if not isinstance(server, dict) or "command" not in server:
            continue
        notes = []
        if meta.get("env"):
            notes.append("bin/artcraft 가 " + ", ".join(f"{k}={v}" for k, v in meta["env"].items()) + " 를 자동으로 적용합니다.")
        if tools.get(name) and tools[name] > 100:
            notes.append(f"도구가 {tools[name]}개로 많습니다. 에이전트 컨텍스트 사용량에 유의하세요.")
        items.append({"app": name, "title": meta.get("title", name), "server": f"artcraft-{name}",
                      "tool_count": tools.get(name), "snippets": _snippets(name, server), "notes": notes})
    everything = {"mcpServers": {k: v for k, v in servers.items() if isinstance(v, dict)}}
    return {"apps": items, "all": {"json": json.dumps(everything, indent=2)},
            "note": "MCP 서버는 stdio 이고 앱 UI 제어 포트는 쓰지 않습니다. 에이전트를 실행하는 같은 Mac 에서만 동작합니다."}
