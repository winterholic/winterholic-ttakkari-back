"""접수 상주화(warm pool): 계정마다 엔진 프로세스를 하나 미리 띄워 두고, 메시지가 오면 그 프로세스에 넣는다.

- 2026-10-13 실측(중앙값): luna 5.7초 → 2.6초, haiku 4.7초 → 3.3초. haiku 는 지연 대부분이 모델 시간이라 줄어든 폭이 작다.
- Codex 는 `codex app-server`(stdio JSON-RPC)로 띄워 스레드까지 미리 만든다. `codex exec` 는 프롬프트를 받은 뒤에야
  준비를 시작해서 미리 띄울 단계가 없다. Claude 는 `--input-format stream-json` 으로 띄우면 초기화 뒤 stdin 을 기다린다.
- 한 번 쓴 프로세스는 버리고 새로 띄운다. 접수는 메시지마다 새 대화여야 한다(앞 메시지가 다음 판단에 섞이지 않게).
- 준비된 프로세스가 없거나, 죽었거나, 지침·모델이 바뀌었거나, 주고받다가 프로세스 쪽 문제(EOF·파이프·프로토콜)가 나면
  None 을 돌려 cold 경로(desk._ask 의 1회 실행)로 간다. 모델이 낸 결과(한도·오류·시간 초과)는 cold 와 같은 Attempt 로
  돌려준다. 다시 부르면 사용량을 두 번 쓴다.
- 띄워 둔 동안에는 모델 호출이 없다(실측). 기동 때 계정 조회·텔레메트리 같은 메타 요청만 나간다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from ttakkari.config import get_settings
from ttakkari.runs import desk
from ttakkari.runs import profiles as profiles_mod

log = logging.getLogger(__name__)

_STDERR_TAIL = 2000
_LIMIT_INFOS = {"usageLimitExceeded", "rateLimitExceeded"}


@dataclass
class _Warm:
    key: tuple
    proc: asyncio.subprocess.Process
    born: float
    msgs: asyncio.Queue = field(default_factory=asyncio.Queue)
    thread_id: str | None = None
    next_id: int = 0
    stderr: bytearray = field(default_factory=bytearray)
    tasks: list[asyncio.Task] = field(default_factory=list)

    def alive(self) -> bool:
        return self.proc.returncode is None and not (self.tasks and self.tasks[0].done())


_warm: dict[uuid.UUID, _Warm] = {}
_spawning: dict[uuid.UUID, asyncio.Task] = {}
_last_used: dict[uuid.UUID, float] = {}


async def ask(prof: profiles_mod.Profile, system: str, prompt: str) -> desk.Attempt | None:
    """띄워 둔 프로세스로 답을 받는다. None 이면 부른 쪽이 cold 로 다시 한다."""
    s = get_settings()
    key = _key(prof, system)
    _last_used[prof.id] = time.monotonic()
    w = _warm.pop(prof.id, None)
    # 다음 메시지용은 바로 띄우기 시작한다. 이 요청이 끝나기 전에 온 다음 메시지도 상주 프로세스를 쓸 수 있게.
    _schedule(prof, key, system)
    if w is None:
        return None
    try:
        if w.key != key or not w.alive() or time.monotonic() - w.born > s.desk_warm_max_age_seconds:
            return None
        start = time.monotonic()
        exchange = _claude_exchange(w, prompt) if prof.engine == "claude" else _codex_exchange(w, prompt)
        try:
            res = await asyncio.wait_for(exchange, s.desk_timeout_seconds)
        except TimeoutError:
            return desk.Attempt(None, f"시간 초과({s.desk_timeout_seconds:g}초)", int((time.monotonic() - start) * 1000),
                                warm=True)
        except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.info("desk warm %s 실패, cold 로: %s", prof.name, e)
            return None
        if res is None:
            log.info("desk warm %s 프로세스가 답 전에 끝남, cold 로: %s", prof.name, _tail(w))
            return None
        text, error = res
        if text is None:
            error = error or _tail(w) or f"exit code {w.proc.returncode}"
        return desk.Attempt(text, error, int((time.monotonic() - start) * 1000), warm=True)
    finally:
        _discard(w)


async def shutdown() -> None:
    for t in list(_spawning.values()):
        t.cancel()
        with contextlib.suppress(BaseException):
            await t
    _spawning.clear()
    for w in list(_warm.values()):
        _discard(w)
    _warm.clear()
    _last_used.clear()


# ---- 띄우기·버리기

def _key(prof: profiles_mod.Profile, system: str) -> tuple:
    s = get_settings()
    cfg = s.desk_models.get(prof.engine) or {}
    mcp = tuple(desk._codex_mcp_servers(prof)) if prof.engine == "codex" else ()
    binary = s.codex_bin if prof.engine == "codex" else s.claude_bin
    return (prof.engine, str(prof.config_dir), binary, system, cfg.get("model"), cfg.get("effort"), mcp, str(s.home))


def _schedule(prof: profiles_mod.Profile, key: tuple, system: str) -> None:
    if prof.id in _spawning or prof.id in _warm:
        return
    task = asyncio.create_task(_spawn(prof, key, system))
    _spawning[prof.id] = task
    task.add_done_callback(lambda _t: _spawning.pop(prof.id, None) if _spawning.get(prof.id) is _t else None)


async def _spawn(prof: profiles_mod.Profile, key: tuple, system: str) -> None:
    s = get_settings()
    cfg = s.desk_models.get(prof.engine) or {}
    cwd = s.home / "desk"
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    if prof.engine == "codex":
        cmd = [s.codex_bin, "app-server", "--disable", "hooks"]
        for name in desk._codex_mcp_servers(prof):
            cmd += ["-c", f"mcp_servers.{name}.enabled=false"]
    else:
        cmd = [s.claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               *desk._claude_flags(cfg, system)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=desk._env(prof), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=1 << 24)
    except OSError as e:
        log.info("desk warm %s 띄우기 실패: %s", prof.name, e)
        return
    w = _Warm(key, proc, time.monotonic())
    w.tasks = [asyncio.create_task(_read(w)), asyncio.create_task(_drain(w))]
    try:
        if prof.engine == "codex":
            await asyncio.wait_for(_codex_ready(w, cfg, system, cwd), s.desk_timeout_seconds)
    except asyncio.CancelledError:
        _discard(w)
        raise
    except Exception as e:  # noqa: BLE001 — 띄우기 실패는 cold 로 대신한다
        log.info("desk warm %s 준비 실패: %s %s", prof.name, e, _tail(w))
        _discard(w)
        return
    old = _warm.pop(prof.id, None)
    if old is not None:
        _discard(old)
    _warm[prof.id] = w
    w.tasks.append(asyncio.create_task(_expire(prof, w, system)))


async def _expire(prof: profiles_mod.Profile, w: _Warm, system: str) -> None:
    s = get_settings()
    await asyncio.sleep(s.desk_warm_max_age_seconds)
    if _warm.get(prof.id) is not w:
        return
    _warm.pop(prof.id, None)
    if time.monotonic() - _last_used.get(prof.id, 0.0) < s.desk_warm_idle_seconds:
        _schedule(prof, _key(prof, system), system)
    _discard(w)  # 이 타이머도 w.tasks 에 있어 여기서 같이 취소된다. 그래서 맨 끝에 둔다.


def _discard(w: _Warm) -> None:
    if w.proc.stdin and not w.proc.stdin.is_closing():
        w.proc.stdin.close()
    desk._kill(w.proc)
    for t in w.tasks:
        t.cancel()


async def _read(w: _Warm) -> None:
    assert w.proc.stdout is not None
    try:
        while line := await w.proc.stdout.readline():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                w.msgs.put_nowait(obj)
    finally:
        w.msgs.put_nowait(None)


async def _drain(w: _Warm) -> None:
    assert w.proc.stderr is not None
    while chunk := await w.proc.stderr.read(4096):
        w.stderr += chunk
        del w.stderr[:-_STDERR_TAIL]


def _tail(w: _Warm) -> str:
    return w.stderr.decode("utf-8", "replace").strip()[-500:]


async def _write(w: _Warm, obj: dict) -> None:
    assert w.proc.stdin is not None
    w.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
    await w.proc.stdin.drain()


# ---- Claude: stream-json 입력

async def _claude_exchange(w: _Warm, prompt: str) -> tuple[str | None, str | None] | None:
    await _write(w, {"type": "user", "message": {"role": "user", "content": prompt}})
    while (obj := await w.msgs.get()) is not None:
        if obj.get("type") == "result":
            return desk._claude_result(json.dumps(obj))
    return None


# ---- Codex: app-server JSON-RPC

async def _rpc(w: _Warm, method: str, params: dict) -> int:
    w.next_id += 1
    await _write(w, {"jsonrpc": "2.0", "id": w.next_id, "method": method, "params": params})
    return w.next_id


async def _reply(w: _Warm, rid: int) -> dict:
    while (obj := await w.msgs.get()) is not None:
        if obj.get("id") == rid and "method" not in obj:
            if obj.get("error"):
                raise ValueError(f"app-server 오류: {obj['error']}")
            return obj.get("result") or {}
    raise EOFError("app-server 가 끝났습니다")


async def _codex_ready(w: _Warm, cfg: dict[str, str], system: str, cwd) -> None:
    await _reply(w, await _rpc(w, "initialize", {"clientInfo": {"name": "ttakkari-desk", "version": "1"}}))
    await _write(w, {"jsonrpc": "2.0", "method": "initialized"})
    params: dict = {"cwd": str(cwd), "ephemeral": True, "sandbox": "read-only", "approvalPolicy": "never",
                    "developerInstructions": system}
    if cfg.get("model"):
        params["model"] = cfg["model"]
    if cfg.get("effort"):
        params["config"] = {"model_reasoning_effort": cfg["effort"]}
    res = await _reply(w, await _rpc(w, "thread/start", params))
    w.thread_id = res["thread"]["id"]


async def _codex_exchange(w: _Warm, prompt: str) -> tuple[str | None, str | None] | None:
    rid = await _rpc(w, "turn/start", {"threadId": w.thread_id, "input": [{"type": "text", "text": prompt}]})
    error: str | None = None
    while (obj := await w.msgs.get()) is not None:
        method, params = obj.get("method"), obj.get("params") or {}
        if obj.get("id") == rid and method is None and obj.get("error"):
            # 턴을 시작도 못 했으면 프로토콜 문제다. cold 로 다시 한다.
            return None
        if method == "item/completed" and (params.get("item") or {}).get("type") == "agentMessage":
            return (params["item"].get("text") or "").strip() or None, None
        if method == "error" and not params.get("willRetry"):
            error = _turn_error(params.get("error")) or error
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            return None, _turn_error(turn.get("error")) or error or f"turn {turn.get('status') or 'failed'}"
    return None


def _turn_error(err: dict | None) -> str | None:
    if not isinstance(err, dict):
        return None
    msg = err.get("message") or ""
    # 문구가 바뀌어도 한도로 알아보게(profiles.is_usage_limit 는 문구를 본다).
    if err.get("codexErrorInfo") in _LIMIT_INFOS and not profiles_mod.is_usage_limit(msg):
        msg = f"{msg} (usage limit)".strip()
    return msg or None
