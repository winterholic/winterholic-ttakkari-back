"""접수(desk): 사용자 메시지마다 가벼운 모델이 바로 답하거나 기획 세션에 넘긴다(ADR 0013).

- 전역 하네스를 받지 않는다. Codex 는 `--disable hooks`·사용자 MCP 끄기·빈 작업 폴더, Claude 는 hook 끄기·`--setting-sources local`·
  도구 없음으로 띄운다. 지침은 비서 관리 > 지침 > 접수만 쓴다.
- 도구 없이 한 번에 고르게 한다. 작업판(기획 세션 상태)과 최근 대화는 백엔드가 미리 넣는다. 작은 모델은 여러 번 오가는
  도구 호출에 약하다(2026-10-13 실측).
- 계정은 엔진 순서(desk_engine_order, 기본 luna → haiku) 안에서 기존 우선순위대로. 한도·시간 초과·출력 오류면 다음 계정으로,
  다 실패하면 기획으로 넘긴다. 접수 때문에 사용자 메시지가 멈추는 일은 없게 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    SessionKind,
)
from ttakkari.runs import events
from ttakkari.runs import instructions as instructions_mod
from ttakkari.runs import profiles as profiles_mod

log = logging.getLogger(__name__)

BOARD_LEADS = 8
HISTORY_TURNS = 6
HISTORY_CHARS = 500
LEAD_HISTORY_TURNS = 4
MAX_ATTEMPTS = 4
FALLBACK_REPLY = "바로 답하기 어려워서 기획 세션에 넘겼어요."
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Decision:
    action: str
    reply: str
    lead_id: str | None = None
    title: str | None = None


@dataclass
class Attempt:
    text: str | None
    error: str | None
    elapsed_ms: int
    # 상주 프로세스(desk_pool)로 받았는지. 경로별 지연을 운영 이벤트에서 나눠 보려고 남긴다.
    warm: bool = False


async def handle(db: AsyncSession, run: Run, sess: ChatSession) -> None:
    """run(접수 세션의 사용자 메시지)을 처리하고 결과를 run 에 적는다. 끝내기(_finish)는 부른 쪽이 한다."""
    s = get_settings()
    order = {e: i for i, e in enumerate(s.desk_engine_order)}
    profiles, _blocked = await profiles_mod.availability(db, "auto", sess.pool)
    pool = sorted((p for p in profiles if p.engine in order), key=lambda p: order[p.engine])
    leads = await _leads(db, sess)
    prompt = await _prompt(db, run, sess, leads)
    system = instructions_mod.read("desk")

    decision: Decision | None = None
    for prof in pool[:MAX_ATTEMPTS]:
        run.engine, run.profile = prof.engine, prof.name
        run.model = (s.desk_models.get(prof.engine) or {}).get("model")
        res = await _ask(prof, system, prompt)
        decision = _parse(res.text) if res.text else None
        if decision is not None:
            events.append(db, run, "desk.route", {"action": decision.action, "account": prof.name,
                                                   "engine": prof.engine, "ms": res.elapsed_ms, "warm": res.warm})
            break
        reason = res.error or "출력 형식 오류"
        limited = profiles_mod.is_usage_limit(res.error)
        if limited:
            profiles_mod.cool_down(prof.id, profiles_mod.reset_seconds_from(res.error))
        events.append(db, run, "desk.retry", {"account": prof.name, "engine": prof.engine, "ms": res.elapsed_ms,
                                               "warm": res.warm,
                                               "reason": "usage_limit" if limited else reason[:300]})
        await db.commit()
        events.notify(run.id)

    if decision is None:
        decision = Decision("delegate", FALLBACK_REPLY)
        events.append(db, run, "desk.route", {"action": "delegate", "account": None, "engine": None, "fallback": True})

    reply = decision.reply
    if decision.action == "delegate":
        lead = await _delegate(db, run, sess, leads, decision)
        reply = f"{reply}\n\n→ 기획 세션 「{lead.title}」"
        events.append(db, run, "desk.delegated", {"lead_id": str(lead.id), "title": lead.title})
    run.result_text = reply
    events.append(db, run, "message", {"text": reply})


# ---- 작업판·대화

async def _leads(db: AsyncSession, sess: ChatSession) -> list[ChatSession]:
    return list(await db.scalars(
        select(ChatSession).where(ChatSession.parent_id == sess.id, ChatSession.kind == SessionKind.orchestrator,
                                  ChatSession.archived_at.is_(None))
        .order_by(ChatSession.updated_at.desc()).limit(BOARD_LEADS)))


async def _lead_line(db: AsyncSession, lead: ChatSession) -> str:
    runs = list(await db.scalars(select(Run).where(Run.session_id == lead.id).order_by(Run.created_at.desc()).limit(5)))
    active = next((r for r in runs if r.status not in TERMINAL_RUN_STATUSES), None)
    done = next((r for r in runs if r.status in TERMINAL_RUN_STATUSES), None)
    state = "진행 중" if active else ({"succeeded": "완료", "failed": "실패", "cancelled": "취소",
                                         "interrupted": "중단"}.get(done.status, done.status) if done else "대기")
    workers = list(await db.scalars(select(ChatSession.id).where(ChatSession.parent_id == lead.id)))
    busy = 0
    if workers:
        busy = len(set(await db.scalars(select(Run.session_id).where(
            Run.session_id.in_(workers), Run.status.in_([RunStatus.queued, RunStatus.running])))))
    line = f"- lead_id={lead.id} 「{lead.title}」 상태: {state} · 작업 세션 {len(workers)}개(실행 중 {busy})"
    if done and (done.result_text or done.error):
        line += " · 최근 결과: " + " ".join((done.result_text or done.error or "").split())[:300]
    return line


async def _prompt(db: AsyncSession, run: Run, sess: ChatSession, leads: list[ChatSession]) -> str:
    board = [await _lead_line(db, lead) for lead in leads] or ["(맡긴 일 없음)"]
    history = await _history(db, sess, run, HISTORY_TURNS)
    return "\n".join([
        f"[지금] {time.strftime('%Y-%m-%d %H:%M')}",
        "[작업판]", *board, "",
        "[최근 대화]", *(history or ["(없음)"]), "",
        "[사용자 메시지]", run.prompt,
    ])


async def _history(db: AsyncSession, sess: ChatSession, run: Run, turns: int) -> list[str]:
    rows = list(await db.scalars(
        select(Run).where(Run.session_id == sess.id, Run.id != run.id, Run.status.in_(TERMINAL_RUN_STATUSES))
        .order_by(Run.created_at.desc()).limit(turns)))
    lines: list[str] = []
    for r in reversed(rows):
        if r.origin == RunOrigin.report:
            lines.append(f"기획 보고: {(r.result_text or '')[:HISTORY_CHARS]}")
            continue
        lines.append(f"사용자: {r.prompt[:HISTORY_CHARS]}")
        if r.result_text:
            lines.append(f"따까리: {r.result_text[:HISTORY_CHARS]}")
    return lines


async def _delegate(db: AsyncSession, run: Run, sess: ChatSession, leads: list[ChatSession],
                    decision: Decision) -> ChatSession:
    from ttakkari.api.sessions import queue_run

    lead = next((x for x in leads if str(x.id) == (decision.lead_id or "")), None)
    if lead is None:
        first = run.prompt.strip().splitlines()[0] if run.prompt.strip() else ""
        title = (decision.title or first or "새 일")[:60]
        lead = ChatSession(workspace_id=sess.workspace_id, title=title, engine="auto",
                           kind=SessionKind.orchestrator, parent_id=sess.id, pool=sess.pool)
        db.add(lead)
        await db.flush()
    history = await _history(db, sess, run, LEAD_HISTORY_TURNS)
    body = ["[접수에서 넘긴 사용자 요청]", run.prompt]
    if history:
        body += ["", "[최근 대화 — 참고만]", *history]
    await queue_run(db, lead, "\n".join(body), origin=RunOrigin.desk,
                    context_artifact_ids=[uuid.UUID(str(a)) for a in run.context_artifact_ids or []])
    return lead


def _parse(text: str) -> Decision | None:
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    action, reply = obj.get("action"), obj.get("reply")
    if action not in ("answer", "delegate") or not isinstance(reply, str):
        return None
    reply = reply.strip()
    if action == "answer" and not reply:
        return None
    lead_id = obj.get("lead_id") if isinstance(obj.get("lead_id"), str) else None
    title = obj.get("title").strip()[:60] if isinstance(obj.get("title"), str) and obj.get("title").strip() else None
    return Decision(action, reply or "기획 세션에 넘겼어요.", lead_id, title)


# ---- 엔진 호출

async def _ask(prof: profiles_mod.Profile, system: str, prompt: str) -> Attempt:
    s = get_settings()
    if s.desk_warm_pool:
        from ttakkari.runs import desk_pool

        warm = await desk_pool.ask(prof, system, prompt)
        if warm is not None:
            return warm
    cfg = s.desk_models.get(prof.engine) or {}
    cwd = s.home / "desk"
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    cmd = _codex_cmd(prof, cfg, system, prompt, cwd) if prof.engine == "codex" else _claude_cmd(cfg, system, prompt)
    start = time.monotonic()
    ms = lambda: int((time.monotonic() - start) * 1000)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=_env(prof), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        return Attempt(None, f"{prof.engine} 실행 파일을 찾을 수 없습니다.", ms())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), s.desk_timeout_seconds)
    except TimeoutError:
        _kill(proc)
        return Attempt(None, f"시간 초과({s.desk_timeout_seconds:g}초)", ms())
    except asyncio.CancelledError:
        _kill(proc)
        raise
    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    text, error = (_codex_result if prof.engine == "codex" else _claude_result)(stdout)
    if text is None:
        error = error or stderr.strip()[-500:] or f"exit code {proc.returncode}"
    return Attempt(text, error, ms())


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def _env(prof: profiles_mod.Profile) -> dict[str, str]:
    from ttakkari.runs.manager import STRIP_ENV_KEYS, STRIP_ENV_PREFIXES

    env = {k: v for k, v in os.environ.items() if not k.startswith(STRIP_ENV_PREFIXES) and k not in STRIP_ENV_KEYS}
    return env | prof.env()


def _codex_cmd(prof: profiles_mod.Profile, cfg: dict[str, str], system: str, prompt: str, cwd: Path) -> list[str]:
    s = get_settings()
    cmd = [s.codex_bin, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
           # 전역 hook(work-history 기록 강제 등)이 돌면 추가 턴이 붙어 4배 넘게 느려진다(2026-10-13 실측).
           "--disable", "hooks",
           "-c", f"developer_instructions={json.dumps(system)}"]
    for name in _codex_mcp_servers(prof):
        cmd += ["-c", f"mcp_servers.{name}.enabled=false"]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    if cfg.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{cfg["effort"]}"']
    return [*cmd, "-C", str(cwd), "--", prompt]


def _codex_mcp_servers(prof: profiles_mod.Profile) -> list[str]:
    """사용자 config.toml 의 MCP 서버 이름. 접수는 도구를 안 쓰니 띄우지 않는다(기동 시간)."""
    home = prof.config_dir or Path.home() / ".codex"
    try:
        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcp_servers")
    return [n for n in servers if _SAFE_NAME.match(n)] if isinstance(servers, dict) else []


def _claude_cmd(cfg: dict[str, str], system: str, prompt: str) -> list[str]:
    return [get_settings().claude_bin, "-p", prompt, "--output-format", "json", *_claude_flags(cfg, system)]


def _claude_flags(cfg: dict[str, str], system: str) -> list[str]:
    """접수용 claude 옵션. cold(1회 실행)와 상주(desk_pool)가 같이 쓴다."""
    cmd = ["--system-prompt", system,
           # 사용자 hook 과 전역 CLAUDE.md·스킬을 빼고, 도구·MCP 없이 답만 받는다.
           "--settings", json.dumps({"disableAllHooks": True}), "--setting-sources", "local",
           "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    if cfg.get("effort"):
        cmd += ["--effort", cfg["effort"]]
    return cmd


def _codex_result(stdout: str) -> tuple[str | None, str | None]:
    error = None
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        t = obj.get("type") if isinstance(obj, dict) else None
        if t == "item.completed" and (obj.get("item") or {}).get("type") == "agent_message":
            return (obj["item"].get("text") or "").strip() or None, None
        if t == "turn.failed":
            error = (obj.get("error") or {}).get("message") or "turn failed"
        elif t == "error":
            error = obj.get("message") or "error"
    return None, error


def _claude_result(stdout: str) -> tuple[str | None, str | None]:
    try:
        obj = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except ValueError:
        return None, stdout.strip()[-500:] or None
    if not isinstance(obj, dict):
        return None, None
    if obj.get("is_error") or obj.get("subtype") not in (None, "success"):
        return None, str(obj.get("result") or obj.get("subtype") or "error")
    return (obj.get("result") or "").strip() or None, None
