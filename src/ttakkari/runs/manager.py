"""Run 수명주기 관리. 프로세스 실행·이벤트 기록·취소·재시작 복구를 한 곳에서만 한다."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import tempfile
import time
import uuid
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psutil
from sqlalchemy import func, select
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari import artcraft_tasks, generation
from ttakkari.agents.antigravity import AntigravityAdapter
from ttakkari.agents.base import AgentAdapter, LaunchSpec, ParseState
from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.agents.codex import CodexAdapter
from ttakkari.agents.copilot import CopilotAdapter
from ttakkari.agents.muse import MuseAdapter
from ttakkari.agents.opencode import OpencodeAdapter
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
from ttakkari.model_catalog import MUSE_EFFORT
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    Artifact,
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
from ttakkari.security import seatbelt

log = logging.getLogger(__name__)

ADAPTERS: dict[str, AgentAdapter] = {
    "claude": ClaudeAdapter(), "codex": CodexAdapter(), "antigravity": AntigravityAdapter(),
    "muse": MuseAdapter(), "opencode": OpencodeAdapter(), "copilot": CopilotAdapter(),
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
# DB 풀 고갈(QueuePool timeout)은 Run 의 잘못이 아니다. 엔진 프로세스가 뜨기 전이면 실패 대신 지터 있는 백오프로 다시 줄 세운다.
MAX_POOL_TIMEOUT_RETRIES = 3
POOL_TIMEOUT_BACKOFF_SECONDS = 5
# 풀에서 Run 시작과 무관한 일(API 요청, heartbeat, 백그라운드)에 남겨 두는 연결 수.
POOL_RESERVED_CONNECTIONS = 2
RUN_HEARTBEAT_SECONDS = 15
REPORT_CHARS = 3000
# 부모 프로세스가 Claude Code·Orca 안에서 떴을 때 자식 에이전트가 중첩 세션으로 오인하지 않게 지운다.
STRIP_ENV_PREFIXES = ("TTAKKARI_", "ORCA_", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID")
# 계정 폴더는 계정 설정으로만 정한다. 백엔드를 띄운 셸(예: Orca)의 값이 기본 계정에 새지 않게 지운다(ADR 0006).
STRIP_ENV_KEYS = {"DATABASE_URL", "PGPASSWORD", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}


def write_roots_for_session(sess: ChatSession, *, root: Path, outbox: Path, settings,
                            extra_writable_roots: Iterable[Path], tmp_dirs: Iterable[Path]) -> list[Path]:
    """Keep Creative Studio intent inference read-only outside its disposable Run outbox."""
    if sess.worker_task_type == "creative_intent":
        return [outbox]
    return [root, outbox, settings.uploads_root, settings.inbox_root,
            *[Path(p) for p in extra_writable_roots], *tmp_dirs]

SYSTEM_NOTE = """\
You are running unattended via Winterholic Ttakkari on the user's Mac Studio. The user is remote, usually on a phone, \
and sees your messages and the files you produce in a web app.
- Save every deliverable file meant for the user (documents, reports, exports, images, archives) into this outbox \
directory: {outbox}. Files there are collected automatically into the agent area, so call `share_file` on each \
deliverable you want the user to open in the conversation; the user can preview and download shared files. Shared files \
stay in AI work files; only keep_artifact (when the user asked in the conversation to keep it) puts a file in the user's library.
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
    process_groups: list[tuple[int, float, int, uuid.UUID, uuid.UUID, int]] = field(default_factory=list)
    start_slot: asyncio.Semaphore | None = None
    # 이 시도가 별도 커밋·dispatch 로 외부에 확정된 부수효과(예: 접수가 넘긴 기획 Run)를 이미 냈다. 있으면 다시 줄 세우지 않는다.
    side_effect_committed: bool = False

    def mark_side_effect(self) -> None:
        self.side_effect_committed = True

    def release_start_slot(self) -> None:
        """엔진 프로세스가 뜨고 첫 커밋으로 연결을 돌려준 뒤 시작 슬롯을 반납한다. 여러 번 불러도 한 번만 반납한다."""
        slot, self.start_slot = self.start_slot, None
        if slot is not None:
            slot.release()


def routing_slots() -> int:
    """동시에 시작 단계(라우팅·프롬프트 준비)에 들어갈 수 있는 Run 수.

    시작 단계의 Run 은 자기 세션의 연결 하나를 쥔 채 안에서 두 번째 연결(choose_profile 등)을 또 얻는다.
    시작하는 Run 이 풀 크기보다 많으면 모든 연결을 바깥 세션이 쥐고 안쪽 연결을 기다려 풀 timeout 까지 서로 막힌다.
    Run 당 2개씩 쓰고 나머지 POOL_RESERVED_CONNECTIONS 개는 비워 두면 이 교착은 구조적으로 생기지 않는다.
    """
    s = get_settings()
    return max(1, (s.database_pool_size + s.database_pool_overflow - POOL_RESERVED_CONNECTIONS) // 2)


class QwenSlotBusy(RuntimeError):
    """Another backend process acquired the machine-wide Qwen slot first."""


attempt_handle: ContextVar[_Handle | None] = ContextVar("run_attempt_handle", default=None)


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
        # 한도 대기 Run 여럿이 같은 초기화 시각에 한꺼번에 깨어나도 시작 단계는 풀 크기에 맞춰 줄 세운다.
        self._routing: asyncio.Semaphore | None = None
        self._pool_retries: dict[uuid.UUID, int] = {}

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

    @property
    def remote(self) -> bool:
        runtime = self.runtime
        return bool(runtime and runtime.role == "api" and runtime.execution_mode == "remote")

    async def is_owned(self, run_id: uuid.UUID, db: AsyncSession | None = None) -> bool:
        from ttakkari.models import RuntimeLease
        if db is not None:
            if await db.get(RuntimeLease, run_id) is not None:
                return True
        elif self.runtime is not None:
            async with sessionmaker()() as session:
                if await session.get(RuntimeLease, run_id) is not None:
                    return True
        return self.is_active(run_id)

    async def request_context_reset(self, session_id: uuid.UUID) -> None:
        if self.runtime:
            await self.runtime.command("reset", session_id=session_id)
            return
        self._reset_requests.add(session_id)

    def start(self, run_id: uuid.UUID, *, lane: str = "workers") -> bool:
        if self.remote:
            return False
        if run_id in self._handles or self._shutting_down:
            return False
        if self.runtime and getattr(self.runtime, "draining", False):
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
        if self.remote:
            await self.runtime.command("cancel", run_id=run_id)
            return True
        h = self._handles.get(run_id)
        if h is None:
            if self.runtime and await self.is_owned(run_id):
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
            await proc.wait()

    async def dispatch(self, session_id: uuid.UUID) -> uuid.UUID | None:
        """세션 대기열의 다음 Run 을 시작한다. 엔진 대화는 순서대로만 이어갈 수 있어 세션당 하나씩만 돌린다.

        세션 행을 FOR UPDATE 로 잠가, 지시 접수와 Run 종료가 동시에 들어와도 두 개가 함께 시작되지 않게 한다.
        """
        if self._shutting_down:
            return None
        if self.remote:
            async with sessionmaker()() as db:
                await generation.wake(db)
                await db.commit()
            return None
        if self.runtime and (getattr(self.runtime, "draining", False) or await self.runtime.refresh() != "active"):
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
            # 전역 중지로 취소된 작업의 보고를 표식을 모르는 구세대가 만들었다면 시작 전에 버린다.
            from ttakkari.runs.agent_control import drop_suppressed_reports
            rows = await drop_suppressed_reports(db, rows)
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
            if lease or self.remote:
                control = await db.get(generation.RuntimeControl, 1)
                db.add(RuntimeCommand(key=str(uuid.uuid4()), kind="cancel", run_id=run.id,
                                      generation_id=lease.generation_id if lease else generation.claim_owner(control)))
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
                              task_type: str | None, db: AsyncSession | None = None,
                              *, qwen_attempted: bool = False) -> tuple[profiles_mod.Profile | None, str]:
        """모든 개인 클라우드 계정이 한도일 때만 로컬 Qwen으로 이어받는다.

        (프로필, "ready") 또는 (None, 이유). readiness는 그때그때 확인하고
        허위로 통과시키지 않는다. engine_models 게이트를 준수한다(반례 3):
        운영자가 qwen-local 모델을 빼면 비상은 발동하지 않고 대기한다.
        자격 미달(ineligible)은 대기 사유에 Qwen 항목을 남기지 않는다(반례 5).
        슬롯 사용 중(qwen_busy)이면 비상도 타지 않고 클라우드 대기로 간다.
        한 Run에서 Qwen을 한 번 썼으면(qwen_attempted) 다시 타지 않는다.
        """
        from ttakkari.runs import worker_routing
        if not worker_routing.qwen_emergency_eligible(
                pool=sess.pool, task_type=task_type or sess.worker_task_type,
                strict_engine=sess.strict_engine, session_kind=sess.kind):
            return None, "ineligible"
        if qwen_attempted:
            return None, "qwen_already_attempted"
        if worker_routing.qwen_local_model() not in get_settings().engine_models.get("qwen-local", []):
            return None, "qwen_model_gated"
        adapter = await _qwen_adapter(db) if db is not None else QWEN_LOCAL_ADAPTER
        ready, reason = await asyncio.to_thread(adapter.readiness)
        if not ready:
            return None, reason
        if db is not None:
            from ttakkari.runs import qwen_slot
            if (await qwen_slot.slot_status(db))["busy"]:
                return None, "qwen_busy"
        return worker_routing.emergency_qwen_profile(sess.pool), "ready"

    async def _parallel_qwen(self, db: AsyncSession, sess: ChatSession, run: Run,
                             task_type: str) -> tuple[profiles_mod.Profile | None, dict]:
        """평시 병렬-fit 작업자를 Qwen에 붙인다. (프로필, 영수증 qwen 블록).

        적합 판정은 이름이 아니라 선언된 작업 속성(needs_mcp·narrow_scope·
        verifiable)과 병렬 문맥(살아 있는 형제 작업자 수)으로 한다. 기획 세션은
        판정에서 원천 제외되므로 평시 경로로 Qwen에 갈 수 없다(비상 경로만).
        feature gate·engine_models 게이트·readiness·1슬롯을 전부 통과해야
        선택하고, 하나라도 걸리면 클라우드로 가며 사유를 블록에 남긴다.
        """
        from ttakkari.runs import qwen_slot, worker_routing
        hints = (run.usage or {}).get("qwen_fit") or {}
        siblings = await qwen_slot.active_sibling_workers(db, sess)
        assessment = worker_routing.qwen_fit_assessment(
            pool=sess.pool, task_type=task_type, session_kind=sess.kind,
            session_engine=sess.engine, strict_engine=sess.strict_engine,
            parallel_siblings=siblings, needs_mcp=hints.get("needs_mcp"),
            narrow_scope=hints.get("narrow_scope"), verifiable=hints.get("verifiable"))
        block: dict[str, object] = {"fit": assessment["fit"], "reasons": list(assessment["reasons"]),
                                    "basis": assessment.get("basis"),
                                    "parallel_siblings": assessment["parallel_siblings"],
                                    "decision": "cloud", "trigger": None,
                                    "busy": None, "queue_length": None, "estimated_wait_s": None}
        if not get_settings().qwen_parallel_routing:
            block["reasons"].append("parallel_routing_disabled")
            return None, block
        if not assessment["fit"]:
            return None, block
        if worker_routing.qwen_local_model() not in get_settings().engine_models.get("qwen-local", []):
            block["reasons"].append("qwen_model_gated")
            return None, block
        adapter = await _qwen_adapter(db)
        ready, reason = await asyncio.to_thread(adapter.readiness)
        if not ready:
            block["reasons"].append(reason)
            return None, block
        slot = await qwen_slot.slot_status(db, exclude_run_id=run.id)
        block["busy"] = slot["busy"]
        block["queue_length"] = slot["queue_length"]
        if slot["busy"]:
            block["reasons"].append("qwen_busy")
            block["estimated_wait_s"] = await qwen_slot.estimate_wait_s(
                db, slot, ahead=slot["queue_length"])
            log.info("run %s parallel-qwen skipped (qwen_busy, siblings=%d, wait_s=%s)",
                     run.id, siblings, block["estimated_wait_s"])
            return None, block
        block["decision"] = "selected"
        block["trigger"] = worker_routing.QWEN_PARALLEL_TRIGGER
        if assessment.get("basis") == "default_routine_fit":
            block["reasons"] = ["default_routine_fit"]
        log.info("run %s parallel-qwen selected (siblings=%d, basis=%s)", run.id, siblings, assessment.get("basis"))
        return worker_routing.emergency_qwen_profile(sess.pool), block

    async def _queue_for_qwen_slot(self, db: AsyncSession, run: Run, sess: ChatSession, slot: dict) -> None:
        """명시 지정 Qwen Run을 슬롯 대기열에 둔다. 실패로 끝내지 않고 차례를 기다린다.

        Run은 queued로 남고, 슬롯이 비면 끝난 Qwen Run이 깨우며(_wake_qwen_waiters),
        그 전에라도 qwen_queue_retry_seconds마다 다시 두드린다.
        """
        from ttakkari.runs import qwen_slot, worker_routing
        position = slot["queue_length"] + 1
        wait_s = await qwen_slot.estimate_wait_s(db, slot, ahead=slot["queue_length"])
        task_type = sess.worker_task_type or "general"
        if run.status == RunStatus.running:
            # 발사 직전에 막히면 이미 running으로 찍혀 있다. queued로 되돌려야
            # 다음 차례에 dispatch가 다시 잡는다(그대로 두면 영원히 안 돈다).
            run.status = RunStatus.queued
            run.pid = None
            events.append(db, run, "run.status", {"status": RunStatus.queued, "waiting": True})
        events.append(db, run, "worker.routing", {
            "policy": worker_routing.POLICY_VERSION, "task_type": task_type, "trigger": "queued",
            "selected": None, "requested_engine": "qwen-local", "requested_model": QWEN_LOCAL_MODEL,
            "scope": {"account_scope": sess.pool, "runtime_scope": "local-machine",
                      "workspace_id": str(sess.workspace_id)},
            "quota": None, "readiness": "ready", "fallback": False,
            "qwen": {"fit": None, "reasons": ["qwen_busy"], "parallel_siblings": None,
                     "decision": "queued", "trigger": None, "busy": True,
                     "queue_length": slot["queue_length"], "queue_position": position,
                     "estimated_wait_s": wait_s, "current": slot["current"]},
        })
        events.append(db, run, "qwen.queued", {"position": position, "queue_length": slot["queue_length"],
                                              "estimated_wait_s": wait_s, "current": slot["current"]})
        until = time.time() + get_settings().qwen_queue_retry_seconds
        run.resume_after = datetime.fromtimestamp(until, UTC)
        await self._commit(db, run)
        self._arm(run.id, sess.id, until)
        log.info("run %s queued for qwen slot (position=%d, wait_s=%s)", run.id, position, wait_s)

    async def _wake_qwen_waiters(self) -> None:
        """끝난 Qwen Run이 슬롯 대기열의 다음 차례를 깨운다. 예약된 재시도보다 먼저 돈다."""
        async with sessionmaker()() as db:
            candidates = list(await db.scalars(
                select(Run).join(ChatSession, ChatSession.id == Run.session_id)
                .where(Run.status == RunStatus.queued, ChatSession.engine == "qwen-local",
                       ChatSession.kind == SessionKind.worker)
                .order_by(Run.created_at)))
            waiters = []
            for candidate in candidates:
                latest_event = await db.scalar(
                    select(RunEvent).where(RunEvent.run_id == candidate.id)
                    .order_by(RunEvent.seq.desc()).limit(1))
                if latest_event is not None and latest_event.type == "qwen.queued":
                    waiters.append(candidate)
                    if len(waiters) == 5:
                        break
            sessions = {w.session_id for w in waiters}
            for w in waiters:
                self._disarm(w.id)
                w.resume_after = None
            if waiters:
                await db.commit()
        for sid in sessions:
            await self.dispatch(sid)

    async def _replay_spawn_queue(self, db: AsyncSession, sess: ChatSession,
                                    run: Run, outbox: Path) -> None:
        """degraded Qwen 기획자가 남긴 위임 intent를 작업 세션으로 띄운다.

        큐: <outbox>/spawn-queue/*.json. 한 파일에 지시 하나. 검증 실패분은
        실행하지 않고 invalid/ 로 옮긴다(fail closed). 처리분은 done/ 으로
        옮겨 재시작·재개 때 중복 실행되지 않는다. 띄운 작업은 일반 경로로
        돌므로 클라우드 한도가 그대로 적용된다(당장 못 돌면 대기한다).
        """
        from ttakkari.runs import worker_routing
        qdir = outbox / worker_routing.QWEN_SPAWN_QUEUE_DIR
        files = sorted(qdir.glob("*.json")) if qdir.is_dir() else []
        if not files:
            return
        done = qdir / "done"
        invalid = qdir / "invalid"
        done.mkdir(parents=True, exist_ok=True)
        invalid.mkdir(parents=True, exist_ok=True)
        s = get_settings()
        created: list[str] = []
        rejected: list[dict[str, object]] = []
        processed = 0
        for path in files:
            if processed >= worker_routing.QWEN_SPAWN_QUEUE_MAX_PER_RUN:
                break
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                path.rename(invalid / path.name)
                rejected.append({"file": path.name, "reason": "unreadable"})
                continue
            intent, reason = worker_routing.parse_qwen_spawn_intent(data)
            if intent is None:
                path.rename(invalid / path.name)
                rejected.append({"file": path.name, "reason": reason})
                continue
            active = await db.scalar(
                select(func.count(func.distinct(ChatSession.id)))
                .join(Run, Run.session_id == ChatSession.id)
                .where(ChatSession.parent_id == sess.id,
                       Run.status.in_([RunStatus.queued, RunStatus.running])))
            if (active or 0) >= 20:  # api/orchestrator.MAX_ACTIVE_WORKERS 와 맞춘다.
                break
            engine = intent["engine"]
            task_type = intent["task_type"]
            default = s.worker_default_models.get(engine) or {}
            model = intent["model"] or (worker_routing.ROUTES[task_type][0][0][1]
                                        if engine == "auto" else default.get("model"))
            w = ChatSession(workspace_id=sess.workspace_id, title=intent["title"], engine=engine,
                            model=model, effort=intent["effort"] or default.get("effort"),
                            kind=SessionKind.worker, parent_id=sess.id, pool=sess.pool,
                            worker_task_type=task_type, strict_engine=intent["strict_engine"])
            db.add(w)
            await db.flush()
            child = Run(session_id=w.id, workspace_id=w.workspace_id, prompt=intent["prompt"],
                        origin=RunOrigin.orchestrator, engine=w.engine, model=w.model, effort=w.effort)
            db.add(child)
            await db.flush()
            events.append(db, child, "run.status", {"status": RunStatus.queued, "ahead": 0})
            record(db, actor="agent", action="orchestrator.replay_spawn_queue",
                   target_type="session", target_id=w.id,
                   detail={"parent_run": str(run.id), "file": path.name,
                           "engine": engine, "task_type": task_type})
            path.rename(done / path.name)
            created.append(str(w.id))
            processed += 1
        events.append(db, run, "orchestrator.spawn_replayed",
                      {"created": created, "rejected": rejected,
                       "pending": max(0, len(files) - processed - len(rejected))})
        await self._commit(db, run)
        for wid in created:
            await self.dispatch(uuid.UUID(wid))

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
            # 반례 5: 자격 미달(ineligible)인데도 qwen_local ready:false가 대기
            # 사유에 찍히지 않게 한다. 비상 자격이 있어 readiness·게이트에서
            # 막힌 경우만 정확한 사유와 함께 남긴다.
            if qwen_state is not None and qwen_state != "ineligible":
                waiting_payload["qwen_local"] = {"ready": False, "reason": qwen_state}
            events.append(db, run, "run.waiting", waiting_payload)
            run.resume_after = datetime.fromtimestamp(until, UTC)
            await self._commit(db, run)
            _spawn(_notify_waiting(run, sess.title, until))
        self._arm(run.id, sess.id, until)

    async def release_waiting(self) -> None:
        if self.remote:
            await self.runtime.command("release_waiting")
            return
        if self.runtime:
            from sqlalchemy import update
            async with sessionmaker()() as db:
                await db.execute(update(Run).where(Run.status == RunStatus.queued).values(resume_after=None))
                await generation.wake(db)
                await db.commit()
            # NOTE: the DB is authoritative here, but wakers armed from the old resume_after
            # live in this process. dispatch() treats an armed waker as "still waiting", so
            # without this a released Run stayed queued until its original wake time.
            for rid in list(self._waiting):
                self._disarm(rid)
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

    async def _release_attempt(self, run_id, runtime, token) -> None:
        handle = self._handles.get(run_id)
        if handle and handle.process_groups:
            for pid, started_at, pgid, owner_id, fence, attempt in tuple(handle.process_groups):
                if owner_id != runtime.id or fence != token:
                    log.error("retaining lease for run %s: process group fence differs", run_id)
                    return
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    handle.process_groups.remove((pid, started_at, pgid, owner_id, fence, attempt))
                    continue
                except OSError:
                    log.error("retaining lease for run %s: process group %s is unobservable", run_id, pgid)
                    return
                try:
                    process = psutil.Process(pid)
                    if abs(process.create_time() - started_at) > 1.0 or os.getpgid(pid) != pgid:
                        log.error("retaining lease for run %s: process group identity changed", run_id)
                        return
                except (psutil.Error, OSError, PermissionError):
                    log.error("retaining lease for run %s: process group leader identity is unknown", run_id)
                    return
                log.error("retaining lease for run %s: attempt process group %s remains", run_id, pgid)
                return
        if handle and handle.proc is not None and handle.proc.returncode is None:
            log.error("retaining lease for run %s: child exit is unconfirmed", run_id)
            return
        if handle and handle.proc is not None:
            try:
                os.killpg(handle.proc.pid, 0)
            except ProcessLookupError:
                pass
            except OSError:
                log.error("retaining lease for run %s: process group exit is unknown", run_id)
                return
            else:
                log.error("retaining lease for run %s: descendants remain in engine process group", run_id)
                return
        await runtime.release(run_id, token)

    async def _release_connection(self, db: AsyncSession, run: Run) -> None:
        """안쪽 연결(choose_profile 등)을 얻기 전에 바깥 세션의 트랜잭션을 끝내 풀 연결을 돌려준다.

        한도·슬롯 폴백의 재선택은 시작 슬롯 밖에서 일어난다. 바깥 세션이 available_profiles() 조회로 연결을 쥔 채
        안쪽 연결을 기다리면, 여러 Run 이 같은 순간 폴백할 때 풀이 말라 서로 기다린다."""
        await self._commit(db, run)

    @contextlib.asynccontextmanager
    async def _start_slot(self, run_id: uuid.UUID):
        """시작 단계 동시 상한. 슬롯을 기다리는 동안은 DB 연결을 쥐지 않는다.

        _attempt 가 엔진 프로세스를 띄우고 첫 커밋을 마치면 슬롯을 먼저 돌려준다. 그 뒤 긴 실행 구간은
        이벤트마다 커밋해 연결을 바로 반납하므로 상한을 쓰지 않는다."""
        if self._routing is None:
            self._routing = asyncio.Semaphore(routing_slots())
        slot = self._routing
        await slot.acquire()
        h = self._handles.get(run_id)
        if h is not None:
            h.start_slot = slot
        try:
            yield
        finally:
            if h is not None:
                h.release_start_slot()
            else:
                slot.release()

    async def _requeue_after_pool_timeout(self, run_id: uuid.UUID, *, authorized: bool) -> bool:
        """풀 고갈로 죽은 Run 을 실패시키지 않고 지터 있는 백오프 뒤로 다시 줄 세운다. 다시 세웠으면 True.

        엔진 프로세스가 이미 떴거나(h.proc) 별도 커밋으로 부수효과가 확정됐으면(h.side_effect_committed) 하지 않는다.
        그 경우는 기존 실패 처리로 간다. 다시 돌리면 같은 일을 두 번 하기 때문이다.
        authorized=False 는 claim 에서 죽어 이 Run 에 쓸 권한이 없다는 뜻이라 DB 는 건드리지 않고 깨우기만 건다."""
        attempt = self._pool_retries.get(run_id, 0) + 1
        h = self._handles.get(run_id)
        if attempt > MAX_POOL_TIMEOUT_RETRIES or (h is not None and (h.proc is not None or h.side_effect_committed)):
            return False
        delay = POOL_TIMEOUT_BACKOFF_SECONDS * attempt + random.uniform(0, POOL_TIMEOUT_BACKOFF_SECONDS)
        until = time.time() + delay
        try:
            async with sessionmaker()() as db:
                run = await db.get(Run, run_id)
                if run is None or run.status not in (RunStatus.queued, RunStatus.running):
                    return False
                if authorized:
                    if run.status == RunStatus.running:
                        run.status = RunStatus.queued
                        run.pid = None
                    run.resume_after = datetime.fromtimestamp(until, UTC)
                    events.append(db, run, "run.status", {
                        "status": RunStatus.queued, "pool_timeout_retry": True, "attempt": attempt,
                        "resume_after": _iso(until)})
                    await self._commit(db, run)
                session_id = run.session_id
        except Exception:  # 풀이 아직 비어 있지 않다. 호출자가 기존대로 실패 처리한다
            log.warning("could not requeue run %s after a DB pool timeout", run_id, exc_info=True)
            return False
        self._pool_retries[run_id] = attempt
        log.warning("run %s requeued after DB pool timeout (attempt %d, retry in %.0fs)", run_id, attempt, delay)
        self._arm(run_id, session_id, until)
        return True

    async def _execute(self, run_id: uuid.UUID, lane: str = "workers") -> None:
        sm = sessionmaker()
        runtime, token = self.runtime, None
        scope = attempt_handle.set(self._handles.get(run_id))
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
            async with self.sem(lane), self._start_slot(run_id), sm() as db:
                if runtime and token:
                    db.info["runtime_lease"] = (runtime, run_id, token)
                await self._run(db, run_id)
            self._pool_retries.pop(run_id, None)
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
            if isinstance(e, PoolTimeoutError) and await self._requeue_after_pool_timeout(
                    run_id, authorized=runtime is None or token is not None):
                return
            self._pool_retries.pop(run_id, None)
            if runtime is not None and token is None:
                return  # A failed claim did not authorize any writes to this Run.
            async with sm() as db:
                run = await db.get(Run, run_id)
                if run is not None:
                    await self._finish(db, run, RunStatus.failed, error=f"내부 오류: {e!r}")
        finally:
            attempt_handle.reset(scope)
            if runtime and token:
                await self._release_attempt(run_id, runtime, token)
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
        scope = attempt_handle.set(self._handles.get(run_id))
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
            self._pool_retries.pop(run_id, None)
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
            # 접수의 답·이벤트는 _finish 의 커밋으로 확정된다. 하지만 기획으로 넘기는 desk._delegate 는 queue_run 이
            # 별도로 커밋하고 dispatch 까지 하므로 그 뒤에는 side_effect_committed 로 막아 기획 Run 이 둘 생기지 않게 한다.
            if isinstance(e, PoolTimeoutError) and await self._requeue_after_pool_timeout(
                    run_id, authorized=runtime is None or token is not None):
                return
            self._pool_retries.pop(run_id, None)
            if runtime is not None and token is None:
                return
            async with sm() as db:
                run = await db.get(Run, run_id)
                if run is not None and run.status not in TERMINAL_RUN_STATUSES:
                    await self._finish(db, run, RunStatus.failed, error=f"내부 오류: {e!r}")
        finally:
            attempt_handle.reset(scope)
            if runtime and token:
                await self._release_attempt(run_id, runtime, token)
            self._handles.pop(run_id, None)
            profiles_mod.release(run_id)

    async def _run(self, db: AsyncSession, run_id: uuid.UUID) -> None:
        s = get_settings()
        run = await db.get(Run, run_id)
        if run is None or run.status != RunStatus.queued:
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

        # 평시 병렬-fit 결정은 클라우드 조회보다 먼저 한다. Qwen이 되면 클라우드를
        # 두드리지 않고 Qwen 단일 후보로 가며, 안 되면 사유를 영수증에 남기고
        # 기존 클라우드 경로를 그대로 돈다. 기획 세션은 평시 판정 대상이 아니다.
        qwen_block: dict | None = None
        parallel_initial = False
        routing_state: dict[str, object] = {"initial_trigger": None}
        parallel_profile = None
        if task_type and not is_meeting:
            parallel_profile, qwen_block = await self._parallel_qwen(db, sess, run, task_type)
        # 기획 세션은 평시 판정 대상이 아니라서 블록을 만들지 않는다. 기획의 Qwen행은
        # 비상 경로뿐이며, 그 영수증은 아래에서 선택-selected 블록으로 찍힌다.
        if parallel_profile is not None:
            profiles, blocked = [parallel_profile], []
            parallel_initial = True
            events.append(db, run, "worker.routing", {
                "policy": worker_routing.POLICY_VERSION,
                "task_type": effective_task_type or "general",
                "trigger": worker_routing.QWEN_PARALLEL_TRIGGER,
                "mode": worker_routing.QWEN_PARALLEL_MODE,
                "selected": None,
                "reason": qwen_block.get("basis") or "task_fit_then_known_quota_then_stage_then_headroom_per_run",
                "scope": {"account_scope": sess.pool, "runtime_scope": "local-machine",
                          "workspace_id": str(sess.workspace_id)},
                "quota": None, "readiness": "ready",
                "qwen": qwen_block,
            })
        else:
            profiles, blocked = await available_profiles()
        if not profiles:
            if blocked:
                emergency, qwen_state = await self._emergency_qwen(sess, effective_task_type, db)
                if emergency is None:
                    await self._wait_for_reset(db, run, sess, blocked, qwen_state=qwen_state)
                    return
                profiles = [emergency]
                events.append(db, run, "worker.routing", {
                    "policy": worker_routing.POLICY_VERSION,
                    "task_type": effective_task_type or "general", "trigger": "emergency",
                    "mode": "orchestrator-degraded" if is_orch else "worker-handoff",
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
            # 반례 1: 비상 프로필을 잡았으면(profiles 비어 있지 않음) 아래 실패
            # 분기로 떨어지지 않는다. 작업·기획(degraded) 모두 같은 분기를 돈다.
            if not profiles:
                if sess.engine == "qwen-local":
                    qwen_adapter = await _qwen_adapter(db)
                    ready, reason = await asyncio.to_thread(qwen_adapter.readiness)
                    if ready and sess.kind == SessionKind.worker:
                        # 준비됐는데 후보가 없으면 슬롯 사용 중이다. 실패가 아니라 대기열이다.
                        # 작업 세션만 대기열에 둔다(명시 지정도 worker 전용).
                        from ttakkari.runs import qwen_slot
                        slot = await qwen_slot.slot_status(db, exclude_run_id=run.id)
                        if slot["busy"]:
                            await self._queue_for_qwen_slot(db, run, sess, slot)
                            return
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
            if run.status == RunStatus.running:  # 첫 선택은 시작 슬롯이 보호한다. 폴백 재선택만 여기서 연결을 푼다.
                await self._release_connection(db, run)
            if is_meeting:
                candidates = meeting_routing.first_stage(candidates)
            elif task_type:
                all_candidates = candidates
                # Muse 탐색은 단계 선택에 들어간 뒤 DB 에서 예약한다. 져서 빠지면 그 후보 없이 단계를 다시 고른다.
                maybe = frozenset(p.id for p in candidates if profiles_mod.muse_probe_eligible(p))
                # 비상 단일 경로: 후보가 전부 Qwen이면 ROUTES 단계 선택을 거치지
                # 않는다(Qwen은 auto 단계가 없다, 반례 2). maybe는 muse만 해당이라
                # 전부-Qwen이면 비어 있어 아래 probe 분기도 돌지 않는다.
                if not all(p.engine == "qwen-local" for p in candidates):
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
                # 비상 Qwen 선택은 명시 지정(sess.engine == "qwen-local")이 아닐 때만
                # 일어나며, 그때 영수증 trigger는 "emergency"다(반례 2). 명시 지정
                # Qwen의 첫 선택은 그대로 "initial"이다. 평시 병렬-fit 선택은
                # 첫 선택에서만 "parallel"이고, 실행 중 한도 소진으로 타는
                # Qwen은 "emergency"다.
                if selected.engine == "qwen-local" and sess.engine != "qwen-local":
                    trigger = worker_routing.QWEN_PARALLEL_TRIGGER if parallel_initial else "emergency"
                else:
                    trigger = "initial" if not run.started_at else "fallback_or_resume"
                receipt_qwen = qwen_block
                if selected.engine == "qwen-local" and (
                        receipt_qwen is None or receipt_qwen.get("decision") != "selected"):
                    # 명시 지정·비상 기획처럼 평시 블록이 없는 선택은 여기서 찍는다.
                    receipt_qwen = {"fit": None, "reasons": [], "parallel_siblings": None,
                                    "decision": "selected", "trigger": trigger,
                                    "explicit": sess.engine == "qwen-local",
                                    "busy": None, "queue_length": None, "estimated_wait_s": None}
                receipt = worker_routing.receipt(
                    effective_task_type or "general", selected, all_candidates, currently_blocked, counts,
                    trigger=trigger,
                    fallback_from={"engine": primary_pair[0], "model": primary_pair[1]} if is_pinned_switched and primary_pair else None,
                    qwen=receipt_qwen,
                )
                if selected.engine == "qwen-local":
                    if parallel_initial and receipt_qwen and receipt_qwen.get("basis") == "default_routine_fit":
                        receipt["reason"] = "default_routine_fit"
                    receipt["scope"] = {"account_scope": sess.pool, "runtime_scope": "local-machine",
                                        "workspace_id": str(sess.workspace_id)}
                    receipt["quota"] = None
                    receipt["readiness"] = "passed"
                    receipt["mode"] = ("orchestrator-degraded" if is_orch
                                       else worker_routing.QWEN_PARALLEL_MODE if trigger == worker_routing.QWEN_PARALLEL_TRIGGER
                                       else "worker-handoff")
                else:
                    receipt["scope"] = {"account_scope": sess.pool, "runtime_scope": "provider-account",
                                        "workspace_id": str(sess.workspace_id)}
                events.append(db, run, "worker.routing", receipt)
                if routing_state["initial_trigger"] is None:
                    routing_state["initial_trigger"] = trigger
            return selected

        prev_engine: str | None = None

        def conversation_key(profile):
            if is_meeting:
                return meeting_routing.conversation_key(profile)
            if task_type or profile.engine in {"antigravity", "muse", "opencode", "copilot"} or (pinned_fallback and profile.engine != sess.engine):
                return worker_routing.conversation_key(profile)
            return profile.key

        def lookup_resume_id(profile):
            es = sess.engine_sessions or {}
            if profile.engine == "qwen-local":
                # Engine switch: never reuse a provider conversation id as a Qwen
                # resume id. Progress travels as handoff text instead.
                return qwen_resume_id(es, conversation_key(profile), prev_engine,
                                      legacy_key=profile.key if profile.engine == sess.engine else None)
            if saved := es.get(conversation_key(profile)):
                return saved
            # Legacy account-only IDs identify the original model. They cannot be
            # reused for another model sharing the same provider account.
            if profile.engine == sess.engine and (
                profile.engine not in {"antigravity", "muse", "opencode", "copilot"}
                or profile.model == worker_routing.primary_target(sess.engine, requested_model)[1]
            ):
                return es.get(profile.key)
            return None

        prof = await choose(remaining, run.id, idle_first=not is_orch, prefer_engine=prefer, balance_usage=True)
        # 첫 선택이 끝났다. 이후 선택의 Qwen trigger는 평시가 아니라 비상이다.
        parallel_initial = False

        # 한도 대기 뒤 이어 받는 Run 은 이미 시작했던 Run 이다. 시작 시각·체크포인트는 처음 것을 지키고 인계 내용을 넘긴다.
        resumed = run.started_at is not None
        routing_request = {"model": requested_model, "engine": sess.engine}
        if sess.kind == SessionKind.worker:
            routing_request.update({"task_type": sess.worker_task_type or effective_task_type or "general",
                                    "account_scope": sess.pool})
        run.usage = {**(run.usage or {}), "routing_request": routing_request}
        resume_handoff = run.resume_handoff or (await _waiting_handoff(db, run) if resumed else "")
        # Profile selection may await other work. A queued cancellation committed
        # in that interval must win before this attempt changes the status.
        with db.no_autoflush:
            await db.refresh(run, attribute_names=["status"], with_for_update=True)
        if run.status != RunStatus.queued:
            return
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
        write_roots = write_roots_for_session(
            sess, root=root, outbox=outbox, settings=s,
            extra_writable_roots=ws.extra_writable_roots, tmp_dirs=_tmp_dirs(),
        )

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
        qwen_attempted = False
        qwen_cloud_handoff = False
        attempts_started = 0
        while True:
            remaining.remove(prof)
            if prof.engine == "qwen-local":
                # 발사 직전 슬롯 재확인(결정 시점과 발사 사이 다른 Run이 잡았을 수 있다).
                from ttakkari.runs import qwen_slot
                preslot = await qwen_slot.slot_status(db, exclude_run_id=run.id)
                if preslot["busy"]:
                    events.append(db, run, "qwen.slot_busy", {
                        "current": preslot["current"],
                        "queue_length": preslot["queue_length"],
                        "attempts_started": attempts_started})
                    log.info("run %s qwen slot busy at launch (attempts=%d)", run.id, attempts_started)
                    if sess.engine == "qwen-local":
                        await self._queue_for_qwen_slot(db, run, sess, preslot)
                        return
                    free_now, blocked_now = await available_profiles()
                    cloud_free = [p for p in free_now if p.engine != "qwen-local"]
                    if qwen_block is not None:
                        # 앞에서 찍은 intent 이벤트의 블록은 손대지 않고 새로 만든다.
                        reasons = list(qwen_block.get("reasons") or [])
                        if "qwen_busy" not in reasons:
                            reasons.append("qwen_busy")
                        qwen_block = {**qwen_block, "decision": "cloud", "trigger": None,
                                      "busy": True, "queue_length": preslot["queue_length"],
                                      "reasons": reasons,
                                      "estimated_wait_s": await qwen_slot.estimate_wait_s(
                                          db, preslot, ahead=preslot["queue_length"])}
                    if attempts_started == 0 and cloud_free:
                        prof = await choose(cloud_free, run.id, prefer_engine=prefer, balance_usage=True)
                        remaining = list(cloud_free)
                        continue
                    if blocked_now:
                        await self._wait_for_reset(db, run, sess, blocked_now,
                                                   handoff=attempt_handoff or None, qwen_state="qwen_busy")
                        return
                    what = "엔진" if sess.engine == "auto" else sess.engine
                    where = "회사" if sess.pool == "work" else "개인"
                    await self._finish(db, run, RunStatus.failed,
                                       error=f"켜진 {where} {what} 계정이 없습니다. 설정 > 계정의 {where} 계정에서 켜 주세요.")
                    return
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
            if prof.engine == "qwen-local" and is_orch:
                # MCP 도구 없는 degraded 기획: 위임 대신 직접 계획·좁은 작업 +
                # spawn-queue intent. 초기 선택·실행 중 전환 모두 같은 루프를 돈다.
                prompt = (worker_routing.qwen_orchestrator_degraded_note(
                    str(outbox / worker_routing.QWEN_SPAWN_QUEUE_DIR)) + "\n" + prompt)
            mcp_servers = {} if prof.engine == "qwen-local" else {
                # SEC2-AGT-02: 에이전트 토큰을 이 Run 에 묶는다. Run 종료 후에는 즉시 거부된다.
                MCP_SERVER_NAME: mcp_server_spec(sess.id, "orchestrator" if is_orch else "agent", run_id=run.id),
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
            if prof.engine == "muse":
                # 세션·Run 에 저장된 값이 다르거나 어느 경로로 왔든 Muse 는 max 로만 띄운다(저장값도 맞춘다).
                effort = run.effort = MUSE_EFFORT
            # 반례 7: Qwen 작업 파일에는 클라우드용 시스템 노트(5.6KB) 대신 짧은
            # 계약만 전달한다. 작업 지시(prompt)는 그대로 둔다.
            note_for_spec = (QWEN_LOCAL_ADAPTER.build_short_system_note(
                outbox=str(outbox), write_roots=[str(p) for p in write_roots],
                kind=str(sess.kind))
                if prof.engine == "qwen-local" else system_note)
            is_game = bool((run.usage or {}).get("game"))  # 게임 worker 는 저장소 밖 기록을 쓰지 않는다(ADR 0058)
            spec = LaunchSpec(
                cwd=root, prompt=prompt, engine_session_id=resume_id,
                model=model, effort=effort,
                outbox=outbox, write_roots=write_roots, system_note=note_for_spec,
                codex_sandbox=ws.codex_sandbox, mcp_servers=mcp_servers, session_id=str(sess.id),
                setting_sources=s.game_worker_claude_setting_sources if is_game else None,
            )
            adapter = await _qwen_adapter(db) if prof.engine == "qwen-local" else ADAPTERS[prof.engine]
            env = adapter.child_env() if prof.engine == "qwen-local" else (
                await asyncio.to_thread(prof.env, _child_env(
                    run.id, write_roots, s.home, s.guard_log,
                    extra_protected=[Path(p).expanduser() for p in s.game_worker_protected_paths] if is_game else ()))
            )
            await self._commit(db, run)
            attempt_start_seq = run.last_seq
            if prof.engine == "qwen-local":
                qwen_attempted = True
            try:
                state, rc, timed_out, missing = await self._attempt(db, run, adapter, spec, env, root, h, prof.id)
            except QwenSlotBusy:
                qwen_attempted = False
                from ttakkari.runs import qwen_slot
                slot = await qwen_slot.slot_status(db, exclude_run_id=run.id)
                slot["busy"] = True
                if sess.engine == "qwen-local" and sess.kind == SessionKind.worker:
                    await self._queue_for_qwen_slot(db, run, sess, slot)
                    return
                events.append(db, run, "qwen.slot_busy", {
                    "current": slot["current"], "queue_length": slot["queue_length"],
                    "reason": "process_lock_contended", "attempts_started": attempts_started})
                free_now, blocked_now = await available_profiles()
                cloud_free = [p for p in free_now if p.engine != "qwen-local"]
                if attempts_started == 0 and cloud_free:
                    if qwen_block is not None:
                        reasons = list(qwen_block.get("reasons") or [])
                        if "qwen_busy" not in reasons:
                            reasons.append("qwen_busy")
                        qwen_block = {**qwen_block, "decision": "cloud", "trigger": None,
                                      "busy": True, "queue_length": slot["queue_length"],
                                      "reasons": reasons,
                                      "estimated_wait_s": await qwen_slot.estimate_wait_s(
                                          db, slot, ahead=slot["queue_length"])}
                    prof = await choose(cloud_free, run.id, prefer_engine=prefer, balance_usage=True)
                    remaining = list(cloud_free)
                    continue
                if blocked_now:
                    await self._wait_for_reset(db, run, sess, blocked_now,
                                               handoff=attempt_handoff or None, qwen_state="qwen_busy")
                else:
                    await self._finish(db, run, RunStatus.failed, error="Qwen 로컬 슬롯을 확보하지 못했습니다.")
                return
            attempts_started += 1
            if prof.engine == "qwen-local" and is_orch:
                await self._replay_spawn_queue(db, sess, run, outbox)
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
            if prof.engine == "opencode" and prof.config_dir and not stopped:
                from ttakkari import opencode_accounts as managed_oc
                with contextlib.suppress(ValueError, OSError):
                    await asyncio.to_thread(
                        managed_oc.record_run, prof.config_dir, success=not failed,
                        error=" ".join(t for t in texts if t)[:2000] if failed else None)
            if prof.engine == "copilot" and not stopped:
                from ttakkari import copilot_accounts as managed_cp
                with contextlib.suppress(ValueError, OSError):
                    await asyncio.to_thread(
                        managed_cp.record_run, prof.config_dir, success=not failed,
                        error=" ".join(t for t in texts if t)[:2000] if failed else None,
                        is_system=prof.config_dir is None)
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
            auth_failed = (failed and not stopped and profiles_mod.is_auth_error(*texts))
            if auth_failed:
                # 인증 만료/실패한 계정은 재시도 대상에서 즉시 제외하고 다음 계정으로 폴백
                remaining = [p for p in remaining if p.id != prof.id]
                events.append(db, run, "account.auth_failed", {
                    "account": prof.name, "engine": prof.engine, "model": model,
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
                # 한 Run에서 Qwen을 썼으면 다시 타지 않는다(무한 재시도 방지).
                emergency, qwen_state = await self._emergency_qwen(
                    sess, effective_task_type, db, qwen_attempted=qwen_attempted)
                if emergency is not None:
                    remaining = [emergency]
            if (not stopped and failed and prof.engine == "qwen-local"
                    and sess.engine != "qwen-local"
                    and not (missing or invalid_model)):
                # 반례 5: 비상 Qwen 자체가 실패하면 바로 failed로 끝내지 않는다.
                # 회복된 클라우드가 있으면 그리로 이어서 돌고, 전 클라우드 쿨다운이면
                # 아래 break 뒤 클라우드 초기화 대기로 돌아간다. 명시 지정 Qwen
                # (sess.engine == "qwen-local")은 기존대로 클라우드로 넘어가지 않는다.
                free_now, blocked_now = await available_profiles()
                cloud_free = [p for p in free_now if p.engine != "qwen-local"]
                if cloud_free:
                    nxt = await choose(cloud_free, run.id, prefer_engine=prefer, balance_usage=True)
                    qwen_cloud_handoff = True
                    last_fallback = {
                        "from": prof.name, "from_engine": "qwen-local", "from_model": model,
                        "to": nxt.name, "to_engine": nxt.engine, "to_model": nxt.model,
                        "reason": "qwen_failed", "fallback_tier": None,
                        "detail": (state.error or tail or "")[:500], "reset_at": None,
                        "mode": "resume" if lookup_resume_id(nxt) else "handoff",
                    }
                    events.append(db, run, "engine.fallback", last_fallback)
                    attempt_handoff = await _attempt_handoff(db, run, prof.name, state, tail, attempt_start_seq)
                    if h:
                        h.stderr_tail.clear()
                    prev_engine = prof.engine
                    prof = nxt
                    remaining = list(cloud_free)
                    continue
                if blocked_now:
                    paused_handoff = await _attempt_handoff(db, run, prof.name, state, tail, 0, waiting=True)
                    # 대기 사유에는 Qwen 실패를 정확히 남긴다(선택 당시 "ready"가 아님).
                    qwen_state = "qwen_attempt_failed"
                    break
            if stopped or not failed or not (missing or limited or invalid_model or auth_failed) or not remaining:
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
                "reason": "missing" if missing else "invalid_model" if invalid_model else "auth_failed" if auth_failed else "usage_limit",
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
            "routing_trigger": routing_state["initial_trigger"],
            "selected_engine": prof.engine,
            "selected_model": selected_model,
            "qwen_attempted": qwen_attempted,
            "qwen_cloud_handoff": qwen_cloud_handoff,
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
        from ttakkari.runs.test_database import RunTestDatabase
        test_db = None
        if os.environ.get("TTAKKARI_TEST_RUNNER") != "1":
            test_db = RunTestDatabase(str(run.id), root)
            start_task = asyncio.create_task(asyncio.to_thread(test_db.start))
            try:
                await asyncio.shield(start_task)
                # TTAKKARI_* is scrubbed from ambient settings before engine launch;
                # this explicit per-attempt URL points only at the owned cluster.
                env.update(test_db.child_env())
            except BaseException:
                with contextlib.suppress(BaseException):
                    await start_task
                await asyncio.shield(asyncio.to_thread(test_db.close))
                raise
        if adapter.engine == "antigravity" or getattr(adapter, "validates_resume_identity", False):
            state.engine_session_id = spec.engine_session_id
        qwen_slot = adapter.engine == "qwen-local"
        qwen_slot_acquired = False
        process_slot_fd: int | None = None

        def release_qwen_slot() -> None:
            nonlocal qwen_slot_acquired, process_slot_fd
            if not qwen_slot_acquired:
                return
            if process_slot_fd is not None:
                from ttakkari.runs import qwen_slot as qwen_slot_mod
                qwen_slot_mod.release_process_lock(process_slot_fd)
                process_slot_fd = None
            self._qwen_local_slot.release()
            qwen_slot_acquired = False

        try:
            if qwen_slot:
                await self._qwen_local_slot.acquire()
                qwen_slot_acquired = True
                from ttakkari.runs import qwen_slot as qwen_slot_mod
                process_slot_fd = qwen_slot_mod.acquire_process_lock()
                if process_slot_fd is None:
                    raise QwenSlotBusy("machine-wide Qwen slot is held by another process")
            cmd = seatbelt.maybe_wrap(
                adapter.engine, adapter.build_command(spec, env), enabled_engines=s.engine_seatbelt,
                home=Path.home(), ttakkari_home=s.home, outbox=spec.outbox, write_roots=spec.write_roots,
                env=env, deny_keychain=s.engine_seatbelt_deny_keychain, workspace=spec.cwd,
            )
            working_directory = getattr(adapter, "working_directory", lambda _spec: root)(spec)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(working_directory), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=32 * 1024 * 1024,
                pass_fds=(process_slot_fd,) if process_slot_fd is not None else (),
            )
        except FileNotFoundError:
            cleanup = getattr(adapter, "cleanup", None)
            try:
                if cleanup:
                    cleanup(spec)
            finally:
                release_qwen_slot()
                if test_db is not None:
                    await asyncio.shield(asyncio.to_thread(test_db.close))
            return state, None, False, True
        except OSError:
            cleanup = getattr(adapter, "cleanup", None)
            try:
                if cleanup:
                    cleanup(spec)
            finally:
                release_qwen_slot()
                if test_db is not None:
                    await asyncio.shield(asyncio.to_thread(test_db.close))
            raise
        except BaseException:
            cleanup = getattr(adapter, "cleanup", None)
            try:
                if cleanup:
                    cleanup(spec)
            finally:
                release_qwen_slot()
                if test_db is not None:
                    await asyncio.shield(asyncio.to_thread(test_db.close))
            raise
        if h:
            h.proc = proc
            if h.owner is not None and h.token is not None:
                try:
                    started_at = psutil.Process(proc.pid).create_time()
                    pgid = os.getpgid(proc.pid)
                except (psutil.Error, OSError, PermissionError):
                    await self._terminate(proc)
                    await proc.wait()
                    raise RuntimeError("could not establish child process group identity")
                attempt = len(h.process_groups) + 1
                await h.owner.record_process_group(run.id, h.token, attempt=attempt, pid=proc.pid,
                                                   process_started_at=started_at, pgid=pgid)
                h.process_groups.append((proc.pid, started_at, pgid, h.owner.id, h.token, attempt))
        run.pid = proc.pid
        stderr_task = asyncio.create_task(_drain_stderr(proc, h))
        heartbeat_task: asyncio.Task | None = None
        timed_out = False
        try:
            await self._commit(db, run)
            if h:
                h.release_start_slot()
            heartbeat_task = asyncio.create_task(
                _heartbeat(run.id, owner=h.owner if h else None, token=h.token if h else None),
                name=f"heartbeat-{run.id}")
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
                    release_qwen_slot()
                    if test_db is not None:
                        await asyncio.shield(asyncio.to_thread(test_db.close))
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
        # share 와의 조회-후-생성 경합을 막으려고 세션 행을 잠근다. 같은 세션의 수집·공유는 직렬화된다.
        await db.execute(select(ChatSession.id).where(ChatSession.id == run.session_id).with_for_update())
        root = Path(ws.root_path).resolve()
        # outbox 자동 수집은 전부 에이전트 파일이다. 에이전트가 outbox 를 작업 공간처럼 써서
        # 로그·증거 JSON·브라우저 캡처 같은 중간 산출물을 함께 두기 때문이다.
        # 사용자에게 전달하려고 share_file 로 공유한 파일도 AI 작업 파일로 유지되며,
        # 대화에서 사용자가 명시적으로 보관을 요청한 파일만 내 보관함(files)에 보관된다.
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
            if source == "agent_output":
                # 실행 중 share_file 로 이미 올린 파일은 여기서 중복 등록하지 않는다(같은 파일이 두 탭에 쌓이지 않게).
                # share 는 호출한 Run 이 아닌 행에 붙을 수 있으므로 Run 이 아니라 (세션, 원본 경로)로 판별한다.
                # 다른 세션이 같은 경로를 등록한 행은 그 세션의 보관함 항목이라 이 세션의 수집을 막지 않는다.
                dup = await db.scalar(select(Artifact.id).where(
                    Artifact.deleted_at.is_(None), Artifact.session_id == run.session_id,
                    Artifact.original_path == str(resolved.path)).limit(1))
                if dup is not None:
                    continue
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
        h = self._handles.get(run.id)
        if h and h.owner and h.token:
            db.info["runtime_lease"] = (h.owner, run.id, h.token)
            await h.owner.fence(db, run.id, h.token)
        self._disarm(run.id)
        self._pool_retries.pop(run.id, None)
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
        from ttakkari.orchestrator import work
        if sess is not None:
            await work.after_run(db, sess, run)
        parent_id = await _enqueue_report(db, sess, run) if sess is not None else None
        if sess is not None:
            await _post_lead_report(db, sess, run)
        await self._commit(db, run)
        if run.engine == "qwen-local" or (run.usage or {}).get("qwen_attempted"):
            # Qwen 슬롯이 비었다(클라우드로 인계돼 끝난 Run 포함). 명시 지정
            # 대기열을 예약 재시도보다 먼저 깨운다.
            _spawn(self._wake_qwen_waiters())
        if reverse_next is not None:
            _spawn(self.dispatch(reverse_next))
        if parent_id is not None:
            _spawn(self.dispatch(parent_id))
        # 푸시 서버가 느려도 Run 슬롯을 붙잡지 않게 따로 띄운다.
        _spawn(_notify(run, sess.title if sess else None))
        if (run.usage or {}).get("game"):  # 자동 빌드 여부와 무관하게 종료 시점의 잠금 상태를 요청 기록에 남긴다
            from ttakkari.games import autobuild
            _spawn(autobuild.after_run(run.id))

    async def _commit(self, db: AsyncSession, run: Run) -> None:
        h = self._handles.get(run.id)
        if h and h.owner and h.token:
            await h.owner.fence(db, run.id, h.token)
            await generation.wake(db, run.id)
        run.heartbeat_at = utcnow()
        await db.commit()
        events.notify(run.id)


_background: set[asyncio.Task] = set()


def qwen_resume_id(engine_sessions: dict | None, key: str, prev_engine: str | None,
                   legacy_key: str | None = None) -> str | None:
    """Qwen 대화 이어쓰기 판정. QwenLocalAdapter.should_resume을 호출한다(반례 5: 죽은 코드 연결).

    엔진 전환이면 provider 대화 id를 Qwen resume id로 재사용하지 않고 None을
    돌려주며, 호출자는 진행 상황을 handoff 텍스트로 실어야 한다. 같은 Qwen
    안의 연속이면 저장된 대화 id를, 명시 지정 세션의 옛 계정 키는 legacy로 잇는다.
    key는 호출자의 conversation_key(profile) 그대로 받는다(명시 지정 Qwen은
    계정 키를 쓰므로 worker_routing 키로 바꾸지 않는다).
    """
    saved = (engine_sessions or {}).get(key)
    if saved:
        return saved if QwenLocalAdapter.should_resume(saved, prev_engine) else None
    if legacy_key:
        legacy = (engine_sessions or {}).get(legacy_key)
        if legacy and not QwenLocalAdapter.should_resume(legacy, prev_engine):
            return None
        return legacy
    return None


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


async def _heartbeat(run_id: uuid.UUID, *, owner=None, token=None) -> None:
    """Persist liveness independently of agent output, without contending on RunEvent.seq."""
    while True:
        await asyncio.sleep(RUN_HEARTBEAT_SECONDS)
        async with sessionmaker()() as db:
            if owner and token:
                db.info["runtime_lease"] = (owner, run_id, token)
                await owner.fence(db, run_id, token)
            run = await db.get(Run, run_id)
            if run is None or run.status != RunStatus.running:
                return
            run.heartbeat_at = utcnow()
            await db.commit()


manager = RunManager()


def _inspect_run_process(pid: int) -> str:
    """프로세스 상태 분류: 'alive' (존재), 'absent' (확실히 없음), 'unknown' (권한/OS 오류)."""
    try:
        p = psutil.Process(pid)
        if p.is_running():
            return "alive"
        return "absent"
    except psutil.NoSuchProcess:
        return "absent"
    except (psutil.AccessDenied, OSError, PermissionError):
        return "unknown"


async def recover_interrupted() -> int:
    """서버가 죽었다 살아나면 실행 중이던 Run 은 프로세스를 잃은 것이다. 상태를 정리해 사용자가 이어서 지시할 수 있게 한다.

    FND-01:
    - ALIVE: 실행 중인 프로세스 보존 (건드리지 않고 스킵)
    - UNKNOWN: AccessDenied/OSError 로 판정 불명인 경우 보존 (건드리지 않고 스킵)
    - ABSENT: NoSuchProcess 로 확실히 종료된 Run 만 interrupted 로 마감
    """
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
        recovered_count = 0
        for run in rows:
            if run.pid:
                try:
                    state = _inspect_run_process(run.pid)
                except psutil.NoSuchProcess:
                    state = "absent"
                except (psutil.AccessDenied, OSError, PermissionError):
                    state = "unknown"
                except Exception:  # noqa: BLE001
                    state = "alive"

                if state in ("alive", "unknown"):
                    continue

                if _looks_like_agent(run.pid):
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
            recovered_count += 1
        await db.commit()
        return recovered_count


async def _consume_pending_cancel(db: AsyncSession, run_id: uuid.UUID) -> bool:
    """전역 중지가 남긴 미집행 cancel 명령이 있으면 회수하고 True. 죽은 세대를 향한 명령이라 여기서 마감한다."""
    from ttakkari.models import RuntimeCommand
    cmd = await db.scalar(
        select(RuntimeCommand).where(RuntimeCommand.run_id == run_id, RuntimeCommand.kind == "cancel",
                                     RuntimeCommand.acknowledged_at.is_(None)).limit(1)
    )
    if cmd is None:
        return False
    cmd.acknowledged_at = utcnow()
    return True


async def resume_interrupted(force: bool = False) -> int:
    """Queue only server-shutdown interruptions, with persisted bounded exponential backoff.

    FND-01 / ACC-03: 소유권 미확인 작업의 무조건 자동 재큐잉을 차단하여
    중복 실행 및 중복 부수효과를 방지한다. auto_requeued_count 는 기본 0이다.
    """
    async with sessionmaker()() as db:
        rows = list(await db.scalars(
            select(Run).where(Run.status == RunStatus.interrupted, Run.error == "서버 종료로 중단",
                              Run.resume_attempts < MAX_RESTART_RESUME_ATTEMPTS)
            .order_by(Run.created_at).with_for_update(skip_locked=True)
        ))
        waiting: list[tuple[uuid.UUID, uuid.UUID, float]] = []
        now = time.time()
        for run in rows:
            if await _consume_pending_cancel(db, run.id):
                # 소유 세대 집행 전에 세대가 죽어 interrupted 로 남은 Run. 재개하지 않고 취소로 마감한다.
                await manager._finish(db, run, RunStatus.cancelled, error="전역 중지 요청 후 세대 종료로 취소 마감")
                continue
            if not force and not get_settings().auto_resume_interrupted:
                continue
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


async def _qwen_adapter(db: AsyncSession) -> QwenLocalAdapter:
    from ttakkari.runs import qwen_activation
    return await qwen_activation.adapter_for(db, QWEN_LOCAL_ADAPTER)


async def _available_profiles(db: AsyncSession, sess: ChatSession, run: Run, is_meeting: bool):
    from ttakkari.meetings import routing as meeting_routing
    from ttakkari.runs import worker_routing
    task, pinned, requested = _routing_policy(sess, run, is_meeting)
    if is_meeting:
        return await meeting_routing.availability(db)
    if sess.engine == "qwen-local":
        from ttakkari.runs import worker_routing as _wr
        # Qwen은 personal scope 전용이다(반례 4: 회사 pool 누설 금지). 명시
        # 지정도 engine_models 게이트를 따른다(반례 3).
        if sess.pool != "personal":
            return [], []
        if _wr.qwen_local_model() not in get_settings().engine_models.get("qwen-local", []):
            return [], []
        qwen_adapter = await _qwen_adapter(db)
        ready, _reason = await asyncio.to_thread(qwen_adapter.readiness)
        if not ready or sess.kind != SessionKind.worker:
            return [], []
        # 전역 1슬롯: 다른 Run이 Qwen을 쓰고 있으면 후보를 내지 않는다.
        # _run의 명시 지정 분기가 이를 대기열로 둔다(실패가 아니다).
        from ttakkari.runs import qwen_slot
        if (await qwen_slot.slot_status(db))["busy"]:
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
    # 오케스트레이터(메인·기획) auto 는 Muse 계정도 폴백 후보로 본다. 네이티브가 모두 한도면 Muse 로 이어서 돈다.
    return await profiles_mod.availability(db, sess.engine, sess.pool, model=requested,
                                           include_muse=(sess.kind == SessionKind.orchestrator))


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
            if generation.claim_owner(control) != manager.runtime.id:
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
    return any(name in out for name in ("claude", "codex", "/agy ", "antigravity", "/muse ", "muse-runtime/bridge.mjs", "opencode", "copilot"))

def _tmp_dirs() -> list[Path]:
    dirs = {Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}
    return sorted(dirs)


def _child_env(run_id: uuid.UUID, write_roots: list[Path], home: Path, guard_log: Path,
               extra_protected: Iterable[Path] = ()) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(STRIP_ENV_PREFIXES) and k not in STRIP_ENV_KEYS}
    env["TTAKKARI_RUN_ID"] = str(run_id)
    env["TTAKKARI_GUARD_WRITE_ROOTS"] = os.pathsep.join(str(p) for p in write_roots)
    env["TTAKKARI_GUARD_PROTECTED"] = os.pathsep.join(str(p) for p in (home, *extra_protected))
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
    from ttakkari.orchestrator import work
    sess = await db.get(ChatSession, run.session_id)
    contract = await work.prompt_note(db, sess) if sess is not None else ""
    prompt = (contract + "\n\n" if contract else "") + run.prompt
    if not run.context_artifact_ids:
        return prompt
    lines = []
    for aid in run.context_artifact_ids:
        art = await db.get(Artifact, uuid.UUID(str(aid)))
        if art is None or art.deleted_at is not None:
            continue
        # 관리 영역 복사본은 guard 보호 영역이라 에이전트가 못 읽는다. 원본 경로를 알려준다.
        lines.append(f"- {art.filename}: {art.original_path}")
    if not lines:
        return prompt
    return "[사용자가 함께 보낸 파일(보고 있던 결과물·첨부)]\n" + "\n".join(lines) + "\n\n" + prompt


async def _system_note_parts(db: AsyncSession, outbox: Path, sess: ChatSession, scope: str = "all") -> list[tuple[str, str]]:
    """(레이어 id, 본문) 순서 목록. 하네스 조회 화면(harness_view)이 같은 목록을 그대로 보여 주므로 순서·id 를 바꾸면 그쪽 시험이 깨진다."""
    parts = [("system_note", SYSTEM_NOTE.format(outbox=outbox) + "\n\n" + instructions_mod.access_note(outbox)),
             ("agent_note", AGENT_NOTE),
             ("capabilities", capabilities.prompt_note())]
    role: instructions_mod.Role | None = None
    if sess.kind == SessionKind.orchestrator:
        # 접수가 만든 기획 세션은 사용자와 직접 대화하지 않는다. 옛 메인 오케스트레이터(부모 없음)는 그대로.
        parts.append(("lead_note", LEAD_NOTE) if sess.parent_id else ("orchestrator_note", ORCHESTRATOR_NOTE))
        role = "lead"
    elif sess.kind == SessionKind.worker:
        role = "worker"
    for key, label, text in (("common_instructions", "User's standing instructions for this assistant",
                              instructions_mod.read("common")),
                             ("role_instructions", "Instructions for your role",
                              instructions_mod.read(role) if role else "")):
        if text:
            parts.append((key, f"[{label}]\n{text}"))
    brief = await pinned_brief(db, scope=scope)
    if brief:
        parts.append(("pinned_brief", "[Pinned facts about the user — use recall for more]\n" + brief))
    if sess.kind == SessionKind.orchestrator:
        parts.append(("planning_contract", instructions_mod.PLANNING_CONTRACT))
        from ttakkari.orchestrator.runtime import model_routing_note
        parts.append(("routing_note", model_routing_note()))
    return parts


async def _system_note(db: AsyncSession, outbox: Path, sess: ChatSession, scope: str = "all") -> str:
    """기본 안내 + 도구 안내 + 비서 지침(~/.ttakkari/harness, 공통 + 역할) + 고정 기억. 매 Run 에 들어가므로 짧게 유지한다(ADR 0007, 0020)."""
    return "\n\n".join(text for _, text in await _system_note_parts(db, outbox, sess, scope))


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat()


def _launch_model(sess: ChatSession, run: Run, prof: profiles_mod.Profile, first: profiles_mod.Profile) -> tuple[str | None, str | None]:
    """계정에 넘길 (모델, 생각 수준). 모델 이름은 엔진마다 다르다. 그 엔진의 모델일 때만 넘기고, 어느 엔진 것인지 모르면 첫 계정에만 넘긴다.

    메인 오케스트레이터는 엔진마다 허용된 한 쌍(설정 orchestrator_fallback_models)으로만 띄운다.
    작업 세션이 다른 엔진으로 넘어가면 같은 급의 상대 모델(model_counterparts)로 바꾼다."""
    s = get_settings()
    if prof.engine == "opencode" and sess.kind != SessionKind.orchestrator:
        return prof.model or run.model or s.worker_default_models["opencode"]["model"], run.effort or "medium"
    if prof.engine == "copilot" and sess.kind != SessionKind.orchestrator:
        return prof.model or run.model or s.worker_default_models["copilot"]["model"], run.effort or "medium"
    if prof.engine == "muse" and sess.kind != SessionKind.orchestrator:
        return prof.model or run.model or s.worker_default_models["muse"]["model"], MUSE_EFFORT
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
             f"Account {profile} stopped before finishing: {(state.error or stderr or 'engine unavailable')[:500]}",
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
    touched = sorted(getattr(state, "touched_files", None) or [])
    if touched:
        # Qwen 실패→클라우드 인계 때 바뀐 파일 목록이 없으면 클라우드가 처음부터 뒤진다.
        lines.append(f"Files changed by the stopped attempt ({len(touched)}):")
        lines.extend(f"- {path}" for path in touched[:40])
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
    from ttakkari.runs.agent_control import should_suppress_report
    if await should_suppress_report(db, run):
        return None
    parent = await db.scalar(
        select(ChatSession).where(ChatSession.id == sess.parent_id).with_for_update()
    )
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
        .order_by(Run.created_at.desc()).limit(1).with_for_update())
    if pending is not None:
        try:
            owned = await manager.is_owned(pending.id, db=db)
        except TypeError:
            owned = await manager.is_owned(pending.id)
        if not owned:
            pending.prompt = pending.prompt + "\n\n" + text
            parent.updated_at = utcnow()
            await db.flush()
            return parent.id
    report = Run(session_id=parent.id, workspace_id=parent.workspace_id, prompt=text, engine=parent.engine,
                 model=parent.model, effort=parent.effort, status=RunStatus.queued, origin=RunOrigin.report)
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
    from ttakkari.orchestrator import work
    contract = await work.load(db, sess.id)
    goal_status = (contract.get("state") or {}).get("status")
    label = {RunStatus.succeeded: "목표 완료" if goal_status == "complete" else "턴 종료(목표 완료 미확정)",
             RunStatus.failed: "실패", RunStatus.cancelled: "취소",
             RunStatus.interrupted: "중단"}.get(RunStatus(run.status), run.status)
    text = run.result_text or (f"(실패) {run.error}" if run.error else "")
    if contract.get("state") and goal_status != "complete":
        state = contract["state"]
        pending = [r["id"] for r in state["requirements"] if r["status"] != "done"]
        text = f"[전체 목표 {goal_status}; 미완료: {', '.join(pending) or '최종 확인'}]\n" + text
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
