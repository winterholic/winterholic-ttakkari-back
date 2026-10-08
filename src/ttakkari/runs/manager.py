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
from ttakkari.db import sessionmaker
from ttakkari.models import Artifact, ChatSession, Run, RunStatus, Workspace, utcnow
from ttakkari.push.service import notify_run_finished
from ttakkari.runs import events
from ttakkari.schemas import ArtifactOut

log = logging.getLogger(__name__)

ADAPTERS: dict[str, AgentAdapter] = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}
MAX_TOUCHED_ARTIFACTS = 50
MAX_OUTBOX_FILES = 200
STDERR_TAIL = 4000
# 부모 프로세스가 Claude Code·Orca 안에서 떴을 때 자식 에이전트가 중첩 세션으로 오인하지 않게 지운다.
STRIP_ENV_PREFIXES = ("TTAKKARI_", "ORCA_", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID")
STRIP_ENV_KEYS = {"DATABASE_URL", "PGPASSWORD"}

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

    async def shutdown(self) -> None:
        for h in list(self._handles.values()):
            h.cancel_requested = True
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
                if run is not None and run.status not in {s.value for s in (RunStatus.succeeded, RunStatus.failed)}:
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
        write_roots = [root, outbox, *[Path(p) for p in ws.extra_writable_roots], *_tmp_dirs()]

        prompt = await _compose_prompt(db, run)
        adapter = ADAPTERS[run.engine]
        spec = LaunchSpec(
            cwd=root, prompt=prompt, engine_session_id=sess.engine_session_id, model=run.model, effort=run.effort,
            outbox=outbox, write_roots=write_roots, system_note=SYSTEM_NOTE.format(outbox=outbox),
            codex_sandbox=ws.codex_sandbox,
        )
        env = _child_env(run.id, write_roots, s.home, s.guard_log)
        cmd = adapter.build_command(spec, env)
        await self._commit(db, run)

        state = ParseState()
        h = self._handles.get(run.id)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(root), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=32 * 1024 * 1024,
            )
        except FileNotFoundError:
            await self._finish(db, run, RunStatus.failed, error=f"{run.engine} 실행 파일을 찾을 수 없습니다.")
            return
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
        run.exit_code = rc

        if state.engine_session_id and state.engine_session_id != sess.engine_session_id:
            sess.engine_session_id = state.engine_session_id
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

        if h and h.cancel_requested:
            await self._finish(db, run, RunStatus.cancelled)
        elif timed_out:
            await self._finish(db, run, RunStatus.failed, error=f"시간 초과({s.run_timeout_seconds}초)")
        elif rc == 0 and not state.is_error:
            await self._finish(db, run, RunStatus.succeeded)
        else:
            tail = "".join(h.stderr_tail)[-STDERR_TAIL:] if h else ""
            await self._finish(db, run, RunStatus.failed, error=state.error or tail.strip() or f"exit code {rc}")

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
                    db, resolved, source=source, workspace_id=ws.id, session_id=run.session_id, run_id=run.id,
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
        await self._commit(db, run)
        sess = await db.get(ChatSession, run.session_id)
        try:
            await notify_run_finished(run, sess.title if sess else None)
        except Exception:
            log.exception("push notification failed for run %s", run.id)

    async def _commit(self, db: AsyncSession, run: Run) -> None:
        await db.commit()
        events.notify(run.id)


manager = RunManager()


async def recover_interrupted() -> int:
    """서버가 죽었다 살아나면 실행 중이던 Run 은 프로세스를 잃은 것이다. 상태를 정리해 사용자가 이어서 지시할 수 있게 한다."""
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(Run).where(Run.status.in_([RunStatus.queued, RunStatus.running]))))
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
    return "[사용자가 지금 보고 있는 파일]\n" + "\n".join(lines) + "\n\n" + run.prompt


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
