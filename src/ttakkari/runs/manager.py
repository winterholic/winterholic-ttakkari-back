"""Run 수명주기 관리. 프로세스 실행·이벤트 기록·취소·재시작 복구를 한 곳에서만 한다."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari import artcraft_tasks, generation
from ttakkari.agents.antigravity import AntigravityAdapter
from ttakkari.agents.base import AgentAdapter, LaunchSpec, ParseState
from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.agents.codex import CodexAdapter
from ttakkari.agents.muse import MuseAdapter
from ttakkari.agents.qwen_local import MODEL as QWEN_LOCAL_MODEL
from ttakkari.agents.qwen_local import QwenLocalAdapter
from ttakkari.artifacts import preview
from ttakkari.artifacts.kinds import needs_server_preview
from ttakkari.artifacts.service import register_file
from ttakkari.audit import record
from ttakkari.broker.policy import BrokerError, ResolvedPath, is_within
from ttakkari.config import get_settings
from ttakkari.context.store import pinned_brief, recall
from ttakkari.db import sessionmaker
from ttakkari.deployment import Admission
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    Artifact,
    ArtifactArea,
    ChatSession,
    Meeting,
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
    LEAD_NOTE,
    MCP_SERVER_NAME,
    ORCHESTRATOR_NOTE,
    mcp_server_spec,
)
from ttakkari.push.service import PushKind, notify, notify_run_finished
from ttakkari.runs import (
    agy_models,
    capabilities,
    context_usage,
    desk,
    events,
    worker_routing,
)
from ttakkari.runs import instructions as instructions_mod
from ttakkari.runs import profiles as profiles_mod
from ttakkari.runs import usage as usage_mod
from ttakkari.schemas import ArtifactOut

log = logging.getLogger(__name__)

ADAPTERS: dict[str, AgentAdapter] = {
    "claude": ClaudeAdapter(), "codex": CodexAdapter(), "antigravity": AntigravityAdapter(),
    "muse": MuseAdapter(),
}
QWEN_LOCAL_ADAPTER = QwenLocalAdapter(worker_path=get_settings().qwen_local_worker_path,
                                      enabled=get_settings().qwen_local_enabled,
                                      turns=get_settings().qwen_local_turns)
MAX_TOUCHED_ARTIFACTS = 50
MAX_OUTBOX_FILES = 200
MAX_CHECKPOINT_UNTRACKED = 500
STDERR_TAIL = 4000
HANDOFF_TURNS = 8
HANDOFF_CHARS = 1500
WAIT_HANDOFF_CHARS = 6000
MAX_RESTART_RESUME_ATTEMPTS = 3
RESTART_RESUME_BACKOFF_SECONDS = 30
RUN_HEARTBEAT_SECONDS = 15
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
    owner: generation.GenerationRuntime | None = None
    token: uuid.UUID | None = None


class RunManager:
    def __init__(self, *, runtime: generation.GenerationRuntime | None = None) -> None:
        self._runtime = runtime
        self._handles: dict[uuid.UUID, _Handle] = {}
        # 역할별 동시 실행 자리(ADR 0013). 접수·기획·작업이 서로의 자리를 막지 않는다.
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._shutting_down = False
        self._restart_waker: asyncio.Task[None] | None = None
        # 모든 계정이 사용량 한도라 초기화를 기다리는 Run → (재개 시각, 세션). 같은 세션의 다음 Run 도 이 Run 뒤에 선다.
        self._waiting: dict[uuid.UUID, tuple[float, uuid.UUID]] = {}
        self._wakers: dict[uuid.UUID, asyncio.Task[None]] = {}
        # 실행 중에 새 대화로 바꾸라고 한 세션. 실행이 끝날 때 적용해야 그 Run 이 엔진 대화 id 를 다시 덮어쓰지 않는다.
        self._reset_requests: set[uuid.UUID] = set()
        self._qwen_local_slot = asyncio.Semaphore(1)

    def sem(self, lane: str) -> asyncio.Semaphore:
        if lane not in self._sems:
            s = get_settings()
            size = {"desk": s.max_concurrent_desk, "leads": s.max_concurrent_leads}.get(lane, s.max_concurrent_workers)
            self._sems[lane] = asyncio.Semaphore(size)
        return self._sems[lane]

    def is_active(self, run_id: uuid.UUID) -> bool:
        return run_id in self._handles

    @property
    def runtime(self) -> generation.GenerationRuntime | None:
        return self._runtime or generation.current

    async def is_owned(self, run_id: uuid.UUID) -> bool:
        if self.runtime is None:
            return self.is_active(run_id)
        from ttakkari.models import RuntimeLease
        async with sessionmaker()() as db:
            return await db.get(RuntimeLease, run_id) is not None

    async def request_context_reset(self, session_id: uuid.UUID) -> None:
        if self.runtime:
            await self.runtime.command("reset", session_id=session_id)
            return
        self._reset_requests.add(session_id)

    def start(self, run_id: uuid.UUID, *, lane: str = "workers") -> bool:
        if run_id in self._handles or self._shutting_down:
            return False
        lease = Admission(get_settings().home / "deploy").acquire()
        if lease is None:
            if self._restart_waker is None or self._restart_waker.done():
                self._restart_waker = asyncio.create_task(self._resume_after_restart())
            return False
        coro = self._execute_desk(run_id) if lane == "desk" else self._execute(run_id, lane)
        task = asyncio.create_task(coro, name=f"run-{run_id}")
        self._handles[run_id] = _Handle(task=task)
        # 시작 전에 취소된 task도 잠금을 반환해야 재시작 예약이 영원히 막히지 않는다.
        task.add_done_callback(lambda _t: lease.close())
        task.add_done_callback(lambda t: self._handles.pop(run_id, None)
                               if (h := self._handles.get(run_id)) and h.task is t else None)
        return True

    async def _resume_after_restart(self) -> None:
        while not self._shutting_down:
            await asyncio.sleep(1)
            lease = Admission(get_settings().home / "deploy").acquire()
            if lease is not None:
                lease.close()
                self._restart_waker = None
                await resume_queued()
                return

    async def cancel(self, run_id: uuid.UUID) -> bool:
        h = self._handles.get(run_id)
        if h is None:
            if self.runtime:
                if await self.is_owned(run_id):
                    await self.runtime.command("cancel", run_id=run_id)
                    return True
                async with sessionmaker()() as db:
                    run = await db.get(Run, run_id)
                    if run and run.status == RunStatus.queued:
                        await self.cancel_queued(db, run)
                        await db.commit()
                        return True
            return False
        if self.runtime and h.owner is None:
            async with sessionmaker()() as db:
                run = await db.get(Run, run_id)
                if run is not None:
                    await self.cancel_queued(db, run)
                    await db.commit()
            h.task.cancel()
            return True
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
        grace = get_settings().cancel_grace_seconds
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            # On macOS, a just-exited but unreaped group leader can make killpg
            # report EPERM. Reap it before treating that error as a stale group.
            try:
                await asyncio.wait_for(proc.wait(), grace)
            except TimeoutError:
                pass
            if proc.returncode is None:
                raise
            # Re-signal after reaping: descendants may still be alive in the
            # process group even though its original leader has exited.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), grace)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

    async def dispatch(self, session_id: uuid.UUID) -> uuid.UUID | None:
        """세션 대기열의 다음 Run 을 시작한다. 엔진 대화는 순서대로만 이어갈 수 있어 세션당 하나씩만 돌린다.

        세션 행을 FOR UPDATE 로 잠가, 지시 접수와 Run 종료가 동시에 들어와도 두 개가 함께 시작되지 않게 한다.
        """
        if self._shutting_down:
            return None
        if self.runtime and await self.runtime.refresh() != "active":
            return None
        async with sessionmaker()() as db:
            kind = await db.scalar(select(ChatSession.kind).where(ChatSession.id == session_id).with_for_update())
            if kind == SessionKind.desk:
                # 접수는 대화를 이어 쓰지 않는다. 메시지마다 따로, 바로 돈다(바쁜 접수 뒤에 줄 서지 않는다).
                queued = list(await db.scalars(select(Run.id).where(Run.session_id == session_id,
                                                                    Run.status == RunStatus.queued)))
                started = [rid for rid in queued if self.start(rid, lane="desk")]
                await db.commit()
                return started[0] if started else None
            rows = list(await db.scalars(
                select(Run).where(Run.session_id == session_id, Run.status.in_([RunStatus.queued, RunStatus.running]))
                .order_by(Run.created_at)))
            if any(r.status == RunStatus.running or r.id in self._handles for r in rows):
                await db.rollback()
                return None
            nxt = rows[0] if rows else None
            now_ts = time.time()
            now_dt = utcnow()
            if nxt is not None:
                is_armed = self._waiting.get(nxt.id, (0.0, nxt.id))[0] > now_ts
                is_future = bool(nxt.resume_after and nxt.resume_after > now_dt)
                if is_armed or is_future:
                    await db.rollback()
                    return None
                if nxt.resume_after and nxt.resume_after <= now_dt:
                    nxt.resume_after = None
            started = nxt is not None and self.start(nxt.id)
            await db.commit()
            return nxt.id if started else None

    async def cancel_queued(self, db: AsyncSession, run: Run) -> None:
        """아직 시작하지 않은(프로세스 없는) 대기 Run 을 취소한다."""
        if self.runtime:
            from ttakkari.generation import _control
            from ttakkari.models import RuntimeCommand, RuntimeLease
            await _control(db)
            lease = await db.get(RuntimeLease, run.id)
            if lease:
                db.add(RuntimeCommand(key=str(uuid.uuid4()), kind="cancel", run_id=run.id,
                                      generation_id=lease.generation_id))
                await generation.wake(db)
                return
            await db.refresh(run)
            if run.status != RunStatus.queued:
                return
        await self._finish(db, run, RunStatus.cancelled)

    def _arm(self, run_id: uuid.UUID, session_id: uuid.UUID, until: float) -> None:
        """한도 대기 중인 Run 을 `until` 에 다시 돌린다. 맥이 잠들었다 깨도 늦지 않게 짧게 끊어 시계를 본다."""
        self._disarm(run_id)
        self._waiting[run_id] = (until, session_id)
        self._wakers[run_id] = asyncio.create_task(self._wake(run_id, session_id, until), name=f"wake-{run_id}")

    def _disarm(self, run_id: uuid.UUID) -> None:
        self._waiting.pop(run_id, None)
        waker = self._wakers.pop(run_id, None)
        if waker is not None and waker is not asyncio.current_task():
            waker.cancel()

    def is_waiting(self, run_id: uuid.UUID) -> bool:
        return self._waiting.get(run_id, (0.0, run_id))[0] > time.time()

    async def _wake(self, run_id: uuid.UUID, session_id: uuid.UUID, until: float) -> None:
        while (left := until - time.time()) > 0:
            await asyncio.sleep(min(left, 60))
        self._waiting.pop(run_id, None)
        self._wakers.pop(run_id, None)
        await self.dispatch(session_id)

    async def _emergency_qwen(self, sess: ChatSession,
                              task_type: str | None) -> tuple[profiles_mod.Profile | None, str]:
        """모든 개인 클라우드 계정이 한도일 때만 로컬 Qwen으로 이어받는다.

        (프로필, "ready") 또는 (None, 이유). readiness는 그때그때 확인하고
        허위로 통과시키지 않는다. 준비되지 않은 Qwen은 대기 사유에만 남는다.
        """
        from ttakkari.runs import worker_routing
        if not worker_routing.qwen_emergency_eligible(
                pool=sess.pool, task_type=task_type or sess.worker_task_type,
                strict_engine=sess.strict_engine, session_kind=sess.kind):
            return None, "ineligible"
        ready, reason = await asyncio.to_thread(QWEN_LOCAL_ADAPTER.readiness)
        if not ready:
            return None, reason
        return worker_routing.emergency_qwen_profile(sess.pool), "ready"

    async def _wait_for_reset(self, db: AsyncSession, run: Run, sess: ChatSession,
                              blocked: list[tuple[profiles_mod.Profile, float]], handoff: str | None = None,
                              qwen_state: str | None = None) -> None:
        """쓸 수 있는 계정이 없다. Run 을 queued 로 두고 가장 이른 초기화 시각에 같은 Run 을 다시 돌린다.

        계정을 계속 두드리지 않는다. 사용자에게 한 번 알리고(재시작 뒤 다시 대기해도 중복 알림 없음), 한도가 계속 반복되면
        max_limit_waits 번째에 실패로 끝낸다. handoff 는 중간에 멈춘 Run 의 진행·미완료 내용으로, 재개 때 새 계정에 실린다."""
        until = min(u for _, u in blocked)
        run.resume_after = datetime.fromtimestamp(until, UTC)
        if handoff:
            run.resume_handoff = handoff
        prev = await db.scalar(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "run.waiting")
                               .order_by(RunEvent.seq.desc()).limit(1))
        repeat = prev is not None and prev.seq == run.last_seq and abs(
            (datetime.fromisoformat(prev.payload["until"]).timestamp() if prev.payload.get("until") else 0) - until) < 60
        if not repeat:
            waits = await db.scalar(select(func.count()).select_from(RunEvent).where(
                RunEvent.run_id == run.id, RunEvent.type == "run.waiting")) or 0
            if waits >= get_settings().max_limit_waits:
                await self._finish(db, run, RunStatus.failed,
                                   error=f"사용량 한도 때문에 {waits}번 기다렸지만 끝내지 못했습니다. 한도 상태를 확인해 주세요.")
                return
            if run.status == RunStatus.running:
                run.status = RunStatus.queued
                run.pid = None
                events.append(db, run, "run.status", {"status": RunStatus.queued, "waiting": True})
            if handoff is None and prev is not None:
                handoff = str((prev.payload or {}).get("handoff") or "")
            waiting_payload: dict[str, object] = {
                "until": _iso(until),
                "accounts": [{"name": p.name, "engine": p.engine, "resets_at": _iso(u)}
                             for p, u in sorted(blocked, key=lambda b: b[1])],
                "handoff": (handoff or "")[:WAIT_HANDOFF_CHARS],
            }
            if qwen_state is not None:
                waiting_payload["qwen_local"] = {"ready": False, "reason": qwen_state}
            events.append(db, run, "run.waiting", waiting_payload)
            await self._commit(db, run)
            _spawn(_notify_waiting(run, sess.title, until))
        self._arm(run.id, sess.id, until)

    async def release_waiting(self) -> None:
        if self.runtime:
            from sqlalchemy import update
            async with sessionmaker()() as db:
                await db.execute(update(Run).where(Run.status == RunStatus.queued).values(resume_after=None))
                await generation.wake(db)
                await db.commit()
            await _reconsider_quota_waits()
            return
        """한도 대기를 풀고 바로 다시 시도한다(사용자가 계정 한도 상태를 풀거나 계정을 켰을 때). 여전히 못 쓰면 다시 대기한다."""
        pending = list(self._waiting.items())
        for rid, _ in pending:
            self._disarm(rid)
        from sqlalchemy import update
        async with sessionmaker()() as db:
            await db.execute(update(Run).where(Run.status == RunStatus.queued).values(resume_after=None))
            await db.commit()
        await _reconsider_quota_waits()
        for sid in {sid for _, (_, sid) in pending}:
            await self.dispatch(sid)

    async def shutdown(self) -> None:
        # 재시작으로 끊긴 Run 은 사용자 취소와 구분해 interrupted 로 남긴다.
        self._shutting_down = True
        if self._restart_waker is not None:
            self._restart_waker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_waker
            self._restart_waker = None
        for rid in list(self._wakers):
            self._disarm(rid)
        for h in list(self._handles.values()):
            if h.proc is not None:
                await self._terminate(h.proc)
            h.task.cancel()
        for h in list(self._handles.values()):
            with contextlib.suppress(BaseException):
                await h.task

    # ---- 실행

    async def _execute(self, run_id: uuid.UUID, lane: str = "workers") -> None:
        sm = sessionmaker()
        runtime, token = self.runtime, None
        try:
            if lane == "workers":
                async with sm() as db:
                    kind = await db.scalar(select(ChatSession.kind).join(Run, Run.session_id == ChatSession.id)
                                           .where(Run.id == run_id))
                lane = "leads" if kind == SessionKind.orchestrator else "workers"
            if runtime:
                limit = get_settings().max_concurrent_leads if lane == "leads" else get_settings().max_concurrent_workers
                token = await runtime.claim(run_id, lane, limit)
                if token is None:
                    return
                if h := self._handles.get(run_id):
                    h.owner, h.token = runtime, token
            async with self.sem(lane), sm() as db:
                if runtime and token:
                    db.info["runtime_lease"] = (runtime, run_id, token)
                await self._run(db, run_id)
        except generation.OwnershipLost:
            log.error("discarded stale writes for run %s", run_id)
        except asyncio.CancelledError:
            if runtime is not None and token is None:
                raise
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
            if runtime is not None and token is None:
                return  # A failed claim did not authorize any writes to this Run.
            async with sm() as db:
                run = await db.get(Run, run_id)
                if run is not None:
                    await self._finish(db, run, RunStatus.failed, error=f"내부 오류: {e!r}")
        finally:
            if runtime and token:
                await runtime.release(run_id, token)
            self._handles.pop(run_id, None)
            profiles_mod.release(run_id)
            if not self._shutting_down and (runtime is None or token is not None):
                async with sm() as db:
                    sid = await db.scalar(select(Run.session_id).where(Run.id == run_id))
                if sid is not None:
                    _spawn(self.dispatch(sid))
                _spawn(_reconsider_quota_waits())

    async def _execute_desk(self, run_id: uuid.UUID) -> None:
        """접수 Run. 세션 대기열·엔진 대화 없이 한 번 고르고 끝난다. 실패해도 사용자 메시지를 기획으로 넘겨 멈추지 않게 한다."""
        sm = sessionmaker()
        runtime, token = self.runtime, None
        try:
            if runtime:
                token = await runtime.claim(run_id, "desk", get_settings().max_concurrent_desk)
                if token is None:
                    return
                if h := self._handles.get(run_id):
                    h.owner, h.token = runtime, token
            async with self.sem("desk"), sm() as db:
                if runtime and token:
                    db.info["runtime_lease"] = (runtime, run_id, token)
                run = await db.get(Run, run_id)
                if run is None or run.status != RunStatus.queued:
                    return
                sess = await db.get(ChatSession, run.session_id)
                assert sess is not None
                run.status = RunStatus.running
                run.started_at = utcnow()
                events.append(db, run, "run.status", {"status": RunStatus.running})
                await self._commit(db, run)
                await desk.handle(db, run, sess)
                await self._finish(db, run, RunStatus.succeeded)
        except generation.OwnershipLost:
            log.error("discarded stale writes for desk run %s", run_id)
        except asyncio.CancelledError:
            if runtime is not None and token is None:
                raise
            async with sm() as db:
                run = await db.get(Run, run_id)
                # 자리를 기다리다 서버가 내려간 접수는 시작도 안 했다. 대기열에 남겨 재시작 뒤 돌린다.
                not_started = run is not None and run.status == RunStatus.queued and self._shutting_down
                if run is not None and not not_started and run.status not in TERMINAL_RUN_STATUSES:
                    h = self._handles.get(run_id)
                    status = RunStatus.cancelled if (h and h.cancel_requested) else RunStatus.interrupted
                    await self._finish(db, run, status, error=None if status == RunStatus.cancelled else "서버 종료로 중단")
            raise
        except Exception as e:
            log.exception("desk run %s crashed", run_id)
            if runtime is not None and token is None:
                return
            async with sm() as db:
                run = await db.get(Run, run_id)
                if run is not None and run.status not in TERMINAL_RUN_STATUSES:
                    await self._finish(db, run, RunStatus.failed, error=f"내부 오류: {e!r}")
        finally:
            if runtime and token:
                await runtime.release(run_id, token)
            self._handles.pop(run_id, None)
            profiles_mod.release(run_id)

    async def _run(self, db: AsyncSession, run_id: uuid.UUID) -> None:
        s = get_settings()
        run = await db.get(Run, run_id)
        if run is None:
            return
        sess = await db.get(ChatSession, run.session_id)
        ws = await db.get(Workspace, run.workspace_id)
        assert sess is not None and ws is not None
        if self.runtime:
            from ttakkari.generation_service import apply_reset
            await apply_reset(db, sess)
        root = Path(ws.root_path)

        # 쓸 수 있는 계정이 없으면 시작하지 않고 가장 이른 한도 초기화까지 기다린다(대기 Run 은 queued 로 남는다).
        from ttakkari.meetings import routing as meeting_routing
        from ttakkari.runs import worker_routing
        is_meeting = await db.scalar(select(Meeting.id).where(Meeting.analysis_run_id == run.id)) is not None
        is_orch = sess.kind == SessionKind.orchestrator
        task_type, pinned_fallback, requested_model = _routing_policy(sess, run, is_meeting)
        primary_pair = worker_routing.primary_target(sess.engine, requested_model) if pinned_fallback else None
        effective_task_type = (
            task_type
            or sess.worker_task_type
            or (worker_routing.infer_task_type(requested_model) if (pinned_fallback or sess.kind == SessionKind.worker) else None)
        )
        if sess.kind == SessionKind.worker:
            run.usage = {**(run.usage or {}), "routing_request": {
                "engine": sess.engine, "model": requested_model,
                "task_type": sess.worker_task_type or effective_task_type or "general",
                "account_scope": sess.pool,
            }}

        async def available_profiles():
            return await _available_profiles(db, sess, run, is_meeting)

        profiles, blocked = await available_profiles()
        if not profiles:
            if blocked:
                emergency, qwen_state = await self._emergency_qwen(sess, effective_task_type)
                if emergency is None:
                    await self._wait_for_reset(db, run, sess, blocked, qwen_state=qwen_state)
                    return
                profiles = [emergency]
                events.append(db, run, "worker.routing", {
                    "policy": worker_routing.POLICY_VERSION,
                    "task_type": effective_task_type or "general", "trigger": "emergency",
                    "selected": None,
                    "scope": {"account_scope": sess.pool, "runtime_scope": "local-machine",
                              "workspace_id": str(sess.workspace_id)},
                    "quota": None, "readiness": qwen_state,
                    "emergency_from": [{"account": p.name, "engine": p.engine, "resets_at": _iso(u)}
                                       for p, u in sorted(blocked, key=lambda b: b[1])],
                })
                events.append(db, run, "engine.fallback", {
                    "from": "all personal cloud accounts", "from_engine": "cloud",
                    "from_model": None, "to": emergency.name, "to_engine": "qwen-local",
                    "to_model": emergency.model, "reason": "usage_limit_all_cloud_limited",
                    "mode": "handoff",
                    "detail": "개인 클라우드 계정이 모두 한도라 로컬 Qwen으로 이어서 실행",
                })
            if sess.engine == "qwen-local":
                ready, reason = await asyncio.to_thread(QWEN_LOCAL_ADAPTER.readiness)
                events.append(db, run, "worker.routing", {
                    "policy": worker_routing.POLICY_VERSION,
                    "task_type": effective_task_type or "general", "trigger": "initial",
                    "selected": None, "requested_engine": "qwen-local", "requested_model": QWEN_LOCAL_MODEL,
                    "scope": {"account_scope": sess.pool, "runtime_scope": "local-machine",
                              "workspace_id": str(sess.workspace_id)},
                    "quota": None, "readiness": reason, "fallback": False,
                })
                await self._finish(db, run, RunStatus.failed,
                                   error=f"Qwen local blocked: {reason if not ready else 'no local worker profile'}")
                return
            what = "엔진" if sess.engine == "auto" else sess.engine
            where = "회사/개인" if is_meeting else ("회사" if sess.pool == "work" else "개인")
            await self._finish(db, run, RunStatus.failed,
                               error=f"켜진 {where} {what} 계정이 없습니다. 설정 > 계정의 {where} 계정에서 켜 주세요.")
            return

        await context_usage.prepare(db, sess, run)

        # 모든 역할과 고정 엔진 세션도 신선한 사용량을 비교한다(ADR 0006).
        # 모델·엔진 범위는 지키고, 계정 전환 시 저장된 엔진 대화와 _handoff_context로 이어 받는다.
        prefer = profiles_mod.model_engine(run.model)
        remaining = list(profiles)
        choose_account = self.runtime.choose_profile if self.runtime else profiles_mod.choose_async

        async def choose(candidates, run_id, **kwargs):
            if is_meeting:
                candidates = meeting_routing.first_stage(candidates)
            elif task_type:
                all_candidates = candidates
                # Muse 탐색은 단계 선택에 들어간 뒤 DB 에서 예약한다. 져서 빠지면 그 후보 없이 단계를 다시 고른다.
                maybe = frozenset(p.id for p in candidates if profiles_mod.muse_probe_eligible(p))
                candidates = worker_routing.first_stage(candidates, task_type, maybe)
                if maybe:
                    won = await profiles_mod.claim_muse_probe([p for p in candidates if p.id in maybe])
                    if maybe - won:
                        candidates = worker_routing.first_stage(all_candidates, task_type, won)
                    kwargs = {**kwargs, "probe_ids": won}
                counts = await profiles_mod.running_counts_async()
                # Selection excludes this Run's own claim; receipts use the same denominator.
                own = profiles_mod._in_use.get(run_id)
                if own in counts:
                    counts[own] = max(0, counts[own] - 1)
            elif pinned_fallback:
                all_candidates = candidates
                candidates = worker_routing.pinned_first_stage(
                    candidates, sess.engine, requested_model, sess.worker_task_type,
                )
                if kwargs.get("balance_usage") and any(p.engine == "muse" for p in candidates):
                    kwargs = {**kwargs, "probe_ids": await profiles_mod.claim_muse_probe(candidates)}
                counts = await profiles_mod.running_counts_async()
                own = profiles_mod._in_use.get(run_id)
                if own in counts:
                    counts[own] = max(0, counts[own] - 1)
            elif any(p.engine == "qwen-local" for p in candidates):
                all_candidates = candidates
                counts = await profiles_mod.running_counts_async()
                own = profiles_mod._in_use.get(run_id)
                if own in counts:
                    counts[own] = max(0, counts[own] - 1)
            selected = await choose_account(candidates, run_id, **kwargs)
            is_pinned_switched = bool(
                pinned_fallback and primary_pair and (
                    selected.engine != primary_pair[0]
                    or (selected.engine == "antigravity" and selected.model != primary_pair[1])
                )
            )
            if task_type or is_pinned_switched or selected.engine == "qwen-local":
                _, currently_blocked = await available_profiles()
                receipt = worker_routing.receipt(
                    effective_task_type or "general", selected, all_candidates, currently_blocked, counts,
                    trigger="initial" if not run.started_at else "fallback_or_resume",
                    fallback_from={"engine": primary_pair[0], "model": primary_pair[1]} if is_pinned_switched and primary_pair else None,
                )
                if selected.engine == "qwen-local":
                    receipt["scope"] = {"account_scope": sess.pool, "runtime_scope": "local-machine",
                                        "workspace_id": str(sess.workspace_id)}
                    receipt["quota"] = None
                    receipt["readiness"] = "passed"
                else:
                    receipt["scope"] = {"account_scope": sess.pool, "runtime_scope": "provider-account",
                                        "workspace_id": str(sess.workspace_id)}
                events.append(db, run, "worker.routing", receipt)
            return selected

        prev_engine: str | None = None

        def conversation_key(profile):
            if is_meeting:
                return meeting_routing.conversation_key(profile)
            if task_type or profile.engine in {"antigravity", "muse"} or (pinned_fallback and profile.engine != sess.engine):
                return worker_routing.conversation_key(profile)
            return profile.key

        def lookup_resume_id(profile):
            es = sess.engine_sessions or {}
            if profile.engine == "qwen-local" and prev_engine not in (None, "qwen-local"):
                # Engine switch: never reuse a provider conversation id as a Qwen
                # resume id. Progress travels as handoff text instead.
                return None
            if saved := es.get(conversation_key(profile)):
                return saved
            # Legacy account-only IDs identify the original model. They cannot be
            # reused for another model sharing the same provider account.
            if profile.engine == sess.engine and (
                profile.engine not in {"antigravity", "muse"}
                or profile.model == worker_routing.primary_target(sess.engine, requested_model)[1]
            ):
                return es.get(profile.key)
            return None

        prof = await choose(remaining, run.id, idle_first=not is_orch, prefer_engine=prefer, balance_usage=True)

        # 한도 대기 뒤 이어 받는 Run 은 이미 시작했던 Run 이다. 시작 시각·체크포인트는 처음 것을 지키고 인계 내용을 넘긴다.
        resumed = run.started_at is not None
        routing_request = {"model": requested_model, "engine": sess.engine}
        if sess.kind == SessionKind.worker:
            routing_request.update({"task_type": sess.worker_task_type or effective_task_type or "general",
                                    "account_scope": sess.pool})
        run.usage = {**(run.usage or {}), "routing_request": routing_request}
        resume_handoff = run.resume_handoff or (await _waiting_handoff(db, run) if resumed else "")
        self._disarm(run.id)
        run.status = RunStatus.running
        run.error = None
        run.resume_handoff = None
        run.resume_after = None
        run.started_at = run.started_at or utcnow()
        events.append(db, run, "run.status", {"status": RunStatus.running})
        if resumed:
            events.append(db, run, "run.resumed", {"account": prof.name, "attempt": run.resume_attempts})
        last_fallback: dict[str, object] | None = None
        if pinned_fallback and primary_pair and (
            prof.engine != primary_pair[0]
            or (prof.engine == "antigravity" and prof.model != primary_pair[1])
        ):
            primary_blocked = [
                (p, u) for p, u in blocked
                if p.engine == primary_pair[0] and (p.engine != "antigravity" or p.model == primary_pair[1])
            ]
            from_prof, from_until = min(primary_blocked, key=lambda b: b[1]) if primary_blocked else (
                profiles_mod.Profile(id=uuid.UUID(int=0), name=primary_pair[0], engine=primary_pair[0], model=primary_pair[1]),
                None,
            )
            tier = (
                "same_family"
                if (prof.engine, prof.model) in worker_routing.same_family_counterparts(*primary_pair)
                else "task_fit"
            )
            last_fallback = {
                "from": from_prof.name, "from_engine": from_prof.engine, "from_model": from_prof.model,
                "to": prof.name, "to_engine": prof.engine, "to_model": prof.model,
                "reason": "usage_limit", "fallback_tier": tier,
                "detail": f"고정 엔진({from_prof.engine}/{from_prof.model}) 한도로 {prof.engine}/{prof.model}({prof.name}) 경로로 전환",
                "reset_at": _iso(from_until),
                "mode": "resume" if lookup_resume_id(prof) else "handoff",
            }
            events.append(db, run, "engine.fallback", last_fallback)
        await self._commit(db, run)

        untracked_before: set[str] = set()
        if ws.is_git:
            if run.checkpoint_ref:
                untracked_before = await _checkpoint_untracked(db, run)
            else:
                untracked_before = await _git_untracked(root)
                ref = await _git_checkpoint(root, run.id)
                if ref:
                    run.checkpoint_ref = ref
                    events.append(db, run, "checkpoint", {"ref": ref, "untracked": sorted(untracked_before)[:MAX_CHECKPOINT_UNTRACKED]})

        outbox = s.outbox_root / str(run.id)
        outbox.mkdir(parents=True, exist_ok=True)
        run.outbox_path = str(outbox)
        # 업로드 폴더는 guard 보호 영역(~/.ttakkari) 안이라 에이전트가 첨부를 읽으려면 허용 목록에 있어야 한다.
        write_roots = [root, outbox, s.uploads_root, s.inbox_root, *[Path(p) for p in ws.extra_writable_roots], *_tmp_dirs()]

        # ArtCraft 작업(ADR 0039)은 첨부를 작업 폴더로 복사해 안내하고 앱 MCP 서버를 붙인다. 일반 세션은 plan 이 None.
        art_plan = await _artcraft_plan(db, sess, run, outbox)
        base_prompt = art_plan.prompt if art_plan else await _compose_prompt(db, run)
        context_handoff = (run.usage or {}).get("context_handoff") or {}
        base_prompt = context_handoff.get("text", "") + base_prompt
        if (run.usage or {}).get("context_routing", {}).get("action") == "review":
            base_prompt += ("\n[컨텍스트 검토] 같은 작업의 연속성·남은 분량·기록·재로딩 비용을 비교한다. "
                            "새 단계라면 기록 후 fresh_context/reset_my_context를 사용한다. "
                            "장기 작업은 handoff 스킬의 task-handoff, 단기 교대는 일반 handoff를 활용한다.\n")
        if is_orch:
            # 시스템 프롬프트가 아니라 지시 앞에 붙인다. 매 턴 바뀌는 값이 시스템 프롬프트에 있으면 프롬프트 캐시가 깨진다.
            ctx = await context_usage.latest(db, sess)
            base_prompt = f"[내 대화 컨텍스트 사용률: {context_usage.describe(ctx)}]\n" + base_prompt
        system_note = await _system_note(db, outbox, sess, scope="work" if sess.pool == "work" else "all")
        if art_plan:
            system_note += "\n\n" + art_plan.note
        h = self._handles.get(run.id)
        state = ParseState()
        rc: int | None = None
        timed_out = False
        attempt_handoff = resume_handoff
        paused_handoff: str | None = None
        qwen_state: str | None = None
        while True:
            remaining.remove(prof)
            resume_id = lookup_resume_id(prof)
            prompt = base_prompt
            resumed_model = (
                prof.model
                if (task_type or (pinned_fallback and (prof.engine != sess.engine or prof.engine == "antigravity")))
                else None
            )
            # 처음 쓰는 계정은 앞선 대화를 통째로, 쓴 적 있는 계정은 그 뒤 다른 계정에서 오간 대화만 받는다.
            prompt = await _handoff_context(db, run, resumed_profile=prof.name if resume_id else None,
                                            resumed_model=resumed_model) + prompt
            prompt = attempt_handoff + prompt
            mcp_servers = {} if prof.engine == "qwen-local" else {
                MCP_SERVER_NAME: mcp_server_spec(sess.id, "orchestrator" if is_orch else "agent"),
                **(art_plan.mcp_servers if art_plan else {}),
            }
            run.engine, run.profile = prof.engine, prof.name
            if is_meeting:
                model, effort = prof.model, meeting_routing.effort(prof)
                run.model, run.effort = model, effort
            elif task_type or pinned_fallback:
                model, effort = prof.model, worker_routing.effort(prof, run.effort or sess.effort)
                run.model, run.effort = model, effort
            else:
                model, effort = _launch_model(sess, run, prof, profiles[0])
            spec = LaunchSpec(
                cwd=root, prompt=prompt, engine_session_id=resume_id,
                model=model, effort=effort,
                outbox=outbox, write_roots=write_roots, system_note=system_note,
                codex_sandbox=ws.codex_sandbox, mcp_servers=mcp_servers, session_id=str(sess.id),
            )
            adapter = QWEN_LOCAL_ADAPTER if prof.engine == "qwen-local" else ADAPTERS[prof.engine]
            env = adapter.child_env() if prof.engine == "qwen-local" else (
                await asyncio.to_thread(prof.env, _child_env(run.id, write_roots, s.home, s.guard_log))
            )
            await self._commit(db, run)
            attempt_start_seq = run.last_seq
            state, rc, timed_out, missing = await self._attempt(db, run, adapter, spec, env, root, h, prof.id)
            if state.rate_limits:
                usage_mod.remember_stream(prof.id, usage_mod.windows_from_stream(state.rate_limits))
            if state.engine_session_id:
                updates = {conversation_key(prof): state.engine_session_id}
                if not task_type and prof.engine == sess.engine and prof.engine != "antigravity":
                    updates[prof.key] = state.engine_session_id
                sess.engine_sessions = {**(sess.engine_sessions or {}), **updates}
                sess.engine_session_id = state.engine_session_id
            failed = missing or rc != 0 or state.is_error
            stopped = timed_out or self._shutting_down or (h is not None and h.cancel_requested)
            tail = "".join(h.stderr_tail)[-STDERR_TAIL:] if h else ""
            texts = (state.error, tail, None if rc == 0 else state.result_text, state.limit_text)
            if prof.engine == "antigravity" and prof.config_dir and not stopped:
                # Only this account loses readiness; the host account and other managed homes are untouched.
                from ttakkari import antigravity_accounts as managed_agy
                with contextlib.suppress(ValueError, OSError):
                    await asyncio.to_thread(
                        managed_agy.record_run, prof.config_dir, success=not failed,
                        error=" ".join(t for t in texts if t)[:2000] if failed else None)
            limited = (failed and prof.engine != "qwen-local"
                       and (state.limit_hit or profiles_mod.is_usage_limit(*texts)))
            reset_at: float | None = None
            if limited and not stopped:
                # 마지막 계정이어도 쉬게 해야 다음 지시가 한도 걸린 계정부터 다시 두드리지 않는다.
                reset_at = state.limit_resets_at or profiles_mod.reset_epoch_from(*texts)
                profiles_mod.cool_down(prof.limit_key, None if reset_at is None else max(1, int(reset_at - time.time()) + profiles_mod.RESET_BUFFER))
                events.append(db, run, "account.limited", {
                    "account": prof.name, "engine": prof.engine, "model": model, "reset_at": _iso(reset_at),
                    "detail": (state.error or tail or "")[:300],
                })
            # 계정이 모르는 모델이라 CLI 가 거절했다. 같은 계정·모델을 다시 고르지 않고 다음 후보로 넘어간다.
            invalid_model = (failed and not stopped and prof.engine == "antigravity"
                             and agy_models.is_invalid_model(*texts))
            if invalid_model:
                agy_models.reject(prof.id, prof.model, account=prof.name, detail=(state.error or tail or ""))
                remaining = [p for p in remaining if (p.id, p.model) != (prof.id, prof.model)]
                events.append(db, run, "account.model_unsupported", {
                    "account": prof.name, "engine": prof.engine, "model": prof.model,
                    "detail": (state.error or tail or "")[:300],
                })
            # 이 Run 이 도는 동안 다른 Run 이 한도에 걸린 계정은 넘어갈 후보에서 뺀다.
            remaining = [p for p in remaining if not profiles_mod.profile_limited_until(p)]
            if missing and task_type:
                # All models of an engine use the same executable. A different model cannot
                # recover a missing binary; continue with a suitable different engine once.
                remaining = [p for p in remaining if p.engine != prof.engine]
            elif missing and pinned_fallback:
                remaining = []
            if limited and not stopped and not remaining:
                # Mid-run exhaustion: no usable cloud candidate left. The existing
                # fallback event below records the switch with saved progress.
                emergency, qwen_state = await self._emergency_qwen(sess, effective_task_type)
                if emergency is not None:
                    remaining = [emergency]
            if stopped or not failed or not (missing or limited or invalid_model) or not remaining:
                if missing and not remaining:
                    await self._finish(db, run, RunStatus.failed, error=f"{prof.engine} 실행 파일을 찾을 수 없습니다.")
                    return
                if limited and not stopped:
                    # 켜진 계정을 다 써 봤다. 실패로 끝내지 않고 초기화를 기다렸다가 이어서 한다.
                    paused_handoff = await _attempt_handoff(db, run, prof.name, state, tail, 0, waiting=True)
                break
            nxt = await choose(remaining, run.id, prefer_engine=prefer, balance_usage=True)
            tier = None
            if pinned_fallback and primary_pair and (
                nxt.engine != primary_pair[0]
                or (nxt.engine == "antigravity" and nxt.model != primary_pair[1])
            ):
                tier = (
                    "same_family"
                    if (nxt.engine, nxt.model) in worker_routing.same_family_counterparts(*primary_pair)
                    else "task_fit"
                )
            from_model = model or (primary_pair[1] if primary_pair and prof.engine == primary_pair[0] else prof.model)
            last_fallback = {
                "from": prof.name, "from_engine": prof.engine, "from_model": from_model,
                "to": nxt.name, "to_engine": nxt.engine, "to_model": nxt.model,
                "reason": "missing" if missing else "invalid_model" if invalid_model else "usage_limit",
                "fallback_tier": tier,
                "detail": (state.error or tail or "")[:500], "reset_at": _iso(reset_at),
                # resume: 그 계정의 엔진 대화를 이어감 · handoff: 대화·진행 요약을 새 대화에 실어 보냄
                "mode": "resume" if lookup_resume_id(nxt) else "handoff",
            }
            events.append(db, run, "engine.fallback", last_fallback)
            attempt_handoff = await _attempt_handoff(db, run, prof.name, state, tail, attempt_start_seq)
            if h:
                h.stderr_tail.clear()
            prev_engine = prof.engine
            prof = nxt
        profiles_mod.release(run.id)
        if paused_handoff is not None:
            _, blocked = await available_profiles()
            if blocked:
                await self._wait_for_reset(db, run, sess, blocked, handoff=paused_handoff,
                                           qwen_state=qwen_state)
                return
        run.exit_code = rc

        sess.updated_at = utcnow()
        if sess.id in self._reset_requests:
            self._reset_requests.discard(sess.id)
            context_usage.reset_engine_sessions(sess)
        run.cost_usd = state.cost_usd
        selected_model = model or run.model or (
            prof.model or worker_routing.primary_target(prof.engine, sess.model)[1]
            if prof.engine in profiles_mod.ENGINES else None
        )
        verify_needed = worker_routing.needs_independent_verification(effective_task_type, selected_model)
        routing_usage: dict[str, object] = {
            "routing_request": routing_request,
            "task_type": effective_task_type,
            "selected_engine": prof.engine,
            "selected_model": selected_model,
            "verification_required": verify_needed,
            "verification_status": "pending" if verify_needed else "not_required",
            "false_completion": None,
        }
        if last_fallback is not None:
            routing_usage["fallback"] = last_fallback
        run.usage = {**(run.usage or {}), **state.usage, **context_usage.measure(state, prof.engine, prof.config_dir), **routing_usage}
        run.result_text = state.result_text or state.last_message

        diff = None
        if ws.is_git and run.checkpoint_ref:
            # 엔진마다 파일 변경 보고 방식이 달라(codex 는 셸로 고치기도 한다) git 기준으로 변경 파일을 다시 모은다.
            diff = await _git_diff_stat(root, run.checkpoint_ref, untracked_before)
            if diff:
                for f in diff["files"] + [{"path": p} for p in diff["new_files"]]:
                    state.touched_files.add(str(root / f["path"]))
        if art_plan:
            await asyncio.to_thread(artcraft_tasks.sweep_outputs, art_plan, run, outbox)
        from ttakkari import reverse_tasks
        await reverse_tasks.collect_outputs(db, run, outbox)
        await self._collect_artifacts(db, run, ws, outbox, state)
        if diff:
            events.append(db, run, "run.diff", diff)

        if self._shutting_down and not (h and h.cancel_requested):
            await self._finish(db, run, RunStatus.interrupted, error="서버 종료로 중단")
        elif h and h.cancel_requested:
            await self._finish(db, run, RunStatus.cancelled)
        elif timed_out:
            error = f"시간 초과({s.run_timeout_seconds}초)"
            if run.resume_attempts:
                await self._retry_restart_resume(db, run, error)
            else:
                await self._finish(db, run, RunStatus.failed, error=error)
        elif rc == 0 and not state.is_error:
            await self._finish(db, run, RunStatus.succeeded)
        else:
            tail = "".join(h.stderr_tail)[-STDERR_TAIL:] if h else ""
            error = state.error or tail.strip() or f"exit code {rc}"
            if run.resume_attempts and not (h and h.cancel_requested):
                await self._retry_restart_resume(db, run, error)
            else:
                await self._finish(db, run, RunStatus.failed, error=error)

    async def _retry_restart_resume(self, db: AsyncSession, run: Run, error: str) -> None:
        if run.resume_attempts >= MAX_RESTART_RESUME_ATTEMPTS:
            await self._finish(db, run, RunStatus.failed,
                               error=f"재개 {MAX_RESTART_RESUME_ATTEMPTS}회 실패 후 중단: {error}")
            return
        run.resume_attempts += 1
        run.status = RunStatus.queued
        run.finished_at = None
        run.error = None
        run.resume_handoff = f"[이전 재개 실패: {error[:500]}]\n" + await _interrupted_handoff(db, run)
        delay = RESTART_RESUME_BACKOFF_SECONDS * (2 ** (run.resume_attempts - 1))
        until = time.time() + delay
        run.resume_after = datetime.fromtimestamp(until, UTC)
        events.append(db, run, "run.status", {"status": RunStatus.queued, "restart_resume_retry": True,
                                               "attempt": run.resume_attempts, "error": error[:500],
                                               "resume_after": _iso(until)})
        await self._commit(db, run)
        self._arm(run.id, run.session_id, until)

    async def _attempt(self, db: AsyncSession, run: Run, adapter: AgentAdapter, spec: LaunchSpec, env: dict[str, str],
                       root: Path, h: _Handle | None, account_id: uuid.UUID | None = None) -> tuple[ParseState, int | None, bool, bool]:
        """엔진 프로세스 한 번. (상태, 종료 코드, 시간 초과, 실행 파일 없음)."""
        s = get_settings()
        state = ParseState()
        if adapter.engine == "antigravity" or getattr(adapter, "validates_resume_identity", False):
            state.engine_session_id = spec.engine_session_id
        qwen_slot = adapter.engine == "qwen-local"
        qwen_slot_acquired = False
        try:
            if qwen_slot:
                await self._qwen_local_slot.acquire()
                qwen_slot_acquired = True
            cmd = adapter.build_command(spec, env)
            working_directory = getattr(adapter, "working_directory", lambda _spec: root)(spec)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(working_directory), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=32 * 1024 * 1024,
            )
        except FileNotFoundError:
            cleanup = getattr(adapter, "cleanup", None)
            if cleanup:
                cleanup(spec)
            if qwen_slot_acquired:
                self._qwen_local_slot.release()
            return state, None, False, True
        except OSError:
            cleanup = getattr(adapter, "cleanup", None)
            if cleanup:
                cleanup(spec)
            if qwen_slot_acquired:
                self._qwen_local_slot.release()
            raise
        except BaseException:
            cleanup = getattr(adapter, "cleanup", None)
            if cleanup:
                cleanup(spec)
            if qwen_slot_acquired:
                self._qwen_local_slot.release()
            raise
        if h:
            h.proc = proc
        run.pid = proc.pid
        stderr_task = asyncio.create_task(_drain_stderr(proc, h))
        heartbeat_task: asyncio.Task | None = None
        timed_out = False
        try:
            await self._commit(db, run)
            heartbeat_task = asyncio.create_task(_heartbeat(run.id), name=f"heartbeat-{run.id}")
            await asyncio.wait_for(self._pump(db, run, proc, adapter, state, root, account_id, spec), s.run_timeout_seconds)
        except TimeoutError:
            timed_out = True
            await self._terminate(proc)
        except asyncio.CancelledError:
            await self._terminate(proc)
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            try:
                if proc.returncode is None:
                    await self._terminate(proc)
                await proc.wait()
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(stderr_task, 5)
                if adapter.engine == "qwen-local" and h and h.stderr_tail:
                    events.append(db, run, "log", {"stream": "stderr",
                                                    "text": "".join(h.stderr_tail)[-STDERR_TAIL:]})
                    await self._commit(db, run)
                if adapter.engine == "antigravity":
                    with contextlib.suppress(OSError):
                        (Path("/private/tmp") / f"ttakkari-agy-{run.id}.log").unlink()
                    # agy loads/reconnects MCP after init. Retain the private config
                    # through the attempt, but remove it even if termination fails.
                    # A signal failure still propagates; it is not proof of shutdown.
                cleanup = getattr(adapter, "cleanup", None)
                try:
                    if cleanup:
                        cleanup(spec)
                finally:
                    if qwen_slot_acquired:
                        self._qwen_local_slot.release()
        rc = proc.returncode
        finalize_process = getattr(adapter, "finalize_process", None)
        if finalize_process:
            finalize_process(
                state, return_code=rc,
                stderr="".join(h.stderr_tail)[-STDERR_TAIL:] if h else "",
            )
        return state, rc, timed_out, False

    async def _pump(self, db: AsyncSession, run: Run, proc: asyncio.subprocess.Process,
                    adapter: AgentAdapter, state: ParseState, cwd: Path,
                    account_id: uuid.UUID | None = None, spec: LaunchSpec | None = None) -> None:
        assert proc.stdout is not None
        seen_limits = None
        seen_account_usage = None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            parse_stdout = getattr(adapter, "parse_stdout_line", None)
            if parse_stdout:
                produced = parse_stdout(text, state, cwd)
                for n in produced:
                    events.append(db, run, n.type, n.payload)
                if produced:
                    await self._commit(db, run)
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
            if state.resume_mismatch:
                for n in produced:
                    events.append(db, run, n.type, n.payload)
                await self._commit(db, run)
                return
            if account_id is not None and state.rate_limits is not seen_limits:
                # 실행 중 엔진이 알려 준 사용량을 바로 반영해 수집 주기 사이에도 값이 신선하게 한다.
                seen_limits = state.rate_limits
                usage_mod.remember_stream(account_id, usage_mod.windows_from_stream(seen_limits or {}))
            if account_id is not None and state.account_usage is not seen_account_usage:
                seen_account_usage = state.account_usage
                if seen_account_usage:
                    usage_mod.remember_observation(account_id, seen_account_usage)
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
        self._disarm(run.id)
        run.status = status
        run.finished_at = utcnow()
        if error:
            run.error = error[:8000]
        usage_info = run.usage or {}
        events.append(db, run, "run.finished", {
            "status": status, "error": run.error, "result_text": run.result_text,
            "cost_usd": run.cost_usd, "exit_code": run.exit_code,
            "engine": run.engine, "profile": run.profile, "model": run.model,
            "task_type": usage_info.get("task_type"),
            "verification_required": usage_info.get("verification_required", False),
            "verification_status": usage_info.get("verification_status"),
            "fallback": usage_info.get("fallback"),
        })
        record(db, actor="agent", action="run.finished", outcome="ok" if status == RunStatus.succeeded else "error",
               target_type="run", target_id=run.id, detail={"status": status})
        sess = await db.get(ChatSession, run.session_id)
        if self.runtime and sess:
            from ttakkari.generation_service import apply_reset
            await apply_reset(db, sess)
        from ttakkari import reverse_tasks
        reverse_next = await reverse_tasks.on_finished(db, run)
        parent_id = await _enqueue_report(db, sess, run) if sess is not None else None
        if sess is not None:
            await _post_lead_report(db, sess, run)
        await self._commit(db, run)
        if reverse_next is not None:
            _spawn(self.dispatch(reverse_next))
        if parent_id is not None:
            _spawn(self.dispatch(parent_id))
        # 푸시 서버가 느려도 Run 슬롯을 붙잡지 않게 따로 띄운다.
        _spawn(_notify(run, sess.title if sess else None))

    async def _commit(self, db: AsyncSession, run: Run) -> None:
        h = self._handles.get(run.id)
        if h and h.owner and h.token:
            await h.owner.fence(db, run.id, h.token)
            await generation.wake(db, run.id)
        run.heartbeat_at = utcnow()
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


async def _notify_waiting(run: Run, title: str | None, until: float) -> None:
    try:
        when = datetime.fromtimestamp(until).astimezone().strftime("%m/%d %H:%M")
        name = (title or "새 세션")[:60]
        await notify(PushKind.run_waiting, "작업 대기 · 사용량 한도", f"{name} · {when}에 이어서 진행",
                     url=f"/chat/{run.session_id}", tag=f"{run.session_id}-waiting", target_id=run.id)
    except Exception:
        log.exception("push notification failed for run %s", run.id)


async def _heartbeat(run_id: uuid.UUID) -> None:
    """Persist liveness independently of agent output, without contending on RunEvent.seq."""
    while True:
        await asyncio.sleep(RUN_HEARTBEAT_SECONDS)
        async with sessionmaker()() as db:
            run = await db.get(Run, run_id)
            if run is None or run.status != RunStatus.running:
                return
            run.heartbeat_at = utcnow()
            await db.commit()


manager = RunManager()


async def recover_interrupted() -> int:
    """서버가 죽었다 살아나면 실행 중이던 Run 은 프로세스를 잃은 것이다. 상태를 정리해 사용자가 이어서 지시할 수 있게 한다."""
    async with sessionmaker()() as db:
        # An accidentally started legacy server must not perform a global sweep
        # over a generation cluster, even if it currently has no active Runs.
        from sqlalchemy import text
        if await db.scalar(text("SELECT to_regclass('public.runtime_control')")):
            from ttakkari.models import RuntimeControl
            if await db.scalar(select(RuntimeControl.active_generation).where(RuntimeControl.id == 1)):
                raise RuntimeError("generation runtime is registered; legacy recovery is forbidden")
        # 시작하지 않은 대기 Run 은 그대로 두고 resume_queued 가 이어서 돌린다.
        rows = list(await db.scalars(select(Run).where(Run.status == RunStatus.running)))
        for run in rows:
            if run.pid and _looks_like_agent(run.pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(run.pid, signal.SIGTERM)
            run.heartbeat_at = utcnow()
            run.status = RunStatus.interrupted
            run.pid = None
            run.finished_at = utcnow()
            run.error = "서버 종료로 중단"
            run.resume_handoff = await _interrupted_handoff(db, run)
            events.append(db, run, "run.finished", {"status": RunStatus.interrupted, "error": run.error,
                                                    "result_text": None, "cost_usd": None, "exit_code": None})
        await db.commit()
        return len(rows)


async def resume_interrupted() -> int:
    """Queue only server-shutdown interruptions, with persisted bounded exponential backoff."""
    async with sessionmaker()() as db:
        rows = list(await db.scalars(
            select(Run).where(Run.status == RunStatus.interrupted, Run.error == "서버 종료로 중단",
                              Run.resume_attempts < MAX_RESTART_RESUME_ATTEMPTS)
            .order_by(Run.created_at).with_for_update(skip_locked=True)
        ))
        waiting: list[tuple[uuid.UUID, uuid.UUID, float]] = []
        now = time.time()
        for run in rows:
            if not run.resume_handoff:
                run.resume_handoff = await _interrupted_handoff(db, run)
            run.resume_attempts += 1
            run.status = RunStatus.queued
            run.finished_at = None
            run.error = None
            events.append(db, run, "run.status", {"status": RunStatus.queued, "restart_resume": True,
                                                   "attempt": run.resume_attempts})
            delay = RESTART_RESUME_BACKOFF_SECONDS * (2 ** (run.resume_attempts - 1))
            until = now + delay
            run.resume_after = datetime.fromtimestamp(until, UTC)
            waiting.append((run.id, run.session_id, until))
        await db.commit()
    for run_id, session_id, until in waiting:
        if until > time.time():
            manager._arm(run_id, session_id, until)
        else:
            await manager.dispatch(session_id)
    return len(waiting)


async def resume_queued() -> int:
    await _reconsider_quota_waits()
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(Run).where(Run.status == RunStatus.queued).order_by(Run.created_at)))
    now = utcnow()
    for run in rows:
        if run.resume_after:
            if run.resume_after > now:
                manager._arm(run.id, run.session_id, run.resume_after.timestamp())
            else:
                run.resume_after = None
                manager._disarm(run.id)
    for sid in {run.session_id for run in rows}:
        await manager.dispatch(sid)
    return len({run.session_id for run in rows})


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

def _routing_policy(sess: ChatSession, run: Run, is_meeting: bool):
    from ttakkari.runs import worker_routing
    # run.model describes the last actual attempt after a fallback. Keep the original
    # request stable across waits/restarts, including same-engine model switches.
    request = (run.usage or {}).get("routing_request")
    requested = request.get("model") if isinstance(request, dict) else (
        sess.model if run.started_at else run.model or sess.model
    )
    task = ((sess.worker_task_type or worker_routing.infer_task_type(requested))
            if sess.kind == SessionKind.worker and sess.engine == "auto" and not is_meeting else None)
    pinned = (sess.engine in profiles_mod.ENGINES and sess.kind not in {SessionKind.orchestrator, SessionKind.desk}
              and not is_meeting and run.origin != RunOrigin.system and not sess.strict_engine)
    return task, pinned, requested


async def _available_profiles(db: AsyncSession, sess: ChatSession, run: Run, is_meeting: bool):
    from ttakkari.meetings import routing as meeting_routing
    from ttakkari.runs import worker_routing
    task, pinned, requested = _routing_policy(sess, run, is_meeting)
    if is_meeting:
        return await meeting_routing.availability(db)
    if sess.engine == "qwen-local":
        ready, _reason = await asyncio.to_thread(QWEN_LOCAL_ADAPTER.readiness)
        if not ready or sess.kind != SessionKind.worker:
            return [], []
        profile_id = uuid.uuid5(uuid.NAMESPACE_URL, f"ttakkari:qwen-local:{sess.pool}")
        return [profiles_mod.Profile(id=profile_id, name=f"Qwen local ({sess.pool})", engine="qwen-local",
                                     model=QWEN_LOCAL_MODEL)], []
    if task:
        return await worker_routing.availability(db, task, sess.pool, model=requested)
    if pinned:
        return await worker_routing.pinned_availability(
            db, sess.engine, sess.pool, model=requested, task_type=sess.worker_task_type,
        )
    return await profiles_mod.availability(db, sess.engine, sess.pool, model=requested)


async def _reconsider_quota_waits() -> None:
    """A stored reset is a wakeup hint, not a veto on newly eligible routes.

    Only the active coordinator can clear unowned quota waits. It never steals a
    lease, changes cancellation/backoff, or launches an engine while holding locks.
    """
    from ttakkari.models import RuntimeLease
    awoken_sessions: set[uuid.UUID] = set()
    async with sessionmaker()() as db:
        if manager.runtime:
            from ttakkari.generation import _control
            control = await _control(db)
            if control.active_generation != manager.runtime.id:
                return
        rows = list(await db.scalars(select(Run).where(
            Run.status == RunStatus.queued, Run.resume_after.is_not(None),
            ~select(RuntimeLease.run_id).where(RuntimeLease.run_id == Run.id).exists(),
        ).with_for_update(skip_locked=True)))
        now = utcnow()
        for run in rows:
            latest = await db.scalar(select(RunEvent).where(RunEvent.run_id == run.id)
                                     .order_by(RunEvent.seq.desc()).limit(1))
            if latest is None or latest.type != "run.waiting":
                continue
            sess = await db.get(ChatSession, run.session_id)
            is_meeting = await db.scalar(select(Meeting.id).where(Meeting.analysis_run_id == run.id)) is not None
            ready, _ = await _available_profiles(db, sess, run, is_meeting)
            if ready or (run.resume_after and run.resume_after <= now):
                # _waiting_handoff retains the previous provider progress even for a
                # legacy wait that predates persisted resume_handoff.
                run.resume_after = None
                manager._disarm(run.id)
                awoken_sessions.add(run.session_id)
        if awoken_sessions and manager.runtime:
            from ttakkari import generation
            await generation.wake(db)
        await db.commit()
    for sid in awoken_sessions:
        await manager.dispatch(sid)

def _looks_like_agent(pid: int) -> bool:
    """재부팅 뒤에는 PID 가 재사용될 수 있다. 엉뚱한 프로세스 그룹을 죽이지 않게 이름을 확인한다."""
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                             check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(name in out for name in ("claude", "codex", "/agy ", "antigravity", "/muse ", "muse-runtime/bridge.mjs"))

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


async def _artcraft_plan(db: AsyncSession, sess: ChatSession, run: Run, outbox: Path):
    """ArtCraft 작업 세션이면 실행 계획, 아니면 None. 준비가 실패해도 Run 은 계속한다(안내만 줄어든다)."""
    try:
        return await artcraft_tasks.prepare_run(db, sess, run, outbox)
    except Exception as exc:  # noqa: BLE001
        events.append(db, run, "log", {"stream": "system", "text": f"ArtCraft 작업 준비 실패: {exc}"})
        return None


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


async def _system_note(db: AsyncSession, outbox: Path, sess: ChatSession, scope: str = "all") -> str:
    """기본 안내 + 도구 안내 + 비서 지침(~/.ttakkari/harness, 공통 + 역할) + 고정 기억. 매 Run 에 들어가므로 짧게 유지한다(ADR 0007, 0020)."""
    parts = [SYSTEM_NOTE.format(outbox=outbox), AGENT_NOTE, capabilities.prompt_note()]
    role: instructions_mod.Role | None = None
    if sess.kind == SessionKind.orchestrator:
        # 접수가 만든 기획 세션은 사용자와 직접 대화하지 않는다. 옛 메인 오케스트레이터(부모 없음)는 그대로.
        parts.append(LEAD_NOTE if sess.parent_id else ORCHESTRATOR_NOTE)
        role = "lead"
    elif sess.kind == SessionKind.worker:
        role = "worker"
    for label, text in (("User's standing instructions for this assistant", instructions_mod.read("common")),
                        ("Instructions for your role", instructions_mod.read(role) if role else "")):
        if text:
            parts.append(f"[{label}]\n{text}")
    brief = await pinned_brief(db, scope=scope)
    if brief:
        parts.append("[Pinned facts about the user — use recall for more]\n" + brief)
    if sess.kind == SessionKind.orchestrator:
        from ttakkari.orchestrator.runtime import model_routing_note
        parts.append(model_routing_note())
    return "\n\n".join(parts)


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat()


def _launch_model(sess: ChatSession, run: Run, prof: profiles_mod.Profile, first: profiles_mod.Profile) -> tuple[str | None, str | None]:
    """계정에 넘길 (모델, 생각 수준). 모델 이름은 엔진마다 다르다. 그 엔진의 모델일 때만 넘기고, 어느 엔진 것인지 모르면 첫 계정에만 넘긴다.

    메인 오케스트레이터는 엔진마다 허용된 한 쌍(설정 orchestrator_fallback_models)으로만 띄운다.
    작업 세션이 다른 엔진으로 넘어가면 같은 급의 상대 모델(model_counterparts)로 바꾼다."""
    s = get_settings()
    if prof.engine == "muse" and sess.kind != SessionKind.orchestrator:
        return prof.model or run.model or s.worker_default_models["muse"]["model"], run.effort or "high"
    if prof.engine == "antigravity" and sess.kind != SessionKind.orchestrator:
        model = prof.model or run.model
        return model, worker_routing.effort(prof, run.effort)
    if sess.kind == SessionKind.orchestrator:
        fb = s.orchestrator_fallback_models.get(prof.engine) or {}
        return fb.get("model"), fb.get("effort") or run.effort
    if (profiles_mod.model_engine(run.model) or first.engine) == prof.engine:
        return run.model, run.effort
    peer = s.model_counterparts.get((run.model or "").lower())
    return (peer if profiles_mod.model_engine(peer) == prof.engine else None), run.effort


async def _waiting_handoff(db: AsyncSession, run: Run) -> str:
    ev = await db.scalar(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "run.waiting")
                         .order_by(RunEvent.seq.desc()).limit(1))
    return str((ev.payload or {}).get("handoff") or "") if ev else ""


async def _interrupted_handoff(db: AsyncSession, run: Run) -> str:
    """Persist progress for restart recovery and warn that side effects may already exist."""
    rows = list(await db.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id,
                               RunEvent.type.in_(("message", "tool.call", "tool.result", "guard.blocked")))
        .order_by(RunEvent.seq.desc()).limit(24)
    ))
    lines = [
        "[서버 종료 후 이어서 실행: 원래 지시를 계속한다]",
        "파일 변경과 외부 작업이 이미 반영됐는지 먼저 확인하라. 완료된 작업을 반복하거나 중복 생성하지 말라.",
        "마지막 진행 기록:",
    ]
    for event in reversed(rows):
        payload = event.payload or {}
        detail = payload.get("text") or payload.get("output") or payload.get("reason")
        if event.type == "tool.call":
            detail = f"{payload.get('name')} {json.dumps(payload.get('input'), ensure_ascii=False)[:300]}"
        if detail:
            lines.append(f"{event.type}: {str(detail)[:500]}")
    if run.profile:
        lines.append(f"이전 엔진 프로필: {run.profile}; 엔진 세션은 저장된 대화 ID로 이어간다.")
    return "\n".join(lines)[:WAIT_HANDOFF_CHARS] + "\n\n"


async def _checkpoint_untracked(db: AsyncSession, run: Run) -> set[str]:
    ev = await db.scalar(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "checkpoint")
                         .order_by(RunEvent.seq).limit(1))
    return set((ev.payload or {}).get("untracked") or []) if ev else set()


async def _handoff_context(db: AsyncSession, run: Run, resumed_profile: str | None = None,
                           resumed_model: str | None = None) -> str:
    """다른 엔진·계정에서 오간 앞선 대화(지시·답)를 넘긴다. 도구 호출 내역은 넘기지 않는다(ADR 0006 한계).

    resumed_profile 이 있으면 그 계정이 자기 엔진 대화를 이어 받는 경우다. task 정책 작업자는
    resumed_model까지 일치하는 마지막 Run 뒤의 대화를 넘겨 같은 계정의 다른 모델 진행도 보존한다."""
    stmt = select(Run).where(Run.session_id == run.session_id, Run.id != run.id, Run.status.in_(TERMINAL_RUN_STATUSES))
    if (since := context_usage.reset_at(await db.get(ChatSession, run.session_id))) is not None:
        stmt = stmt.where(Run.started_at >= since)
    rows = list(await db.scalars(stmt.order_by(Run.created_at.desc()).limit(HANDOFF_TURNS)))
    if resumed_profile:
        missed = []
        for r in rows:
            if r.profile == resumed_profile and (resumed_model is None or r.model == resumed_model):
                break
            if r.profile:
                missed.append(r)
        rows = missed
    if not rows:
        return ""
    lines = ["[이 계정이 쉬는 동안 다른 계정에서 오간 대화 — 참고만 하고 아래 새 지시에 답한다]" if resumed_profile
             else "[이전 대화 — 다른 엔진·계정에서 이어받음. 참고만 하고 아래 새 지시에 답한다]"]
    for r in reversed(rows):
        lines.append(f"사용자: {r.prompt[:HANDOFF_CHARS]}")
        if r.result_text or r.error:
            lines.append(f"에이전트: {(r.result_text or f'(실패) {r.error}')[:HANDOFF_CHARS]}")
    return "\n".join(lines) + "\n\n[새 지시]\n"


async def _attempt_handoff(db: AsyncSession, run: Run, profile: str, state: ParseState,
                           stderr: str, since_seq: int, *, waiting: bool = False) -> str:
    """Carry observable progress to the next account without assuming the failed turn was atomic.

    waiting=True 는 한도 초기화를 기다렸다가 이어 받는 경우다. 같은 Run 의 지금까지 진행 전체를 더 길게 싣는다."""
    lines = ["[Previous account handoff]",
             f"Account {profile} stopped on a usage limit: {(state.error or stderr or 'engine unavailable')[:500]}",
             ("This request is not finished. The original request follows below; continue from where the progress "
              "below stops, without repeating completed steps."),
             "Inspect existing work, modified files, checkpoints, and completed tool results before repeating work.",
             "Avoid re-executing non-idempotent operations or creating duplicate files, branches, or runs.",
             "Search recall for relevant memories and indexed work-history; verify the current state first."]
    rows = list(await db.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.seq > since_seq,
                               RunEvent.type.in_(("message", "tool.call", "tool.result", "guard.blocked")))
        .order_by(RunEvent.seq.desc()).limit(24 if waiting else 12)))
    if rows:
        lines.append("Progress so far in this request (oldest first):")
    for event in reversed(rows):
        payload = event.payload or {}
        detail = payload.get("text") or payload.get("output") or payload.get("reason")
        if event.type == "tool.call":
            detail = f"{payload.get('name')} {json.dumps(payload.get('input'), ensure_ascii=False)[:300]}"
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
    text = "\n".join(lines)
    return (text[:WAIT_HANDOFF_CHARS] if waiting else text) + "\n\n"


async def _enqueue_report(db: AsyncSession, sess: ChatSession, run: Run) -> uuid.UUID | None:
    """작업 세션 Run 이 끝나면 오케스트레이터에게 결과 보고 Run 을 넣는다. 시작 전 보고가 있으면 거기에 덧붙인다."""
    if sess.kind != SessionKind.worker or sess.parent_id is None:
        return None
    parent = await db.get(ChatSession, sess.parent_id)
    if parent is None or parent.archived_at is not None:
        return None
    label = {RunStatus.succeeded: "완료", RunStatus.failed: "실패", RunStatus.cancelled: "취소",
             RunStatus.interrupted: "중단"}.get(RunStatus(run.status), run.status)
    usage_info = run.usage or {}
    body = [f"[작업 보고] 작업 세션 '{sess.title}' (worker_id={sess.id}) {label}."]
    if fb := usage_info.get("fallback"):
        body.append(
            f"경로 전환: {fb.get('from_engine')}/{fb.get('from_model')}({fb.get('from')}) → "
            f"{fb.get('to_engine')}/{fb.get('to_model')}({fb.get('to')}) (사유: {fb.get('reason')})"
        )
    if run.status == RunStatus.succeeded and usage_info.get("verification_required"):
        body.append(
            f"독립 검증 필요: 이 작업은 {run.engine}/{run.model or sess.model or 'unknown'} 모델이 수행했습니다. "
            "최종 완료로 판단하기 전에 다른 공급자(task_type='verification', GPT-6 Luna 또는 Claude Sonnet)로 "
            "독립 검증하고, 결과를 message_worker(verification_outcome='passed'|'failed')로 기록하세요."
        )
    if run.result_text:
        body.append("결과:\n" + run.result_text[:REPORT_CHARS])
    if run.error:
        body.append("오류: " + run.error[:1000])
    body.append("작업 세션 컨텍스트: " + context_usage.describe(await context_usage.latest(db, sess)))
    text = "\n".join(body)
    pending = await db.scalar(
        select(Run).where(Run.session_id == parent.id, Run.status == RunStatus.queued, Run.origin == RunOrigin.report)
        .order_by(Run.created_at.desc()).limit(1))
    if pending is not None and manager.runtime is None and not await manager.is_owned(pending.id):
        pending.prompt = pending.prompt + "\n\n" + text
        return parent.id
    report = Run(session_id=parent.id, workspace_id=parent.workspace_id, prompt=text, engine=parent.engine,
                 model=parent.model, effort=parent.effort, origin=RunOrigin.report)
    db.add(report)
    await db.flush()
    events.append(db, report, "run.status", {"status": RunStatus.queued, "ahead": 0})
    parent.updated_at = utcnow()
    return parent.id


async def _post_lead_report(db: AsyncSession, sess: ChatSession, run: Run) -> None:
    """기획 세션 Run 이 끝나면 그 마지막 답을 접수 대화에 보고로 붙인다(ADR 0013). 모델을 다시 부르지 않는다."""
    if sess.kind != SessionKind.orchestrator or sess.parent_id is None:
        return
    parent = await db.get(ChatSession, sess.parent_id)
    if parent is None or parent.kind != SessionKind.desk or parent.archived_at is not None:
        return
    label = {RunStatus.succeeded: "완료", RunStatus.failed: "실패", RunStatus.cancelled: "취소",
             RunStatus.interrupted: "중단"}.get(RunStatus(run.status), run.status)
    text = run.result_text or (f"(실패) {run.error}" if run.error else "")
    if run.status == RunStatus.cancelled and not text:
        return
    now = utcnow()
    report = Run(session_id=parent.id, workspace_id=parent.workspace_id, origin=RunOrigin.report,
                 prompt=f"[기획 보고] 기획 세션 '{sess.title}' (lead_id={sess.id}) {label}.",
                 engine=run.engine, profile=run.profile, model=run.model, status=RunStatus.succeeded,
                 result_text=text, started_at=now, finished_at=now)
    db.add(report)
    await db.flush()
    events.append(db, report, "run.status", {"status": RunStatus.succeeded})
    events.append(db, report, "run.finished", {"status": RunStatus.succeeded, "error": None, "result_text": text,
                                               "cost_usd": None, "exit_code": None, "lead_id": str(sess.id)})
    parent.updated_at = now


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
