"""Durable goal contract, independent of engine conversations and PMS percentages.

Uses existing app_settings and Run queues. No schema migration or second WBS engine.
Evidence is an agent assertion with a locator, not an independently verified receipt.
"""
from __future__ import annotations

import json
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.audit import record
from ttakkari.models import (
    AppSetting,
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    RuntimeCommand,
    RuntimeLease,
    ScheduledAction,
    SessionKind,
    utcnow,
)
from ttakkari.orchestrator.goal_signals import (
    error_signature,
    evidence_line,
    extract_items,
    failure_kind,
    is_complex,
    request_text,
    run_evidence,
    unmapped_items,
    weak_evidence,
)
from ttakkari.runs import events

ACTIVE = (RunStatus.queued, RunStatus.running)
MAX_UNCHANGED_CONTINUATIONS = 2
MAX_TRANSIENT_RETRIES = 2


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "review", "done"] = "pending"
    evidence: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def evidence_for_done(self):
        if self.status == "done":
            if not self.evidence:
                raise ValueError(f"{self.id}: done requires evidence")
            if weak_evidence(self.evidence):
                raise ValueError(f"{self.id}: done evidence '{self.evidence}' is weak; must provide command output, commit, or verification receipt")
        return self


class WorkState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    goal: str = Field(min_length=1, max_length=2000)
    strategy: str = Field(min_length=1, max_length=3000)
    requirements: list[Requirement] = Field(min_length=1, max_length=100)
    status: Literal["active", "blocked", "paused", "complete"] = "active"
    next_action: str = Field(default="", max_length=2000)
    blocker: str = Field(default="", max_length=2000)
    replan_reason: str = Field(default="", max_length=2000)
    completed_work: list[str] = Field(default_factory=list, max_length=100)
    validation: list[str] = Field(default_factory=list, max_length=100)
    changed_files: list[str] = Field(default_factory=list, max_length=100)
    decisions: list[str] = Field(default_factory=list, max_length=100)
    pms_project_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def completion(self):
        ids = [r.id for r in self.requirements]
        if len(set(ids)) != len(ids):
            raise ValueError("requirement ids must be unique")
        pending = [r.id for r in self.requirements if r.status != "done"]
        if self.status == "complete" and pending:
            raise ValueError(f"unfinished requirements: {', '.join(pending)}")
        if self.status == "active" and not self.next_action:
            raise ValueError("active work requires next_action")
        if self.status in ("blocked", "paused") and not self.blocker:
            raise ValueError("blocked/paused work requires an exact reason (paused: cite user's pause request)")
        for values in (self.completed_work, self.validation, self.changed_files, self.decisions):
            if any(not v.strip() or len(v) > 2000 for v in values):
                raise ValueError("checkpoint entries must contain 1–2000 characters")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False)) > 30000:
            raise ValueError("checkpoint exceeds 30000 characters; link detailed repository evidence")
        return self


class WorkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["get", "checkpoint"] = "get"
    revision: int = Field(default=0, ge=0)
    state: WorkState | None = None


def key(sid: uuid.UUID) -> str:
    return f"lead-work:{sid}"


async def load(db: AsyncSession, sid: uuid.UUID) -> dict:
    row = await db.get(AppSetting, key(sid), populate_existing=True)
    data = dict(row.value) if row else {"revision": 0, "state": None}
    state = data.get("state")
    if state:
        reqs = state.get("requirements", [])
        done_reqs = [r for r in reqs if r.get("status") == "done"]
        pending_reqs = [r for r in reqs if r.get("status") != "done"]
        status = state.get("status", "active")

        # 실행 중인 작업자 세션 조회 (A6)
        active_workers = list(await db.scalars(
            select(ChatSession.title).where(
                ChatSession.parent_id == sid,
                ChatSession.archived_at.is_(None),
                ChatSession.id.in_(select(Run.session_id).where(Run.status.in_(ACTIVE)))
            )
        ))

        weak_items = [r for r in done_reqs if weak_evidence(r.get("evidence", ""))]

        data["goal_status"] = status
        data["completed_scope"] = done_reqs
        data["pending_scope"] = pending_reqs
        data["running_tasks"] = active_workers
        data["next_action"] = state.get("next_action", "")
        data["stop_reason"] = state.get("blocker", "") if status in ("blocked", "paused") else ""
        data["auto_resumable"] = status == "active" or (status == "blocked" and "progress" in state.get("blocker", ""))
        data["user_intervention_needed"] = status in ("blocked", "paused") or bool(weak_items)
    return data


async def checkpoint(db: AsyncSession, sess: ChatSession, body: WorkIn) -> dict:
    # Same lock is taken by the finish hook; concurrent checkpoints cannot overwrite each other.
    await db.scalar(select(ChatSession.id).where(ChatSession.id == sess.id).with_for_update())
    current = await load(db, sess.id)
    if current["revision"] != body.revision:
        raise HTTPException(409, "stale work revision; get current state before updating")
    if body.state is None:
        raise HTTPException(422, "checkpoint requires state")
    value = body.state.model_dump(mode="json")
    previous = current.get("state")
    if previous:
        old = {r["id"]: r for r in previous["requirements"]}
        new = {r["id"]: r for r in value["requirements"]}
        if old.keys() - new.keys() or any(new[i]["text"] != r["text"] for i, r in old.items() if i in new):
            raise HTTPException(422, "preserve original requirements; add corrections and explain changes")
        if previous.get("pms_project_id") and previous["pms_project_id"] != value.get("pms_project_id"):
            raise HTTPException(422, "preserve linked PMS project")
        for ident, req in old.items():
            if req["status"] == "done" and req["evidence"] not in new[ident]["evidence"]:
                raise HTTPException(422, f"{ident}: preserve completed evidence; append correction/revalidation")
        if previous["goal"] != value["goal"]:
            raise HTTPException(422, "preserve original goal; record new facts in strategy/decisions")
        if previous["strategy"] != value["strategy"] and not value["replan_reason"]:
            raise HTTPException(422, "strategy changes require replan_reason")
    if value["status"] == "complete":
        children = select(ChatSession.id).where(ChatSession.parent_id == sess.id)
        if await db.scalar(select(Run.id).where(Run.session_id.in_(children), Run.status.in_(ACTIVE)).limit(1)):
            raise HTTPException(422, "unfinished worker execution; reconcile worker results before completion")
    if value.get("pms_project_id"):
        from ttakkari.api.pms import _project
        from ttakkari.pms import service as pms
        project = await _project(db, uuid.UUID(value["pms_project_id"]))
        if value["status"] == "complete":
            ordered, _ = await pms.tree(db, project.id)
            unfinished = [n.wbs for n in ordered if n.is_leaf and n.status != "done"]
            if unfinished:
                raise HTTPException(422, f"unfinished PMS tasks: {', '.join(unfinished[:20])}")
    original = current.get("original_request")
    if original is None:
        first = await db.scalar(select(Run).where(Run.session_id == sess.id,
                                   Run.origin.in_((RunOrigin.user, RunOrigin.desk)))
                                .order_by(Run.created_at, Run.id).limit(1))
        original = {"run_id": str(first.id), "prompt": first.prompt} if first else None

    # 원본 요청 항목 대조: 완료 선언 시 원본 요청에 있던 항목이 누락됐는지 검증 (A2, A3)
    if value["status"] == "complete" and original:
        orig_items = original.get("items")
        if not orig_items and original.get("prompt"):
            orig_items = extract_items(request_text(original["prompt"], "user"))
        if orig_items:
            missing = unmapped_items(orig_items, value["requirements"])
            if missing:
                labels = [m["label"] for m in missing]
                raise HTTPException(422, f"unfinished original requirements: {', '.join(labels)}")

    # Updating prose/revision alone is not progress. Budget only resets when observed work changes.
    progress_fields = ("requirements", "completed_work", "validation", "changed_files")
    progressed = not previous or any(previous.get(f) != value.get(f) for f in progress_fields)
    saved = {"revision": body.revision + 1, "state": value, "original_request": original,
             "unchanged_continuations": 0 if progressed else current.get("unchanged_continuations", 0),
             "state_checkpoint_progressed": progressed,
             "updated_at": utcnow().isoformat()}
    row = await db.get(AppSetting, key(sess.id))
    if row is None:
        db.add(AppSetting(key=key(sess.id), value=saved))
    else:
        row.value = saved
    await db.flush()
    if value["status"] != "active":
        await cancel_queued_continuations(db, sess.id, f"goal contract is {value['status']}: {value.get('blocker', '')}")
    return saved


async def ensure_auto_contract(db: AsyncSession, sess: ChatSession, prompt: str | None = None,
                               origin: str = "user") -> dict | None:
    """복합 실행 요청에서 목표 계약이 누락되는 경로를 제거한다(A2)."""
    if sess.kind != SessionKind.orchestrator or sess.archived_at:
        return None
    current = await load(db, sess.id)
    state = current.get("state")

    if not prompt:
        first = await db.scalar(select(Run).where(
            Run.session_id == sess.id,
            Run.origin.in_((RunOrigin.user, RunOrigin.desk))
        ).order_by(Run.created_at, Run.id).limit(1))
        if first:
            prompt = first.prompt
            origin = first.origin.value if hasattr(first.origin, "value") else str(first.origin)

    if not prompt:
        return current

    clean = request_text(prompt, origin)
    # 단순 질문·짧은 요청에는 계약이나 추가 추론을 강제하지 않는다 (A2).
    if not is_complex(clean):
        return current

    # 이미 active 상태인 계약이 있으면 새로 덮어쓰지 않는다.
    if state and state.get("status") == "active":
        return current

    # 계약이 없거나 이전 계약이 complete/blocked/paused인 상태에서 새 복합 요청이 들어옴
    items = extract_items(clean)
    if items:
        reqs = [Requirement(id=it["label"], text=it["text"], status="pending") for it in items]
    else:
        lines = [ln.strip() for ln in clean.splitlines() if len(ln.strip()) >= 5]
        req_texts = lines[:10] if len(lines) >= 2 else [clean[:300]]
        reqs = [Requirement(id=str(idx + 1), text=t, status="pending") for idx, t in enumerate(req_texts)]

    goal_text = clean.splitlines()[0][:200]
    first_run = await db.scalar(select(Run).where(
        Run.session_id == sess.id,
        Run.origin.in_((RunOrigin.user, RunOrigin.desk))
    ).order_by(Run.created_at, Run.id).limit(1))
    orig_data = {
        "run_id": str(first_run.id) if first_run else None,
        "prompt": prompt,
        "items": items,
    }
    auto_state = WorkState(
        goal=goal_text,
        strategy="복합 요청 자동 접수 — 원본 요구 항목별 실행 및 검증",
        requirements=reqs,
        status="active",
        next_action=reqs[0].text,
    )
    saved = {
        "revision": current.get("revision", 0) + 1,
        "state": auto_state.model_dump(mode="json"),
        "original_request": orig_data,
        "unchanged_continuations": 0,
        "updated_at": utcnow().isoformat(),
    }
    row = await db.get(AppSetting, key(sess.id))
    if row is None:
        db.add(AppSetting(key=key(sess.id), value=saved))
    else:
        row.value = saved
    await db.flush()
    return saved


async def prompt_note(db: AsyncSession, sess: ChatSession, run: Run | None = None) -> str:
    if sess.kind != SessionKind.orchestrator or sess.id is None:
        return ""
    current = await load(db, sess.id)
    if not current.get("state") and run:
        clean = request_text(run.prompt, run.origin.value if hasattr(run.origin, "value") else str(run.origin))
        if is_complex(clean):
            await ensure_auto_contract(db, sess, run.prompt, run.origin.value if hasattr(run.origin, "value") else str(run.origin))
            current = await load(db, sess.id)
    if not current.get("state"):
        return ""
    # Entire state fits its explicit limit; do not silently truncate goal or pending requirements.
    original = current.get("original_request")
    pointer = ({"run_id": original["run_id"], "prompt": original["prompt"]} if original and
               len(original.get("prompt", "")) <= 8000 else {"run_id": original["run_id"], "prompt": "원본은 read_session으로 확인"}
               if original else None)
    payload = {"revision": current["revision"], "original_request": pointer, "state": current["state"]}
    return "[영속 목표 계약 — 보고/메모는 실행 증거와 대조하고, 원래 요구와 미완료부터 이어간다]\n" + json.dumps(payload, ensure_ascii=False)


def is_contract_resume_schedule(schedule: ScheduledAction) -> bool:
    """Only an explicit opt-in marks a schedule as a goal continuation.

    Prompt/description text and generic Run origins are user content, not ownership.
    Automatic goal followups currently create Runs directly; legacy schedules without
    the flag remain personal schedules rather than being retroactively claimed.
    """
    return schedule.action_type == "send_message" and (schedule.payload or {}).get("work_continuation") is True


async def cancel_queued_continuations(db: AsyncSession, sid: uuid.UUID, reason: str) -> None:
    """Caller holds the session lock. Stop only explicitly owned goal followups.

    Lock each queued Run against generation claims. Already leased work must be
    cancelled by its owner, without changing its status/event sequence unfenced.
    """
    rows = (await db.scalars(select(Run).where(
        Run.session_id == sid, Run.origin == RunOrigin.orchestrator,
        Run.status == RunStatus.queued).order_by(Run.id).with_for_update())).all()
    for queued in rows:
        marker = (queued.usage or {}).get("work_continuation")
        owned = marker is True or isinstance(marker, dict) and bool(marker)
        # Pre-marker scheduled Runs retain a durable link to their exact schedule.
        if not owned and (queued.request_key or "").startswith("schedule:"):
            try:
                schedule_id = uuid.UUID(queued.request_key.removeprefix("schedule:"))
            except ValueError:
                continue
            schedule = await db.get(ScheduledAction, schedule_id)
            owned = (schedule is not None and is_contract_resume_schedule(schedule)
                     and (schedule.payload or {}).get("session_id") == str(sid))
        if not owned:
            continue
        lease = await db.get(RuntimeLease, queued.id)
        if lease is not None:
            from ttakkari.generation import wake
            db.add(RuntimeCommand(key=str(uuid.uuid4()), kind="cancel", run_id=queued.id,
                                  generation_id=lease.generation_id))
            await wake(db, queued.id)
            record(db, actor="system", action="work.cancel_requested", target_type="run",
                   target_id=queued.id, detail={"reason": reason[:2000]})
            continue
        queued.status = RunStatus.cancelled
        queued.finished_at = utcnow()
        queued.resume_after = None
        queued.error = reason[:2000]
        events.append(db, queued, "run.finished", {"status": RunStatus.cancelled, "error": queued.error,
                                                  "result_text": queued.result_text, "work_continuation": True})
        record(db, actor="system", action="run.finished", outcome="error", target_type="run",
               target_id=queued.id, detail={"status": RunStatus.cancelled, "reason": queued.error})


async def after_run(db: AsyncSession, sess: ChatSession, run: Run) -> uuid.UUID | None:
    if sess.kind != SessionKind.orchestrator or sess.archived_at:
        return None
    await db.scalar(select(ChatSession.id).where(ChatSession.id == sess.id).with_for_update())
    current = await load(db, sess.id)
    state = current.get("state")
    if not state and run.prompt:
        # 계약이 누락된 복합 요청인지 검사해 자동 생성 (A2)
        clean = request_text(run.prompt, run.origin.value if hasattr(run.origin, "value") else str(run.origin))
        if is_complex(clean):
            await ensure_auto_contract(db, sess, run.prompt, run.origin.value if hasattr(run.origin, "value") else str(run.origin))
            current = await load(db, sess.id)
            state = current.get("state")
    if not state or state["status"] != "active":
        return None

    # Run 실행 증거 수집 (A5): 도구 호출, 파일 변경, 커밋, 테스트 결과 추출
    ev = await run_evidence(db, run)
    prev_fingerprint = current.get("last_evidence_fingerprint")
    new_fingerprint = ev.get("fingerprint")
    evidence_progressed = bool(ev.get("effective") and new_fingerprint != prev_fingerprint)
    state_progressed = current.get("state_checkpoint_progressed", False)
    progressed = evidence_progressed or state_progressed

    # 체크포인트 누락 성과 복원 (A4, A5): 에이전트가 상태 체크포인트를 건너뛰었더라도 실행 증거를 상태에 병합
    if ev.get("effective"):
        existing_files = set(state.get("changed_files", []))
        existing_tests = set(state.get("validation", []))
        added_files = [f for f in ev.get("files", []) if f not in existing_files]
        added_tests = [t for t in ev.get("tests", []) if t not in existing_tests]
        if added_files or added_tests:
            state = {
                **state,
                "changed_files": (state.get("changed_files", []) + added_files)[:100],
                "validation": (state.get("validation", []) + added_tests)[:100],
            }

    # 취소/실패 처리
    if run.status != RunStatus.succeeded:
        row = await db.get(AppSetting, key(sess.id))
        if run.status == RunStatus.cancelled:
            new_status = "paused"
            reason = f"{run.engine} Run {run.id} cancelled: {run.error or 'cancelled; inspect user stop request'}"
            row.value = {**current, "state": {**state, "status": new_status, "blocker": reason[:2000]},
                         "revision": current["revision"] + 1}
            events.append(db, run, f"work.{new_status}", {"reason": reason[:2000]})

            # 취소 시 자동 재개 예약 취소
            sched_stmt = select(ScheduledAction).where(
                ScheduledAction.action_type == "send_message",
                ScheduledAction.status.in_(("pending", "leased")),
                ScheduledAction.payload["session_id"].as_string() == str(sess.id),
            )
            pending_scheds = (await db.scalars(sched_stmt)).all()
            for ps in pending_scheds:
                if is_contract_resume_schedule(ps):
                    ps.status = "cancelled"
                    ps.last_error = f"cancelled due to session {sess.id} work pause on run cancel"
                    ps.updated_at = utcnow()
            await cancel_queued_continuations(db, sess.id, reason)
            return None
        elif run.status == RunStatus.failed:
            f_kind = failure_kind(run.error)
            sig = error_signature(run.error)
            transient_map = dict(current.get("transient_retries") or {})
            retries = transient_map.get(sig, 0)

            # 일시 오류(계정 제한, 엔진 일시 오류)이고 재시도 예산(MAX_TRANSIENT_RETRIES) 이내이면 즉시 blocked하지 않고 재개 (A5)
            if f_kind in ("usage_limit", "engine_transient") and retries < MAX_TRANSIENT_RETRIES:
                transient_map[sig] = retries + 1
                row.value = {**current, "state": state, "transient_retries": transient_map}
                continuation_prompt = (
                    f"[미완료 목표 자동 이어가기] 직전 시도에서 일시적 {f_kind} 오류 발생 ({run.error or 'transient failure'}).\n"
                    f"오류 지문 {sig} (시도 {retries + 1}/{MAX_TRANSIENT_RETRIES}). 동일 실패 반복을 피하고 대체 엔진/방법으로 계속하라.\n"
                    f"미완료: " + ", ".join(r["text"] for r in state["requirements"] if r["status"] != "done")
                    + "\n다음 동작: " + state["next_action"]
                    + "\n현재 변경·완료 증거를 재사용하라. 전체 재분석/동일 검사 반복 금지."
                )
                continuation = Run(session_id=sess.id, workspace_id=sess.workspace_id, engine=sess.engine,
                                   model=sess.model, effort=sess.effort, origin=RunOrigin.orchestrator,
                                   prompt=continuation_prompt,
                                   usage={"work_continuation": {"source_run_id": str(run.id), "revision": current["revision"], "transient": True}})
                db.add(continuation)
                await db.flush()
                events.append(db, continuation, "run.status", {"status": RunStatus.queued, "work_continuation": True, "transient": True})
                events.append(db, run, "work.continued", {"run_id": str(continuation.id), "revision": current["revision"], "transient": True})
                return continuation.id
            else:
                # 영구 장애이거나 같은 실패 반복 상한 초과 (A5)
                reason = f"{run.engine} Run {run.id} failed: {run.error or 'unknown failure'}"
                row.value = {**current, "state": {**state, "status": "blocked", "blocker": reason[:2000]},
                             "revision": current["revision"] + 1}
                events.append(db, run, "work.blocked", {"reason": reason[:2000], "failure_kind": f_kind})
                return None
        return None

    # User steering/reports already queued, and live child workers, are durable owners of the next step.
    children = select(ChatSession.id).where(ChatSession.parent_id == sess.id, ChatSession.archived_at.is_(None))
    existing = await db.scalar(select(Run.id).where(Run.id != run.id, Run.status.in_(ACTIVE),
                                (Run.session_id == sess.id) | Run.session_id.in_(children)).limit(1))
    schedules = (await db.scalars(select(ScheduledAction).where(
        ScheduledAction.action_type == "send_message", ScheduledAction.status.in_(("pending", "leased")),
        ScheduledAction.payload["session_id"].as_string() == str(sess.id)))).all()
    if existing is not None or any(is_contract_resume_schedule(item) for item in schedules):
        return None

    # 남은 미완료 요구사항 확인
    pending_reqs = [r for r in state["requirements"] if r["status"] != "done"]
    if not pending_reqs:
        # 모든 요구사항 완료됨 (완료 체크포인트는 에이전트/클라이언트가 검증 후 호출)
        return None

    # 진전 판정 및 연속 미진전 카운트 (A4, A5)
    row = await db.get(AppSetting, key(sess.id))
    if progressed:
        count = 0
    else:
        count = current.get("unchanged_continuations", 0) + 1

    if count >= MAX_UNCHANGED_CONTINUATIONS:
        state = {**state, "status": "blocked",
                 "blocker": f"No recorded progress after {MAX_UNCHANGED_CONTINUATIONS} automatic continuations; inspect execution evidence before resuming"}
        row.value = {**current, "state": state, "revision": current["revision"] + 1, "unchanged_continuations": count}
        events.append(db, run, "work.blocked", {"reason": state["blocker"]})
        return None

    # 같은 프롬프트 단순 반복 금지 (A4): 진전 성과, 완료된 항목, 남은 미완료 항목을 구체적으로 명시
    ev_line = evidence_line(ev)
    done_texts = [r["text"] for r in state["requirements"] if r["status"] == "done"]
    continuation_prompt = (
        "[미완료 목표 자동 이어가기] 중간 Gate/턴 종료는 목표 완료가 아니다.\n"
        f"- 직전 실행 성과: {ev_line}\n"
        f"- 이미 완료된 항목: {', '.join(done_texts) if done_texts else '없음'} (재실행 금지)\n"
        "- 남은 미완료 항목: " + ", ".join(r["text"] for r in pending_reqs)
        + "\n다음 동작: " + state["next_action"]
        + "\n현재 변경·완료 증거를 재사용하고 남은 미완료 항목에 집중하라. 전체 재분석/동일 검사 반복 금지."
    )
    continuation = Run(session_id=sess.id, workspace_id=sess.workspace_id, engine=sess.engine,
                       model=sess.model, effort=sess.effort, origin=RunOrigin.orchestrator,
                       prompt=continuation_prompt,
                       usage={"work_continuation": {"source_run_id": str(run.id), "revision": current["revision"]}})
    db.add(continuation)
    await db.flush()
    row.value = {**current, "state": state, "unchanged_continuations": count,
                 "last_evidence_fingerprint": new_fingerprint if ev.get("effective") else prev_fingerprint,
                 "state_checkpoint_progressed": False}
    events.append(db, continuation, "run.status", {"status": RunStatus.queued, "work_continuation": True})
    events.append(db, run, "work.continued", {"run_id": str(continuation.id), "revision": current["revision"]})
    return continuation.id
