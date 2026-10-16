"""접수(desk): 사용자 메시지마다 가벼운 모델이 바로 답하거나 기획 세션에 넘긴다(ADR 0013).

- 전역 하네스를 받지 않는다. Codex 는 `--disable hooks`·사용자 MCP 끄기·빈 작업 폴더, Claude 는 hook 끄기·`--setting-sources local`·
  도구 없음으로 띄운다. 지침은 비서 관리 > 지침 > 접수만 쓴다.
- 도구 없이 한 번에 고르게 한다. 작업판(기획 세션 상태)과 최근 대화는 백엔드가 미리 넣는다. 작은 모델은 여러 번 오가는
  도구 호출에 약하다(2026-10-13 실측).
- 계정은 엔진 순서(desk_engine_order, 기본 luna → haiku) 안에서 사용 상한까지 여유가 큰 순서로. 한도·시간 초과·출력 오류면 다음 계정으로,
  다 실패하면 기획으로 넘긴다. 접수 때문에 사용자 메시지가 멈추는 일은 없게 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import time
import tomllib
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.experiments.qwen35_observer import observe_if_enabled, record_drop
from ttakkari.experiments.qwen35_shadow import CandidateSnapshot
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    SessionKind,
    utcnow,
)
from ttakkari.runs import desk_qwen, events
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
    observe_shadow_route(s, run, sess, leads, pool, prompt, system)

    decision: Decision | None = await _try_qwen(db, run, sess, leads)
    for _ in range(0 if decision is not None else min(MAX_ATTEMPTS, len(pool))):
        # 접수도 같은 엔진의 계정끼리 사용량을 비교한다. 다른 엔진의 미측정값은 이 비교를 끄지 않는다.
        candidates = [p for p in pool if p.engine == pool[0].engine]
        prof = await profiles_mod.choose_async(candidates, run.id, balance_usage=True)
        pool.remove(prof)
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
        from ttakkari.generation import fence_pending, wake
        await fence_pending(db)
        if get_settings().runtime_generation:
            await wake(db, run.id)
        await db.commit()
        events.notify(run.id)

    if decision is None:
        decision = Decision("delegate", FALLBACK_REPLY)
        events.append(db, run, "desk.route", {"action": "delegate", "account": None, "engine": None, "fallback": True})

    reply = decision.reply
    if decision.action == "delegate":
        lead, dedup = await _delegate(db, run, sess, decision)
        reply = f"{reply}\n\n→ 기획 세션 「{lead.title}」"
        payload: dict[str, object] = {"lead_id": str(lead.id), "title": lead.title}
        if dedup:
            payload |= {"dedup": True, "requested_title": _new_title(run, decision)}
        events.append(db, run, "desk.delegated", payload)
    run.result_text = reply
    events.append(db, run, "message", {"text": reply})


def observe_shadow_route(settings, run: Run, sess: ChatSession, leads: list[ChatSession], pool: list,
                         prompt: str, system: str) -> None:
    """Capture only when opted in; isolate every observer failure from live routing."""
    if not settings.qwen35_shadow_enabled:
        return
    try:
        model_ids = tuple(dict.fromkeys(
            model for profile in pool
            if (model := (settings.desk_models.get(profile.engine) or {}).get("model"))
        ))
        policy_digest = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
        observe_if_enabled(
            enabled=True,
            environment=settings.env,
            request_text=run.prompt,
            context_text=prompt,
            policy_version=f"adr-0013:v1;desk-instructions-sha256:{policy_digest}",
            scope=sess.pool,
            candidates=CandidateSnapshot(
                lead_ids=tuple(str(lead.id) for lead in leads),
                model_ids=model_ids,
                tool_ids=(),  # desk has no MCP/tool execution surface
                permission_ids=(),
            ),
            sample_key=str(run.id),
            sample_rate=settings.qwen35_shadow_sample_rate,
        )
    except Exception:  # noqa: BLE001 -- observer failures never affect the live route.
        record_drop("observer_error")
        log.warning("Qwen shadow observer failed; live desk route continues", exc_info=False)


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


async def _context(db: AsyncSession, run: Run, sess: ChatSession, leads: list[ChatSession]) -> tuple[list[str], list[str]]:
    """작업판 줄과 최근 대화 줄. 엔진 프롬프트(_prompt)와 로컬 Qwen(desk_qwen)이 같은 재료를 쓴다."""
    board = [await _lead_line(db, lead) for lead in leads]
    return board, await _history(db, sess, run, HISTORY_TURNS)


async def _prompt(db: AsyncSession, run: Run, sess: ChatSession, leads: list[ChatSession]) -> str:
    board, history = await _context(db, run, sess, leads)
    return "\n".join([
        f"[지금] {time.strftime('%Y-%m-%d %H:%M')}",
        "[작업판]", *(board or ["(맡긴 일 없음)"]), "",
        "[최근 대화]", *(history or ["(없음)"]), "",
        "[사용자 메시지]", run.prompt,
    ])


def _qwen_context(board: list[str], history: list[str], message: str, max_chars: int) -> tuple[list[str], list[str]]:
    # CPU 접수의 짧은 제한 시간 안에 분류·검증을 마쳐야 한다. 문장 중간을 잘라 근거를 바꾸지 않는다.
    budget = min(max_chars, 2000) - 64
    def size(b: list[str], h: list[str]) -> int:
        return len("\n".join(b)) + len("\n".join(h)) + len(message)
    if size(board, history) <= budget:
        return board, history
    compact = []
    for line in board:
        core = line.split(" · 최근 결과: ", 1)[0]
        compact.append(core if "상태:" in core else line)
    if size(compact, []) > budget:
        return board, history  # 작업판을 빈 것으로 바꾸거나 사용자 메시지를 잘라서 답하지 않는다.
    recent: list[str] = []
    for line in reversed(history):
        if size(compact, [line, *recent]) > budget:
            break
        recent.insert(0, line)
    for index, line in enumerate(board):
        candidate = compact.copy()
        candidate[index] = line
        if size(candidate, recent) <= budget:
            compact = candidate
    return compact, recent


async def _try_qwen(db: AsyncSession, run: Run, sess: ChatSession, leads: list[ChatSession]) -> Decision | None:
    """1순위 로컬 Qwen. 못 쓰면 None 을 돌려주고 호출한 쪽이 Luna/Haiku 경로로 넘어간다."""
    s = get_settings()
    if not desk_qwen.available():
        if s.desk_qwen_enabled:
            reason = "not_ready" if desk_qwen.endpoint() is None else "cooldown"
            events.append(db, run, "desk.skip", {"account": "qwen-local", "engine": "qwen", "ms": 0,
                                                   "warm": False, "reason": reason})
        return None
    board, history = await _context(db, run, sess, leads)
    original_chars = len("\n".join(board)) + len("\n".join(history)) + len(run.prompt)
    board, history = _qwen_context(board, history, run.prompt, s.desk_qwen_max_prompt_chars)
    context_chars = len("\n".join(board)) + len("\n".join(history)) + len(run.prompt)
    base_url = desk_qwen.endpoint()
    if base_url is None:
        return None  # 서버가 준비 전이거나 실패 중: 기다리지 않고 Luna/Haiku 로 간다
    started = time.monotonic()
    try:
        with desk_qwen.measure_operational_run(run.id, base_url):
            q = await desk_qwen.decide(board, history, run.prompt, [str(lead.id) for lead in leads])
    except desk_qwen.QwenUnavailable as exc:
        events.append(db, run, "desk.retry" if exc.cooldown else "desk.skip", {"account": "qwen-local", "engine": "qwen",
                                               "ms": int((time.monotonic() - started) * 1000), "warm": False,
                                               "reason": exc.reason[:300], "context_chars": context_chars})
        return None
    run.engine, run.profile, run.model = "qwen", None, s.desk_qwen_model
    events.append(db, run, "desk.route", {"action": q.action, "account": "qwen-local", "engine": "qwen", "ms": q.ms, "warm": True,
                                           "qwen": {"kind": q.kind, "abstained": q.abstained, "grounded": q.grounded, "verified": q.verified,
                                   "calls": q.calls, "prompt_version": q.prompt_version,
                                   "context_chars": context_chars, "context_compacted": context_chars < original_chars}})
    return Decision(q.action, q.reply, q.lead_id)


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


async def _delegate(db: AsyncSession, run: Run, sess: ChatSession, decision: Decision) -> tuple[ChatSession, bool]:
    """넘길 기획 세션과, 같은 일로 보고 방금 만든 기획에 이어 넣었는지(dedup)를 돌려준다."""
    from ttakkari.api.sessions import queue_run

    # 접수 행을 잠그고 기획 목록을 다시 읽는다. 작업판은 엔진을 부르기 전 상태라, 동시에 넘긴 다른 접수 Run 이 그사이 만든
    # 기획을 못 본다. 자동 보관(runs/lead_archive)도 같은 행을 잠가 순서가 맞는다. 잠금은 queue_run 의 commit 에서 풀린다.
    await db.scalar(select(ChatSession.id).where(ChatSession.id == sess.id).with_for_update())
    lead = await _open_lead(db, sess, decision.lead_id)
    dedup = False
    if lead is None:
        title = _new_title(run, decision)
        lead = await _same_recent_lead(db, sess, title)
        dedup = lead is not None
        if lead is None:
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
    return lead, dedup


def _new_title(run: Run, decision: Decision) -> str:
    first = run.prompt.strip().splitlines()[0] if run.prompt.strip() else ""
    return (decision.title or first or "새 일")[:60]


async def _open_lead(db: AsyncSession, sess: ChatSession, lead_id: str | None) -> ChatSession | None:
    """이 접수의 보관 안 된 기획만. 보관된 기획을 고르면 새로 만든다(ADR 0013)."""
    try:
        lid = uuid.UUID(lead_id or "")
    except ValueError:
        return None
    return await db.scalar(select(ChatSession).where(
        ChatSession.id == lid, ChatSession.parent_id == sess.id, ChatSession.kind == SessionKind.orchestrator,
        ChatSession.archived_at.is_(None)))


def _title_key(title: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", title).casefold() if ch.isalnum())


async def _same_recent_lead(db: AsyncSession, sess: ChatSession, title: str) -> ChatSession | None:
    """lead_dedup_seconds 안에 이 접수에서 만든 같은 제목(공백·대소문자·구두점 무시) 기획. 동시에 넘긴 같은 일을 하나로 모은다.

    제목은 접수 모델이 메시지마다 따로 짓는다. 같은 일이어도 제목이 다르면 못 잡는다.
    """
    window, key = get_settings().lead_dedup_seconds, _title_key(title)
    if window <= 0 or not key:
        return None
    rows = await db.scalars(select(ChatSession).where(
        ChatSession.parent_id == sess.id, ChatSession.kind == SessionKind.orchestrator,
        ChatSession.archived_at.is_(None), ChatSession.created_at >= utcnow() - timedelta(seconds=window))
        .order_by(ChatSession.created_at.desc()))
    return next((x for x in rows if _title_key(x.title) == key), None)


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

async def _ask(prof: profiles_mod.Profile, system: str, prompt: str, *,
               cfg_override: dict[str, str] | None = None, timeout_seconds: float | None = None,
               use_warm: bool = True, output_schema: Path | None = None) -> Attempt:
    s = get_settings()
    if use_warm and s.desk_warm_pool and cfg_override is None:
        from ttakkari.runs import desk_pool

        warm = await desk_pool.ask(prof, system, prompt)
        if warm is not None:
            return warm
    cfg = cfg_override if cfg_override is not None else (s.desk_models.get(prof.engine) or {})
    timeout = timeout_seconds or s.desk_timeout_seconds
    cwd = s.home / "desk"
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    cmd = (_codex_cmd(prof, cfg, system, prompt, cwd, output_schema=output_schema)
           if prof.engine == "codex" else _claude_cmd(cfg, system, prompt))
    start = time.monotonic()
    ms = lambda: int((time.monotonic() - start) * 1000)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=_env(prof), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        return Attempt(None, f"{prof.engine} 실행 파일을 찾을 수 없습니다.", ms())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        _kill(proc)
        return Attempt(None, f"시간 초과({timeout:g}초)", ms())
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


def _codex_cmd(prof: profiles_mod.Profile, cfg: dict[str, str], system: str, prompt: str, cwd: Path, *,
               output_schema: Path | None = None) -> list[str]:
    s = get_settings()
    cmd = [s.codex_bin, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
           # 전역 hook(work-history 기록 강제 등)이 돌면 추가 턴이 붙어 4배 넘게 느려진다(2026-10-13 실측).
           "--disable", "hooks", "--disable", "shell_tool", "--disable", "unified_exec",
           "--disable", "web_search_request", "--disable", "standalone_web_search",
           "--disable", "request_permissions_tool", "--disable", "sleep_tool", "--disable", "view_image",
           "--disable", "collab", "--disable", "multi_agent_v2", "--disable", "apps",
           "--disable", "enable_mcp_apps", "--disable", "tool_suggest", "--disable", "plugins",
           "--disable", "code_mode", "--disable", "deferred_executor", "--disable", "token_budget",
           "--disable", "current_time_reminder", "--disable", "apply_patch_freeform",
           "-c", 'approval_policy="never"', "-c", "tools.update_plan.enabled=false",
           "-c", f"developer_instructions={json.dumps(system)}"]
    for name in _codex_mcp_servers(prof):
        cmd += ["-c", f"mcp_servers.{name}.enabled=false"]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    if cfg.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{cfg["effort"]}"']
    if output_schema is not None:
        cmd += ["--output-schema", str(output_schema)]
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
