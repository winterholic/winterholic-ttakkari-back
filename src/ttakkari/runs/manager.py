"""Run 수명주기 관리. 프로세스 실행·이벤트 기록·취소·재시작 복구를 한 곳에서만 한다."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.agents.base import AgentAdapter, LaunchSpec, ParseState
from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.agents.codex import CodexAdapter
from ttakkari.artifacts import preview
from ttakkari.artifacts.kinds import needs_server_preview
from ttakkari.artifacts.service import register_file
from ttakkari.audit import record
from ttakkari.broker.policy import BrokerError, ResolvedPath, is_within
from ttakkari.config import get_settings
from ttakkari.context.store import pinned_brief, recall
from ttakkari.db import sessionmaker
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    Artifact,
    ArtifactArea,
    ChatSession,
    Run,
    RunEvent,
    RunOrigin,
    RunStatus,
    SessionKind,
    Workspace,
    utcnow,
)
from ttakkari.orchestrator.runtime import (
    AGENT_NOTE,
    MCP_SERVER_NAME,
    ORCHESTRATOR_NOTE,
    mcp_server_spec,
)
from ttakkari.push.service import notify_run_finished
from ttakkari.runs import events
from ttakkari.runs import profiles as profiles_mod
from ttakkari.runs import usage as usage_mod
from ttakkari.schemas import ArtifactOut

log = logging.getLogger(__name__)

ADAPTERS: dict[str, AgentAdapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}
MAX_TOUCHED_ARTIFACTS = 50
MAX_OUTBOX_FILES = 200
STDERR_TAIL = 4000
HANDOFF_TURNS = 8
HANDOFF_CHARS = 1500
REPORT_CHARS = 3000
# 부모 프로세스가 Claude Code·Orca 안에서 떴을 때 자식 에이전트가 중첩 세션으로 오인하지 않게 지운다.
STRIP_ENV_PREFIXES = ("TTAKKARI_", "ORCA_", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID")
# 계정 폴더는 계정 설정으로만 정한다. 백엔드를 띄운 셸(예: Orca)의 값이 기본 계정에 새지 않게 지운다(ADR 0006).
STRIP_ENV_KEYS = {"DATABASE_URL", "PGPASSWORD", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}

SYSTEM_NOTE = """\
You are running unattended via Winterholic Ttakkari on the user's Mac Studio. The user is remote, usually on a phone, \
and sees your messages and the files you produce in a web app.
- Save every deliverable file meant for the user (documents, reports, exports, images, archives) into this outbox \
directory: {outbox}. Files there are registered automatically and the user can preview and download them.
- Editing code inside the workspace is fine as usual; changed files are tracked separately.
- There is no TTY and nobody can answer interactive prompts. Never wait for confirmation.
- A safety guard blocks a small set of irreversible operations (writes outside the workspace, force pushes, \
reading secrets, etc.). If something is blocked, explain briefly and continue with a safe alternative.
- Reply in the user's language."""


@dataclass
class _Handle:
    task: asyncio.Task[None]
    proc: asyncio.subprocess.Process | None = None
    cancel_requested: bool = False
    stderr_tail: list[str] = field(default_factory=list)


class RunManager:
    def __init__(self) -> None:
        self._handles: dict[uuid.UUID, _Handle] = {}
        self._sem: asyncio.Semaphore | None = None
        self._shutting_down = False

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(get_settings().max_concurrent_runs)
        return self._sem

    def is_active(self, run_id: uuid.UUID) -> bool:
        return run_id in self._handles

    def start(self, run_id: uuid.UUID) -> None:
        task = asyncio.create_task(self._execute(run_id), name=f"run-{run_id}")
        self._handles[run_id] = _Handle(task=task)
        task.add_done_callback(lambda _t: self._handles.pop(run_id, None))

    async def cancel(self, run_id: uuid.UUID) -> bool:
        h = self._handles.get(run_id)
        if h is None:
            return False
        h.cancel_requested = True
        if h.proc is None:
            # 아직 동시 실행 슬롯을 기다리는 중이면 태스크 자체를 취소한다.
            h.task.cancel()
            return True
        await self._terminate(h.proc)
        return True

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), get_settings().cancel_grace_seconds)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

    async def dispatch(self, session_id: uuid.UUID) -> uuid.UUID | None:
        """세션 대기열의 다음 Run 을 시작한다. 엔진 대화는 순서대로만 이어갈 수 있어 세션당 하나씩만 돌린다.

        세션 행을 FOR UPDATE 로 잠가, 지시 접수와 Run 종료가 동시에 들어와도 두 개가 함께 시작되지 않게 한다.
        """
        if self._shutting_down:
            return None
        async with sessionmaker()() as db:
            await db.execute(select(ChatSession.id).where(ChatSession.id == session_id).with_for_update())
            rows = list(await db.scalars(
                select(Run).where(Run.session_id == session_id, Run.status.in_([RunStatus.queued, RunStatus.running]))
                .order_by(Run.created_at)))
            if any(r.status == RunStatus.running or r.id in self._handles for r in rows):
                await db.rollback()
                return None
            nxt = rows[0] if rows else None
            if nxt is not None:
                self.start(nxt.id)
            await db.commit()
            return nxt.id if nxt else None

    async def cancel_queued(self, db: AsyncSession, run: Run) -> None:
        """아직 시작하지 않은(프로세스 없는) 대기 Run 을 취소한다."""
        await self._finish(db, run, RunStatus.cancelled)

    async def shutdown(self) -> None:
        # 재시작으로 끊긴 Run 은 사용자 취소와 구분해 interrupted 로 남긴다.
        self._shutting_down = True
        for h in list(self._handles.values()):
            if h.proc is not None:
                await self._terminate(h.proc)
            h.task.cancel()
        for h in list(self._handles.values()):
            with contextlib.suppress(BaseException):
                await h.task

    # ---- 실행

    async def _execute(self, run_id: uuid.UUID) -> None:
        sm = sessionmaker()
        try:
            async with self.sem, sm() as db:
                await self._run(db, run_id)
        except asyncio.CancelledError:
            async with sm() as db:
                run = await db.get(Run, run_id)
                # 동시 실행 슬롯을 기다리다 서버가 내려간 Run 은 시작도 안 했다. 대기열에 남겨 재시작 뒤 이어서 돌린다.
                not_started = run is not None and run.status == RunStatus.queued and self._shutting_down
                if run is not None and not not_started and run.status not in {s.value for s in (RunStatus.succeeded, RunStatus.failed)}:
                    h = self._handles.get(run_id)
                    status = RunStatus.cancelled if (h and h.cancel_requested) else RunStatus.interrupted
                    await self._finish(db, run, status, error=None if status == RunStatus.cancelled else "서버 종료로 중단")
            raise
        except Exception as e:
            log.exception("run %s crashed", run_id)
            async with sm() as db:
                run = await db.get(Run, run_id)
                if run is not None:
                    await self._finish(db, run, RunStatus.failed, error=f"내부 오류: {e!r}")
        finally:
            self._handles.pop(run_id, None)
            if not self._shutting_down:
                async with sm() as db:
                    sid = await db.scalar(select(Run.session_id).where(Run.id == run_id))
                if sid is not None:
                    _spawn(self.dispatch(sid))

    async def _run(self, db: AsyncSession, run_id: uuid.UUID) -> None:
        s = get_settings()
        run = await db.get(Run, run_id)
        if run is None:
            return
        sess = await db.get(ChatSession, run.session_id)
        ws = await db.get(Workspace, run.workspace_id)
        assert sess is not None and ws is not None
        root = Path(ws.root_path)

        run.status = RunStatus.running
        run.started_at = utcnow()
        events.append(db, run, "run.status", {"status": RunStatus.running})
        await self._commit(db, run)

        untracked_before: set[str] = set()
        if ws.is_git:
            untracked_before = await _git_untracked(root)
            ref = await _git_checkpoint(root, run.id)
            if ref:
                run.checkpoint_ref = ref
                events.append(db, run, "checkpoint", {"ref": ref})

        outbox = s.outbox_root / str(run.id)
        outbox.mkdir(parents=True, exist_ok=True)
        run.outbox_path = str(outbox)
        # 업로드 폴더는 guard 보호 영역(~/.ttakkari) 안이라 에이전트가 첨부를 읽으려면 허용 목록에 있어야 한다.
        write_roots = [root, outbox, s.uploads_root, s.inbox_root, *[Path(p) for p in ws.extra_writable_roots], *_tmp_dirs()]

        base_prompt = await _compose_prompt(db, run)
        is_orch = sess.kind == SessionKind.orchestrator
        system_note = await _system_note(db, outbox, orchestrator=is_orch, scope="work" if sess.pool == "work" else "all")
        mcp_servers = {MCP_SERVER_NAME: mcp_server_spec(sess.id, "orchestrator" if is_orch else "agent")}
        profiles = await profiles_mod.candidates(db, sess.engine, sess.pool)
        if not profiles:
            configured = [a for a in await profiles_mod.effective_order(db, sess.pool)
                          if a.enabled and (sess.engine == "auto" or a.engine == sess.engine)]
            if configured:
                where = "회사" if sess.pool == "work" else "개인"
                await self._finish(db, run, RunStatus.failed,
                                   error=f"켜진 {where} 계정이 모두 사용량 한도 초기화를 기다리고 있습니다.")
                return
            what = "엔진" if sess.engine == "auto" else sess.engine
            where = "회사" if sess.pool == "work" else "개인"
            await self._finish(db, run, RunStatus.failed,
                               error=f"켜진 {where} {what} 계정이 없습니다. 설정 > 계정의 {where} 계정에서 켜 주세요.")
            return
        h = self._handles.get(run.id)
        state = ParseState()
        rc: int | None = None
        timed_out = False
        attempt_handoff = ""
        for i, prof in enumerate(profiles):
            resume_id = (sess.engine_sessions or {}).get(prof.key)
            prompt = base_prompt
            if resume_id is None:
                prompt = await _handoff_context(db, run) + prompt
            prompt = attempt_handoff + prompt
            run.engine, run.profile = prof.engine, prof.name
            spec = LaunchSpec(
                cwd=root, prompt=prompt, engine_session_id=resume_id,
                # 모델 이름은 엔진마다 다르다. 그 엔진의 모델일 때만 넘기고, 어느 엔진 것인지 모르면 첫 계정에만 넘긴다.
                model=run.model if (profiles_mod.model_engine(run.model) or profiles[0].engine) == prof.engine else None,
                effort=run.effort,
                outbox=outbox, write_roots=write_roots, system_note=system_note,
                codex_sandbox=ws.codex_sandbox, mcp_servers=mcp_servers,
            )
            env = _child_env(run.id, write_roots, s.home, s.guard_log) | prof.env()
            adapter = ADAPTERS[prof.engine]
            await self._commit(db, run)
            attempt_start_seq = run.last_seq
            state, rc, timed_out, missing = await self._attempt(db, run, adapter, spec, env, root, h)
            if state.rate_limits:
                usage_mod.remember_stream(prof.id, usage_mod.windows_from_stream(state.rate_limits))
            if state.engine_session_id:
                sess.engine_sessions = {**(sess.engine_sessions or {}), prof.key: state.engine_session_id}
                sess.engine_session_id = state.engine_session_id
            failed = missing or rc != 0 or state.is_error
            stopped = timed_out or self._shutting_down or (h is not None and h.cancel_requested)
            tail = "".join(h.stderr_tail)[-STDERR_TAIL:] if h else ""
            limited = failed and profiles_mod.is_usage_limit(state.error, tail, None if rc == 0 else state.result_text)
            if limited and not stopped:
                # 마지막 계정이어도 쉬게 해야 다음 지시가 한도 걸린 계정부터 다시 두드리지 않는다.
                profiles_mod.cool_down(prof.id, profiles_mod.reset_seconds_from(state.error, tail))
            if stopped or not failed or not (missing or limited) or i == len(profiles) - 1:
                if missing and i == len(profiles) - 1:
                    await self._finish(db, run, RunStatus.failed, error=f"{prof.engine} 실행 파일을 찾을 수 없습니다.")
                    return
                break
            nxt = profiles[i + 1]
            events.append(db, run, "engine.fallback", {
                "from": prof.name, "to": nxt.name, "reason": "missing" if missing else "usage_limit",
                "detail": (state.error or tail or "")[:500],
            })
            attempt_handoff = await _attempt_handoff(db, run, prof.name, state, tail, attempt_start_seq)
            if h:
                h.stderr_tail.clear()
        run.exit_code = rc

        sess.updated_at = utcnow()
        run.cost_usd = state.cost_usd
        run.usage = state.usage
        run.result_text = state.result_text or state.last_message

        diff = None
        if ws.is_git and run.checkpoint_ref:
            # 엔진마다 파일 변경 보고 방식이 달라(codex 는 셸로 고치기도 한다) git 기준으로 변경 파일을 다시 모은다.
            diff = await _git_diff_stat(root, run.checkpoint_ref, untracked_before)
            if diff:
                for f in diff["files"] + [{"path": p} for p in diff["new_files"]]:
                    state.touched_files.add(str(root / f["path"]))
        await self._collect_artifacts(db, run, ws, outbox, state)
        if diff:
            events.append(db, run, "run.diff", diff)

        if self._shutting_down and not (h and h.cancel_requested):
            await self._finish(db, run, RunStatus.interrupted, error="서버 종료로 중단")
        elif h and h.cancel_requested:
            await self._finish(db, run, RunStatus.cancelled)
        elif timed_out:
            await self._finish(db, run, RunStatus.failed, error=f"시간 초과({s.run_timeout_seconds}초)")
        elif rc == 0 and not state.is_error:
            await self._finish(db, run, RunStatus.succeeded)
        else:
            tail = "".join(h.stderr_tail)[-STDERR_TAIL:] if h else ""
            await self._finish(db, run, RunStatus.failed, error=state.error or tail.strip() or f"exit code {rc}")

    async def _attempt(self, db: AsyncSession, run: Run, adapter: AgentAdapter, spec: LaunchSpec, env: dict[str, str],
                       root: Path, h: _Handle | None) -> tuple[ParseState, int | None, bool, bool]:
        """엔진 프로세스 한 번. (상태, 종료 코드, 시간 초과, 실행 파일 없음)."""
        s = get_settings()
        state = ParseState()
        cmd = adapter.build_command(spec, env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(root), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=32 * 1024 * 1024,
            )
        except FileNotFoundError:
            return state, None, False, True
        if h:
            h.proc = proc
        run.pid = proc.pid
        await self._commit(db, run)

        stderr_task = asyncio.create_task(_drain_stderr(proc, h))
        timed_out = False
        try:
            await asyncio.wait_for(self._pump(db, run, proc, adapter, state, root), s.run_timeout_seconds)
        except TimeoutError:
            timed_out = True
            await self._terminate(proc)
        rc = await proc.wait()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(stderr_task, 5)
        return state, rc, timed_out, False

    async def _pump(self, db: AsyncSession, run: Run, proc: asyncio.subprocess.Process,
                    adapter: AgentAdapter, state: ParseState, cwd: Path) -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                events.append(db, run, "log", {"stream": "stdout", "text": text[:4000]})
                await self._commit(db, run)
                continue
            if not isinstance(obj, dict):
                continue
            produced = adapter.parse_line(obj, state, cwd)
            if not produced:
                continue
            for n in produced:
                events.append(db, run, n.type, n.payload)
                if n.type == "guard.blocked":
                    record(db, actor="agent", action="agent.guard_blocked", outcome="denied",
                           target_type="run", target_id=run.id, detail={"reason": n.payload.get("reason", "")[:500]})
            await self._commit(db, run)

    async def _collect_artifacts(self, db: AsyncSession, run: Run, ws: Workspace, outbox: Path, state: ParseState) -> None:
        root = Path(ws.root_path).resolve()
        # 오케스트레이터에 맡겨진 작업 세션의 outbox 는 보고용 중간 산출물이라 에이전트 파일로 둔다.
        # 사용자에게 넘길 것은 오케스트레이터가 share_file 로 올리고, 사용자와 직접 대화하는 세션의 outbox 는 그대로 결과물이다.
        sess = await db.get(ChatSession, run.session_id)
        outbox_area = ArtifactArea.agent if sess is not None and sess.kind == SessionKind.worker else ArtifactArea.files
        found: list[tuple[ResolvedPath, str, str | None]] = []
        to_preview: list[uuid.UUID] = []
        n = 0
        for p in sorted(outbox.rglob("*")):
            if n >= MAX_OUTBOX_FILES:
                events.append(db, run, "log", {"stream": "system", "text": f"outbox 파일이 {MAX_OUTBOX_FILES}개를 넘어 나머지는 등록하지 않았습니다."})
                break
            if p.is_symlink() or not p.is_file():
                continue
            found.append((ResolvedPath(root=outbox.resolve(), path=p.resolve()), "agent_output", p.relative_to(outbox).as_posix()))
            n += 1
        for raw in sorted(state.touched_files)[:MAX_TOUCHED_ARTIFACTS]:
            p = Path(raw)
            if p.is_symlink() or not p.is_file():
                continue
            real = p.resolve()
            if not is_within(real, root) or ".git" in real.relative_to(root).parts:
                continue
            found.append((ResolvedPath(root=root, path=real), "agent_modified", None))
        for resolved, source, rel in found:
            try:
                art = await register_file(
                    db, resolved, source=source, area=outbox_area if source == "agent_output" else None, workspace_id=ws.id, session_id=run.session_id, run_id=run.id,
                    rel_path=rel, export_allowed=ws.export_allowed,
                )
            except (BrokerError, OSError) as e:
                events.append(db, run, "log", {"stream": "system", "text": f"Artifact 등록 실패: {resolved.path.name}: {e}"})
                continue
            events.append(db, run, "artifact.created", {"artifact": ArtifactOut.from_model(art).model_dump(mode="json")})
            if needs_server_preview(art.kind):
                to_preview.append(art.id)
        await self._commit(db, run)
        # 변환 워커는 별도 DB 세션으로 읽는다. 커밋 전에 넣으면 행을 못 보고 pending 에 멈춘다.
        for art_id in to_preview:
            preview.enqueue(art_id)

    async def _finish(self, db: AsyncSession, run: Run, status: RunStatus, error: str | None = None) -> None:
        run.status = status
        run.finished_at = utcnow()
        if error:
            run.error = error[:8000]
        events.append(db, run, "run.finished", {
            "status": status, "error": run.error, "result_text": run.result_text,
            "cost_usd": run.cost_usd, "exit_code": run.exit_code,
        })
        record(db, actor="agent", action="run.finished", outcome="ok" if status == RunStatus.succeeded else "error",
               target_type="run", target_id=run.id, detail={"status": status})
        sess = await db.get(ChatSession, run.session_id)
        parent_id = await _enqueue_report(db, sess, run) if sess is not None else None
        await self._commit(db, run)
        if parent_id is not None:
            _spawn(self.dispatch(parent_id))
        # 푸시 서버가 느려도 Run 슬롯을 붙잡지 않게 따로 띄운다.
        _spawn(_notify(run, sess.title if sess else None))

    async def _commit(self, db: AsyncSession, run: Run) -> None:
        await db.commit()
        events.notify(run.id)


_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    # 참조를 잡아 두지 않으면 태스크가 끝나기 전에 GC 될 수 있다.
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _notify(run: Run, title: str | None) -> None:
    try:
        await notify_run_finished(run, title)
    except Exception:
        log.exception("push notification failed for run %s", run.id)


manager = RunManager()


async def recover_interrupted() -> int:
    """서버가 죽었다 살아나면 실행 중이던 Run 은 프로세스를 잃은 것이다. 상태를 정리해 사용자가 이어서 지시할 수 있게 한다."""
    async with sessionmaker()() as db:
        # 시작하지 않은 대기 Run 은 그대로 두고 resume_queued 가 이어서 돌린다.
        rows = list(await db.scalars(select(Run).where(Run.status == RunStatus.running)))
        for run in rows:
            if run.pid and _looks_like_agent(run.pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(run.pid, signal.SIGTERM)
            run.status = RunStatus.interrupted
            run.finished_at = utcnow()
            run.error = "서버 재시작으로 중단됨"
            events.append(db, run, "run.finished", {"status": RunStatus.interrupted, "error": run.error,
                                                    "result_text": None, "cost_usd": None, "exit_code": None})
        await db.commit()
        return len(rows)


async def resume_queued() -> int:
    async with sessionmaker()() as db:
        sids = list(await db.scalars(select(Run.session_id).where(Run.status == RunStatus.queued).distinct()))
    for sid in sids:
        await manager.dispatch(sid)
    return len(sids)


async def expire_artifacts() -> int:
    from ttakkari.artifacts.service import remove_stored

    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(Artifact).where(
            Artifact.deleted_at.is_(None), Artifact.retention_until.is_not(None), Artifact.retention_until < utcnow())))
        for a in rows:
            remove_stored(a)
            a.deleted_at = utcnow()
        if rows:
            record(db, actor="system", action="artifact.expired", detail={"count": len(rows)})
        await db.commit()
        return len(rows)


# ---- helpers

def _looks_like_agent(pid: int) -> bool:
    """재부팅 뒤에는 PID 가 재사용될 수 있다. 엉뚱한 프로세스 그룹을 죽이지 않게 이름을 확인한다."""
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                             check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(name in out for name in ("claude", "codex"))

def _tmp_dirs() -> list[Path]:
    dirs = {Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}
    return sorted(dirs)


def _child_env(run_id: uuid.UUID, write_roots: list[Path], home: Path, guard_log: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(STRIP_ENV_PREFIXES) and k not in STRIP_ENV_KEYS}
    env["TTAKKARI_RUN_ID"] = str(run_id)
    env["TTAKKARI_GUARD_WRITE_ROOTS"] = os.pathsep.join(str(p) for p in write_roots)
    env["TTAKKARI_GUARD_PROTECTED"] = str(home)
    env["TTAKKARI_GUARD_LOG"] = str(guard_log)
    return env


async def _drain_stderr(proc: asyncio.subprocess.Process, h: _Handle | None) -> None:
    assert proc.stderr is not None
    while True:
        line = await proc.stderr.readline()
        if not line:
            return
        if h is not None:
            h.stderr_tail.append(line.decode("utf-8", errors="replace"))
            if len(h.stderr_tail) > 200:
                del h.stderr_tail[:100]


async def _compose_prompt(db: AsyncSession, run: Run) -> str:
    if not run.context_artifact_ids:
        return run.prompt
    lines = []
    for aid in run.context_artifact_ids:
        art = await db.get(Artifact, uuid.UUID(str(aid)))
        if art is None or art.deleted_at is not None:
            continue
        # 관리 영역 복사본은 guard 보호 영역이라 에이전트가 못 읽는다. 원본 경로를 알려준다.
        lines.append(f"- {art.filename}: {art.original_path}")
    if not lines:
        return run.prompt
    return "[사용자가 함께 보낸 파일(보고 있던 결과물·첨부)]\n" + "\n".join(lines) + "\n\n" + run.prompt


async def _system_note(db: AsyncSession, outbox: Path, *, orchestrator: bool, scope: str = "all") -> str:
    """기본 안내 + 도구 안내 + 비서 지침(~/.ttakkari/harness) + 고정 기억. 매 Run 에 들어가므로 짧게 유지한다(ADR 0007)."""
    parts = [SYSTEM_NOTE.format(outbox=outbox), AGENT_NOTE]
    if orchestrator:
        parts.append(ORCHESTRATOR_NOTE)
    instructions = read_instructions()
    if instructions:
        parts.append("[User's standing instructions for this assistant]\n" + instructions)
    brief = await pinned_brief(db, scope=scope)
    if brief:
        parts.append("[Pinned facts about the user — use recall for more]\n" + brief)
    return "\n\n".join(parts)


def read_instructions() -> str:
    f = get_settings().harness_dir / "instructions.md"
    try:
        text = f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text[: get_settings().context_brief_chars]


async def _handoff_context(db: AsyncSession, run: Run) -> str:
    """처음 쓰는 엔진 프로필에 앞선 대화(지시·답)를 넘긴다. 도구 호출 내역은 넘기지 않는다(ADR 0006 한계)."""
    rows = list(await db.scalars(
        select(Run).where(Run.session_id == run.session_id, Run.id != run.id, Run.status.in_(TERMINAL_RUN_STATUSES))
        .order_by(Run.created_at.desc()).limit(HANDOFF_TURNS)))
    if not rows:
        return ""
    lines = ["[이전 대화 — 다른 엔진·계정에서 이어받음. 참고만 하고 아래 새 지시에 답한다]"]
    for r in reversed(rows):
        lines.append(f"사용자: {r.prompt[:HANDOFF_CHARS]}")
        if r.result_text or r.error:
            lines.append(f"에이전트: {(r.result_text or f'(실패) {r.error}')[:HANDOFF_CHARS]}")
    return "\n".join(lines) + "\n\n[새 지시]\n"


async def _attempt_handoff(db: AsyncSession, run: Run, profile: str, state: ParseState,
                           stderr: str, since_seq: int) -> str:
    """Carry observable progress to the next account without assuming the failed turn was atomic."""
    lines = ["[Previous account handoff]",
             f"Account {profile} stopped: {(state.error or stderr or 'engine unavailable')[:500]}",
             "Continue the current request. Inspect existing work and worker sessions before creating or repeating work.",
             "Search recall for relevant memories and indexed work-history; verify the current state first."]
    rows = list(await db.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.seq > since_seq,
                               RunEvent.type.in_(("message", "tool.call", "tool.result", "guard.blocked")))
        .order_by(RunEvent.seq.desc()).limit(12)))
    for event in reversed(rows):
        payload = event.payload or {}
        detail = payload.get("text") or payload.get("output") or payload.get("name") or payload.get("reason")
        if detail:
            lines.append(f"{event.type}: {str(detail)[:500]}")
    try:
        sess = await db.get(ChatSession, run.session_id)
        hits = await recall(db, run.prompt[:500], 3, kinds=("memory", "chunk"),
                            scope="work" if sess and sess.pool == "work" else "all")
    except Exception:
        log.exception("handoff context search failed for run %s", run.id)
        hits = []
    for hit in hits:
        source = hit.meta.get("path") or hit.meta.get("source") or hit.kind
        lines.append(f"Relevant memory/work-history ({source}): {hit.text[:700]}")
    return "\n".join(lines) + "\n\n"


async def _enqueue_report(db: AsyncSession, sess: ChatSession, run: Run) -> uuid.UUID | None:
    """작업 세션 Run 이 끝나면 오케스트레이터에게 결과 보고 Run 을 넣는다. 시작 전 보고가 있으면 거기에 덧붙인다."""
    if sess.kind != SessionKind.worker or sess.parent_id is None:
        return None
    parent = await db.get(ChatSession, sess.parent_id)
    if parent is None or parent.archived_at is not None:
        return None
    label = {RunStatus.succeeded: "완료", RunStatus.failed: "실패", RunStatus.cancelled: "취소",
             RunStatus.interrupted: "중단"}.get(RunStatus(run.status), run.status)
    body = [f"[작업 보고] 작업 세션 '{sess.title}' (worker_id={sess.id}) {label}."]
    if run.result_text:
        body.append("결과:\n" + run.result_text[:REPORT_CHARS])
    if run.error:
        body.append("오류: " + run.error[:1000])
    text = "\n".join(body)
    pending = await db.scalar(
        select(Run).where(Run.session_id == parent.id, Run.status == RunStatus.queued, Run.origin == RunOrigin.report)
        .order_by(Run.created_at.desc()).limit(1))
    if pending is not None and not manager.is_active(pending.id):
        pending.prompt = pending.prompt + "\n\n" + text
        return parent.id
    report = Run(session_id=parent.id, workspace_id=parent.workspace_id, prompt=text, engine=parent.engine,
                 model=parent.model, effort=parent.effort, origin=RunOrigin.report)
    db.add(report)
    await db.flush()
    events.append(db, report, "run.status", {"status": RunStatus.queued, "ahead": 0})
    parent.updated_at = utcnow()
    return parent.id


async def _git(root: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


async def _git_checkpoint(root: Path, run_id: uuid.UUID) -> str | None:
    """작업 트리를 건드리지 않고 현재 상태를 커밋 객체로 남긴다(stash create). 미추적 파일은 포함되지 않는다."""
    rc, sha = await _git(root, "stash", "create", f"ttakkari checkpoint {run_id}")
    if rc != 0:
        return None
    if not sha:
        rc, sha = await _git(root, "rev-parse", "--verify", "HEAD")
        if rc != 0 or not sha:
            return None
    await _git(root, "update-ref", f"refs/ttakkari/runs/{run_id}", sha)
    return sha


async def _git_untracked(root: Path) -> set[str]:
    rc, out = await _git(root, "ls-files", "--others", "--exclude-standard")
    return set(out.splitlines()) if rc == 0 and out else set()


async def _git_diff_stat(root: Path, ref: str, untracked_before: set[str]) -> dict | None:
    rc, out = await _git(root, "diff", "--numstat", ref)
    if rc != 0:
        return None
    files = []
    for line in out.splitlines()[:500]:
        parts = line.split("\t")
        if len(parts) == 3:
            add, rem, path = parts
            files.append({"path": path, "added": int(add) if add.isdigit() else None,
                          "removed": int(rem) if rem.isdigit() else None})
    new_files = sorted(await _git_untracked(root) - untracked_before)[:200]
    if not files and not new_files:
        return None
    return {"files": files, "new_files": new_files}
