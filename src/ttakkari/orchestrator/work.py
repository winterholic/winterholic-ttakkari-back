"""Durable goal contract, independent of engine conversations and PMS percentages.

Uses existing app_settings and Run queues. No schema migration or second WBS engine.
Evidence is an agent assertion with a locator, not an independently verified receipt.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
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
    classify_followup,
    error_signature,
    evidence_line,
    extract_constraints,
    extract_items,
    failure_kind,
    is_complex,
    request_text,
    run_evidence,
    unmapped_items,
    weak_evidence,
)
from ttakkari.runs import events

log = logging.getLogger("ttakkari.work")

ACTIVE = (RunStatus.queued, RunStatus.running)
# 진전 기록 없이 이어갈 수 있는 자동 후속 횟수. 2회 이어간 뒤 또 진전이 없으면 blocked.
MAX_UNCHANGED_CONTINUATIONS = 2
# 일시 엔진 오류: 같은 오류 지문 2회, 지문과 무관하게 진전 없이 연속 4회까지만 다시 본다. 진전이 있으면 모두 0으로 돌린다.
MAX_TRANSIENT_RETRIES = 2
MAX_TRANSIENT_TOTAL = 4
TRANSIENT_BACKOFF_SECONDS = 60
# 계정 한도·모델 용량은 기다리는 상태다. 대기 횟수에만 상한을 둔다(회당 최대 USAGE_WAIT_MAX_SECONDS).
MAX_USAGE_WAITS = 10
USAGE_WAIT_BASE_SECONDS = 900
USAGE_WAIT_MAX_SECONDS = 6 * 3600
MAX_ADDITIONAL_REQUESTS = 10
MAX_HISTORY = 5
INTAKE_ORIGINS = (RunOrigin.user, RunOrigin.desk)


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
                raise ValueError(f"{self.id}: done evidence '{self.evidence}' is weak; give a command output, commit, or file locator")
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
    # complete 체크포인트에서 원본 요청 항목이 계약과 대응되지 않아 보일 때 한 번 경고하고, 확인했다는 표시로 다시 보낸다.
    confirm_original_items: bool = False

    @model_validator(mode="before")
    @classmethod
    def _hoist_confirm_from_state(cls, data):
        """state 안에 넣어 보낸 confirm_original_items 도 받는다. 409 안내를 state 필드로 읽거나, 이 인자가 없는 옛 MCP 도구
        스키마(오래 산 기획 세션)에서도 확인을 보낼 수 있어야 한다. state 모델은 모르는 필드를 거부하므로 먼저 꺼낸다."""
        state = data.get("state") if isinstance(data, dict) else None
        if isinstance(state, dict) and "confirm_original_items" in state:
            state = dict(state)
            flag = state.pop("confirm_original_items")
            data = {**data, "state": state, "confirm_original_items": bool(data.get("confirm_original_items")) or flag is True}
        return data


def key(sid: uuid.UUID) -> str:
    return f"lead-work:{sid}"


async def load(db: AsyncSession, sid: uuid.UUID) -> dict:
    row = await db.get(AppSetting, key(sid), populate_existing=True)
    return dict(row.value) if row else {"revision": 0, "state": None}


async def view(db: AsyncSession, sid: uuid.UUID) -> dict:
    """API 응답용. 저장값에 파생 필드를 붙여 돌려주되 영속하지는 않는다(요구가 이중 저장되지 않게)."""
    data = await load(db, sid)
    state = data.get("state")
    if not state:
        return data
    reqs = state.get("requirements", [])
    status = state.get("status", "active")
    workers = list(await db.scalars(select(ChatSession.title).where(
        ChatSession.parent_id == sid, ChatSession.archived_at.is_(None),
        ChatSession.id.in_(select(Run.session_id).where(Run.status.in_(ACTIVE))))))
    weak = [r for r in reqs if r.get("status") == "done" and weak_evidence(r.get("evidence", ""))]
    data["goal_status"] = status
    data["completed_scope"] = [r for r in reqs if r.get("status") == "done"]
    data["pending_scope"] = [r for r in reqs if r.get("status") != "done"]
    data["running_tasks"] = workers
    data["next_action"] = state.get("next_action", "")
    data["stop_reason"] = state.get("blocker", "") if status in ("blocked", "paused") else ""
    data["auto_resumable"] = status == "active"
    data["waiting_until"] = data.get("waiting_until") if status == "active" else None
    data["user_intervention_needed"] = status in ("blocked", "paused") or bool(weak)
    return data


async def summary(db: AsyncSession, sid: uuid.UUID) -> dict | None:
    """사용자 화면용 목표 상태 요약. 계약이 없으면 None. `view` 의 파생 로직을 재사용하고 읽기만 한다.

    원본 요청·이력·추가 요청 본문·금지 조건 같은 계약 내부는 담지 않는다. 대기(waiting)·사용자 중지(paused)·
    사용자 조치 필요(blocked)를 phase/user_action 으로 가른다. Run 종료는 상태를 바꾸지 않는다."""
    data = await view(db, sid)
    state = data.get("state")
    if not state:
        return None
    status = state["status"]
    until = None
    if status == "active" and data.get("waiting_until"):
        parsed = datetime.fromisoformat(data["waiting_until"])
        until = parsed if parsed > datetime.now(UTC) else None  # 지난 대기 시각은 더 이상 대기가 아니다
    weak = [r for r in data["completed_scope"] if weak_evidence(r.get("evidence", ""))]
    phase = {"blocked": "blocked", "paused": "paused", "complete": "complete"}.get(status, "waiting" if until else "running")
    user_action = {"blocked": "intervene", "paused": "resume"}.get(status, "review" if weak else "none")

    def req(r: dict) -> dict:
        return {"id": r["id"], "text": r["text"], "status": r["status"], "evidence": (r.get("evidence") or "")[:300]}

    return {"status": status, "phase": phase, "goal": state["goal"], "revision": data["revision"],
            "completed": [req(r) for r in data["completed_scope"]], "pending": [req(r) for r in data["pending_scope"]],
            "completed_count": len(data["completed_scope"]), "pending_count": len(data["pending_scope"]),
            "running_tasks": list(data["running_tasks"]), "next_action": state.get("next_action", ""),
            "waiting_until": until, "stop_reason": data["stop_reason"],
            "blocker_kind": data.get("blocker_kind") if status == "blocked" else None,
            "auto_resumable": status == "active", "user_action": user_action,
            "user_intervention_needed": user_action in ("intervene", "review"),
            "additional_request_count": len(data.get("additional_requests") or []),
            "updated_at": data.get("updated_at")}


def _request_record(run_id: uuid.UUID | str | None, prompt: str, origin: str) -> dict:
    clean = request_text(prompt, origin)
    return {"run_id": str(run_id) if run_id else None, "prompt": prompt, "origin": origin,
            "items": extract_items(clean), "constraints": extract_constraints(clean)}


def _original_gaps(current: dict, requirements: list[dict]) -> list[dict]:
    """계약 요구가 덮지 못한 원본·추가 요청 항목. 판정이 어긋날 수 있어 차단이 아니라 확인 대상이다."""
    gaps: list[dict] = []
    sources = [s for s in [current.get("original_request"), *(current.get("additional_requests") or [])] if s]
    for src in sources:
        items = src.get("items")
        if items is None and src.get("prompt"):
            items = extract_items(request_text(src["prompt"], src.get("origin") or "user"))
        for miss in unmapped_items(items or [], requirements):
            gaps.append({"label": miss["label"], "text": miss["text"], "run_id": src.get("run_id")})
    return gaps


_STATUS_RANK = {"pending": 0, "review": 1, "done": 2}


def _checkpoint_progress(previous: dict | None, value: dict) -> bool:
    """체크포인트가 일을 실제로 진척시켰는가. 요구 상태 전이, 새 changed_files, 새 validation 만 진전이다.

    completed_work·evidence·next_action 문구만 바꾼 체크포인트는 진전이 아니다(외부 대기 중 문구만 갱신하며 무한히
    이어가는 것을 막는다). 새 요구 추가도 범위 증가이지 진전이 아니다."""
    if not previous:
        return True
    old = {r["id"]: _STATUS_RANK[r["status"]] for r in previous["requirements"]}
    if any(_STATUS_RANK[r["status"]] > old[r["id"]] for r in value["requirements"] if r["id"] in old):
        return True
    return any(set(value.get(f) or []) - set(previous.get(f) or []) for f in ("changed_files", "validation"))


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
    # 자동 접수 계약은 아직 아무 요구도 완료되지 않았다면 기획이 자기 계획으로 한 번 재구성할 수 있다.
    reshape = bool(previous) and bool(current.get("auto_generated")) and not any(
        r["status"] == "done" for r in previous["requirements"])
    if previous and not reshape:
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
        # 이전 계약을 보관한 뒤라면 그 이후 첫 요청이 이번 계약의 원본이다.
        since = ((current.get("history") or [{}])[-1]).get("archived_at")
        cond = [Run.session_id == sess.id, Run.origin.in_((RunOrigin.user, RunOrigin.desk))]
        if since:
            cond.append(Run.created_at >= datetime.fromisoformat(since))
        extra_ids = [uuid.UUID(r["run_id"]) for r in current.get("additional_requests") or [] if r.get("run_id")]
        if extra_ids:  # 계약 뒤에 들어와 추가 요청으로 보존된 요청은 원본이 아니다.
            cond.append(Run.id.not_in(extra_ids))
        # 계약을 처음 만드는 체크포인트가 대응하는 요청은 그 시점의 가장 최근 사용자 요청이다(긴 세션의 첫 요청이 아니다).
        first = await db.scalar(select(Run).where(*cond).order_by(Run.created_at.desc(), Run.id.desc()).limit(1))
        original = _request_record(first.id, first.prompt, str(getattr(first.origin, "value", first.origin))) if first else None
        current = {**current, "original_request": original}
    confirmed: list[str] = list(current.get("original_items_confirmed") or [])
    if value["status"] == "complete":
        gaps = _original_gaps(current, value["requirements"])
        if gaps and not body.confirm_original_items:
            listed = "; ".join(f"{g['label']}: {g['text'][:60]}" for g in gaps[:12])
            raise HTTPException(409, "original request items look unmapped to contract requirements "
                                f"({len(gaps)}): {listed}. If each is done or intentionally out of scope, "
                                "resend the same complete checkpoint with the top-level manage_work argument "
                                "confirm_original_items=true (a sibling of state, not a state field)")
        if gaps:
            confirmed = [*confirmed, *(g["label"] for g in gaps)][-60:]
    # Updating prose/revision alone is not progress. Budget only resets when observed work changes.
    progressed = _checkpoint_progress(previous, value)
    resumed = bool(previous) and previous["status"] in ("blocked", "paused") and value["status"] == "active"
    fresh = progressed or resumed
    saved = {**current, "revision": body.revision + 1, "state": value, "original_request": original,
             "unchanged_continuations": 0 if fresh else current.get("unchanged_continuations", 0),
             "state_checkpoint_progressed": bool(current.get("state_checkpoint_progressed")) or progressed,
             "auto_generated": False, "original_items_confirmed": confirmed,
             "updated_at": utcnow().isoformat()}
    if fresh:
        for counter in ("transient_retries", "transient_total", "usage_waits", "waiting_until"):
            saved.pop(counter, None)
    if value["status"] != "blocked":
        saved.pop("blocker_kind", None)
    row = await db.get(AppSetting, key(sess.id))
    if row is None:
        db.add(AppSetting(key=key(sess.id), value=saved))
    else:
        row.value = saved
    await db.flush()
    if value["status"] != "active":
        await cancel_queued_continuations(db, sess.id, f"goal contract is {value['status']}: {value.get('blocker', '')}")
    return saved


def _auto_state(clean: str, items: list[dict[str, str]], constraints: list[str] | None = None) -> WorkState:
    if items:
        reqs = [Requirement(id=it["label"], text=it["text"], status="pending") for it in items]
    else:
        lines = [ln.strip() for ln in clean.splitlines() if len(ln.strip()) >= 5]
        texts = lines[:10] if len(lines) >= 2 else [clean[:300]]
        reqs = [Requirement(id=str(i + 1), text=t[:1000], status="pending") for i, t in enumerate(texts)]
    return WorkState(goal=clean.splitlines()[0][:200] or "요청 수행", strategy="복합 요청 자동 접수 — 원본 요구 항목별 실행 및 검증",
                     requirements=reqs, status="active", next_action=reqs[0].text[:2000],
                     decisions=[f"원본 금지·제약(유지): {c}" for c in (constraints or [])[:10]])


def _archive_entry(current: dict) -> dict:
    return {"archived_at": utcnow().isoformat(), "revision": current.get("revision", 0),
            "state": current.get("state"), "original_request": current.get("original_request"),
            "additional_requests": current.get("additional_requests") or [],
            "original_items_confirmed": current.get("original_items_confirmed") or []}


async def intake_request(db: AsyncSession, sess: ChatSession, run_id: uuid.UUID | None, prompt: str,
                         origin: str) -> str | None:
    """사용자·접수(user/desk) 요청이 복합이면 목표 계약을 연다. 반환: created|appended|archived_new|None.

    - 계약 없음: 자동 계약(active)을 만든다.
    - active/blocked/paused: 덮어쓰지 않는다. 원문을 additional_requests 로 보존해 기획이 범위를 합치게 한다.
    - complete: 이전 계약을 history 로 보관하고 새 계약을 연다(이전 완료 증거는 지우지 않는다).
    호출자는 반드시 safe_intake 로 감싼다. 세션 행 잠금은 manage_work·after_run 과 같은 순서다."""
    if sess.kind != SessionKind.orchestrator or sess.archived_at or origin not in {o.value for o in INTAKE_ORIGINS}:
        return None
    clean = request_text(prompt, origin)
    complex_request = is_complex(clean)
    followup = classify_followup(clean) if not complex_request else "complex"
    await db.scalar(select(ChatSession.id).where(ChatSession.id == sess.id).with_for_update())
    current = await load(db, sess.id)
    state = current.get("state")
    record_ = _request_record(run_id, prompt, origin)
    if state and state["status"] != "complete":
        # 진행 중인 계약에서는 복합 요청과 범위 변경 요청만 추가 요청으로 남긴다. 잡담·질문은 그대로 둔다.
        if not (complex_request or followup == "scope_change"):
            return None
        extra = list(current.get("additional_requests") or [])
        if run_id and any(item.get("run_id") == str(run_id) for item in extra):
            return None
        extra.append({**record_, "state_status": state["status"], "at": utcnow().isoformat()})
        row = await db.get(AppSetting, key(sess.id))
        row.value = {**current, "additional_requests": extra[-MAX_ADDITIONAL_REQUESTS:],
                     "revision": current["revision"] + 1, "updated_at": utcnow().isoformat()}
        await db.flush()
        return "appended"
    if not complex_request:
        return None
    auto = _auto_state(clean, record_["items"], record_["constraints"])
    history = list(current.get("history") or [])
    archived = False
    if state:  # complete: 이전 계약은 보관하고 새 계약을 연다.
        history.append(_archive_entry(current))
        archived = True
    saved = {"revision": current.get("revision", 0) + 1, "state": auto.model_dump(mode="json"),
             "original_request": record_, "additional_requests": [], "history": history[-MAX_HISTORY:],
             "unchanged_continuations": 0, "auto_generated": True, "updated_at": utcnow().isoformat()}
    row = await db.get(AppSetting, key(sess.id))
    if row is None:
        db.add(AppSetting(key=key(sess.id), value=saved))
    else:
        row.value = saved
    await db.flush()
    return "archived_new" if archived else "created"


async def safe_intake(db: AsyncSession, sess: ChatSession, run_id: uuid.UUID | None, prompt: str, origin: str) -> str | None:
    """계약 자동 접수의 어떤 실패도 요청(Run) 저장을 막지 않는다. savepoint 안에서 실행하고 실패는 로그로만 남긴다."""
    try:
        async with db.begin_nested():
            return await intake_request(db, sess, run_id, prompt, origin)
    except Exception: # 접수 보조 기능이라 요청 저장이 항상 우선이다.
        log.exception("goal contract intake failed for session %s; request kept without auto contract", sess.id)
        return None


def _constraints(current: dict) -> list[str]:
    """원본·추가 요청의 금지·읽기 전용 문장(중복 제거). 계약·후속 프롬프트가 바뀌어도 유지한다."""
    found: list[str] = []
    for src in [current.get("original_request"), *(current.get("additional_requests") or [])]:
        for c in (src or {}).get("constraints") or []:
            if c not in found:
                found.append(c)
    return found[:20]


def _additional_note(current: dict) -> list[dict]:
    return [{"run_id": r.get("run_id"), "origin": r.get("origin"), "at": r.get("at"), "prompt": (r.get("prompt") or "")[:4000],
             "items": [f"{i['label']}: {i['text'][:80]}" for i in (r.get("items") or [])[:20]]}
            for r in (current.get("additional_requests") or [])]


def _history_note(current: dict) -> list[dict]:
    out = []
    for h in (current.get("history") or [])[-2:]:
        st = h.get("state") or {}
        out.append({"archived_at": h.get("archived_at"), "goal": st.get("goal"), "status": st.get("status"),
                    "requirements": [{"id": r["id"], "status": r["status"], "evidence": (r.get("evidence") or "")[:160]}
                                     for r in st.get("requirements", [])[:40]]})
    return out


async def prompt_note(db: AsyncSession, sess: ChatSession) -> str:
    if sess.kind != SessionKind.orchestrator or sess.id is None:
        return ""
    current = await load(db, sess.id)
    if not current.get("state"):
        return ""
    # Entire state fits its explicit limit; do not silently truncate goal or pending requirements.
    original = current.get("original_request")
    pointer = ({"run_id": original["run_id"], "prompt": original["prompt"]} if original and
               len(original["prompt"]) <= 8000 else {"run_id": original["run_id"], "prompt": "원본은 read_session으로 확인"}
               if original else None)
    payload = {"revision": current["revision"], "original_request": pointer, "state": current["state"]}
    head = "[영속 목표 계약 — 보고/메모는 실행 증거와 대조하고, 원래 요구와 미완료부터 이어간다]\n"
    if rules := _constraints(current):
        payload["original_constraints"] = rules
        head += ("[원본 금지·제약 — 요청 전체 또는 해당 단계에만 걸린 제한이다. 계약·재계획·후속에서도 그대로 지키고, "
                 "금지된 변경을 실행하는 요구로 바꾸지 마라]\n")
    if extra := _additional_note(current):
        payload["additional_requests"] = extra
        head += ("[추가 요청 — 계약 이후 들어온 사용자 요청이다. 기존 요구와 범위를 합쳐 요구를 추가하고 "
                 "replan_reason 에 근거를 남겨라. 이미 끝난 증거는 재사용한다]\n")
    if hist := _history_note(current):
        payload["previous_contracts"] = hist
        head += "[이전 완료 계약 — 새 요청과 겹치는 증거는 다시 하지 말고 연결해 인용하라]\n"
    return head + json.dumps(payload, ensure_ascii=False)


def is_contract_resume_schedule(schedule: ScheduledAction) -> bool:
    """Only an explicit opt-in marks a schedule as a goal continuation.

    Prompt/description text and generic Run origins are user content, not ownership.
    Automatic goal followups currently create Runs directly; legacy schedules without
    the flag remain personal schedules rather than being retroactively claimed.
    """
    return schedule.action_type == "send_message" and (schedule.payload or {}).get("work_continuation") is True


async def cancel_queued_continuations(db: AsyncSession, sid: uuid.UUID, reason: str, *,
                                      only: frozenset[uuid.UUID] | None = None) -> None:
    """Caller holds the session lock. Stop only explicitly owned goal followups.

    Lock each queued Run against generation claims. Already leased work must be
    cancelled by its owner, without changing its status/event sequence unfenced.
    """
    rows = (await db.scalars(select(Run).where(
        Run.session_id == sid, Run.origin == RunOrigin.orchestrator,
        Run.status == RunStatus.queued).order_by(Run.id).with_for_update())).all()
    for queued in rows:
        if only is not None and queued.id not in only:
            continue
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




async def release_waiting_continuations(db: AsyncSession, sid: uuid.UUID, reason: str) -> None:
    """사용자 새 지시가 오면 한도·오류 대기 중인 자동 후속은 거둔다. 대기가 사용자 지시를 뒤에 세우지 못하게 한다.

    사용자 Run 이 끝나면 after_run 이 미완료 요구로 새 후속을 이어 만든다."""
    now = utcnow()
    rows = (await db.scalars(select(Run).where(
        Run.session_id == sid, Run.origin == RunOrigin.orchestrator, Run.status == RunStatus.queued,
        Run.resume_after.is_not(None), Run.resume_after > now).order_by(Run.id).with_for_update())).all()
    owned = [r for r in rows if isinstance((r.usage or {}).get("work_continuation"), dict)]
    if owned:
        await cancel_queued_continuations(db, sid, reason, only=frozenset(r.id for r in owned))


def _pause_reason(run: Run) -> str:
    return f"{run.engine} Run {run.id} cancelled: {run.error or 'cancelled; inspect user stop request'}"


async def _pause_on_cancel(db: AsyncSession, sess: ChatSession, run: Run, row: AppSetting, current: dict, state: dict) -> None:
    reason = _pause_reason(run)
    row.value = {**current, "state": {**state, "status": "paused", "blocker": reason[:2000]},
                 "revision": current["revision"] + 1, "waiting_until": None}
    events.append(db, run, "work.paused", {"reason": reason[:2000]})
    # 이 계약이 만든 자동 후속·재개 예약은 취소해 이후에 자동으로 되살아나지 않게 한다. 사용자가 만든 무관한 예약은 보존한다.
    pending = (await db.scalars(select(ScheduledAction).where(
        ScheduledAction.action_type == "send_message", ScheduledAction.status.in_(("pending", "leased")),
        ScheduledAction.payload["session_id"].as_string() == str(sess.id)))).all()
    for item in pending:
        if is_contract_resume_schedule(item):
            item.status = "cancelled"
            item.last_error = f"cancelled due to session {sess.id} work pause on run cancel"
            item.updated_at = utcnow()
    await cancel_queued_continuations(db, sess.id, reason)


# 기획 세션이 MCP schedule_action 으로 자기에게 건 예약은 이 시간 안에 도래할 때만 후속의 소유자로 본다.
# 더 먼 예약 때문에 목표가 무기한 멈추지 않게 하는 상한이다.
SELF_SCHEDULE_HORIZON = timedelta(hours=24)
SELF_SCHEDULE_MARK = "created_by_session"  # 예약 생성 API 가 호출한 기획 세션 id 로 서버가 찍는다. 호출자가 정할 수 없다.


def is_self_schedule(schedule: ScheduledAction, sid: uuid.UUID) -> bool:
    """이 기획 세션이 직접 자기 세션에 건 send_message 예약인가. 사용자·다른 세션이 건 예약은 개인 예약이라 소유자가 아니다."""
    payload = schedule.payload or {}
    return (schedule.action_type == "send_message"
            and payload.get("session_id") == str(sid) and payload.get(SELF_SCHEDULE_MARK) == str(sid))


async def _owning_schedules(db: AsyncSession, sid: uuid.UUID) -> list[ScheduledAction]:
    """이 세션에 메시지를 보낼 예정이라 다음 단계를 맡고 있는 예약. 계약 재개 예약은 기한과 무관하게, 기획이 자기에게 건
    예약은 pending 이면 상한 안에 도래할 때만, 실행 중(leased)이면 늘 소유자다. 실패·취소·만료된 예약과 사용자·다른
    세션이 건 개인 예약은 소유자가 아니다."""
    horizon = utcnow() + SELF_SCHEDULE_HORIZON
    rows = (await db.scalars(select(ScheduledAction).where(
        ScheduledAction.action_type == "send_message", ScheduledAction.status.in_(("pending", "leased")),
        ScheduledAction.payload["session_id"].as_string() == str(sid)))).all()
    return [r for r in rows if is_contract_resume_schedule(r)
            or (is_self_schedule(r, sid) and (r.status == "leased" or r.due_at <= horizon))]


async def _owner_exists(db: AsyncSession, sess: ChatSession, run: Run | None) -> bool:
    """대기·실행 중인 사용자 지시와 보고, 살아 있는 작업자 Run, 이 세션을 향한 예약은 다음 단계의 소유자다."""
    children = select(ChatSession.id).where(ChatSession.parent_id == sess.id, ChatSession.archived_at.is_(None))
    existing = await db.scalar(select(Run.id).where(Run.id != (run.id if run else None), Run.status.in_(ACTIVE),
                               (Run.session_id == sess.id) | Run.session_id.in_(children)).limit(1))
    if existing is not None:
        return True
    return bool(await _owning_schedules(db, sess.id))


async def resume_after_schedule_loss(db: AsyncSession, schedule: ScheduledAction) -> uuid.UUID | None:
    """세션을 향한 예약이 실패·취소·만료됐을 때 호출한다. 그 예약만 소유자였다면 자동 이어가기를 한 번 세운다.

    예약이 있는 동안 후속을 만들지 않으므로, 예약이 사라진 뒤 아무도 이어받지 않는 유실을 막는다. 호출자는 예약
    상태를 이미 바꿨고 같은 트랜잭션에서 커밋한다. 무진전 후속 예산(MAX_UNCHANGED_CONTINUATIONS)을 그대로 쓴다."""
    if schedule.action_type != "send_message":
        return None
    try:
        sid = uuid.UUID(str((schedule.payload or {}).get("session_id")))
    except ValueError:
        return None
    if not (is_contract_resume_schedule(schedule) or is_self_schedule(schedule, sid)):
        return None  # 개인 예약은 소유자가 아니었으므로 사라져도 목표 후속을 되살리지 않는다.
    sess = await db.get(ChatSession, sid)
    if sess is None or sess.kind != SessionKind.orchestrator or sess.archived_at:
        return None
    await db.scalar(select(ChatSession.id).where(ChatSession.id == sid).with_for_update())
    current = await load(db, sid)
    state = current.get("state")
    if not state or state["status"] != "active" or await _owner_exists(db, sess, None):
        return None
    row = await db.get(AppSetting, key(sid))
    rec = dict(current)
    rec["updated_at"] = utcnow().isoformat()
    rec.pop("waiting_until", None)
    count = rec.get("unchanged_continuations", 0)
    if count >= MAX_UNCHANGED_CONTINUATIONS:
        _block(rec, state, "no_progress",
               f"No recorded progress after {MAX_UNCHANGED_CONTINUATIONS} automatic continuations; "
               "inspect execution evidence before resuming")
        row.value = rec
        return None
    rec["unchanged_continuations"] = count + 1
    reason = (schedule.last_error or schedule.status or "ended")[:200]
    prompt = _continuation_prompt(state, rec, header=f"[미완료 목표 자동 이어가기] 이 세션에 걸어 둔 예약 {schedule.id} 이(가) 실행되지 못하고 "
                                                      f"{schedule.status} 로 끝났다 ({reason}). 예약을 기다리지 말고 직접 이어가라.")
    continuation = _new_continuation(sess, None, rec, prompt, extra={"schedule_id": str(schedule.id)})
    db.add(continuation)
    await db.flush()
    row.value = rec
    return continuation.id


def _block(rec: dict, state: dict, kind: str, reason: str) -> None:
    rec["state"] = {**state, "status": "blocked", "blocker": reason[:2000]}
    rec["revision"] = rec["revision"] + 1
    rec["blocker_kind"] = kind
    rec.pop("waiting_until", None)


async def _usage_wait_until(db: AsyncSession, sess: ChatSession, run: Run, attempt: int) -> float:
    """한도·용량 대기의 재개 시각(unix 초). 오류 문구의 초기화 시각 → 모든 계정이 쉬는 경우 가장 빠른 해제 → 지수 백오프."""
    from ttakkari.db import sessionmaker
    from ttakkari.runs import profiles
    now = time.time()
    until = None
    try:
        until = profiles.reset_epoch_from(run.error)
        if until is None and (m := _RETRY_IN_RE.search(run.error or "")):
            until = now + int(m.group(1)) * (3600 if m.group(2).lower().startswith("h") else 60)
        if until is None:
            # 별도 세션: 빈 계정 목록이면 ensure_seeded 가 commit 하는데, 호출자 세션(after_run 의 행 잠금 트랜잭션)을
            # 거기서 끝내거나 begin_nested 안에서 닫힌 트랜잭션 오류를 내면 안 된다. 계정·cooldown 은 커밋된 값만 본다.
            async with sessionmaker()() as hint_db:
                free, blocked = await profiles.availability(hint_db, sess.engine, sess.pool)
            if not free and blocked:
                until = min(t for _, t in blocked)
    except Exception: # 힌트 조회 실패는 백오프로 대신한다.
        log.exception("usage wait hint failed for run %s", run.id)
    backoff = min(USAGE_WAIT_BASE_SECONDS * (2 ** attempt), USAGE_WAIT_MAX_SECONDS)
    wait = (until - now) if until else backoff
    return now + max(60.0, min(wait, USAGE_WAIT_MAX_SECONDS))


_RETRY_IN_RE = re.compile(r"(?:try again|retry) in (\d+)\s*(hours?|hrs?|minutes?|mins?)", re.IGNORECASE)


def _continuation_prompt(state: dict, current: dict, *, header: str, ev_line: str | None = None) -> str:
    pending = [r for r in state["requirements"] if r["status"] != "done"]
    done = [r["text"] for r in state["requirements"] if r["status"] == "done"]
    lines = [header]
    if ev_line:
        lines.append(f"- 직전 실행 성과: {ev_line}")
    lines.append(f"- 이미 완료된 항목: {', '.join(done) if done else '없음'} (재실행 금지)")
    lines.append("- 남은 미완료 항목: " + (", ".join(r["text"] for r in pending) if pending else "없음 — 완료 근거를 확인해 complete 로 체크포인트하거나 빠진 요구를 추가하라"))
    if rules := _constraints(current):
        lines.append("- 원본 금지·제약(그대로 지켜라): " + " / ".join(r[:120] for r in rules[:6]))
    if current.get("additional_requests"):
        lines.append("- 계약 이후 추가 요청이 있다. 범위를 합쳐 요구에 추가하라(영속 목표 계약의 additional_requests).")
    lines.append("다음 동작: " + state["next_action"])
    lines.append("현재 변경·완료 증거를 재사용하고 남은 항목에 집중하라. 전체 재분석/동일 검사 반복 금지.")
    return "\n".join(lines)


def _new_continuation(sess: ChatSession, run: Run | None, rec: dict, prompt: str, *, until: float | None = None,
                      extra: dict | None = None) -> Run:
    marker = {"source_run_id": str(run.id) if run else None, "revision": rec["revision"], **(extra or {})}
    return Run(session_id=sess.id, workspace_id=sess.workspace_id, engine=sess.engine, model=sess.model,
               effort=sess.effort, origin=RunOrigin.orchestrator, prompt=prompt, usage={"work_continuation": marker},
               resume_after=datetime.fromtimestamp(until, UTC) if until else None)


async def after_run(db: AsyncSession, sess: ChatSession, run: Run) -> uuid.UUID | None:
    """기획 Run 이 끝났을 때 계약을 정리하고 필요하면 후속 Run 하나를 만든다. 계약은 여기서 만들지 않는다.

    순서: 사용자 중단(최우선) → 증거로 진전 판정·저장 → 영구 실패는 blocked → 소유자(대기·실행 Run, 작업자, 예약) 있으면
    중복 생성 없이 종료 → 일시 실패 대기·재시도 / 성공 후 미완료 이어가기."""
    if sess.kind != SessionKind.orchestrator or sess.archived_at:
        return None
    await db.scalar(select(ChatSession.id).where(ChatSession.id == sess.id).with_for_update())
    current = await load(db, sess.id)
    state = current.get("state")
    if not state or state["status"] != "active":
        return None
    row = await db.get(AppSetting, key(sess.id))
    if run.status == RunStatus.cancelled:
        await _pause_on_cancel(db, sess, run, row, current, state)
        return None
    if run.status not in (RunStatus.succeeded, RunStatus.failed):
        return None  # interrupted Runs use existing restart recovery, not a new duplicate Run.

    # 진전 판정: 계약 체크포인트가 바뀌었거나 Run 이 실제로 파일·커밋·테스트를 바꿨다. 체크포인트를 건너뛴 성과도 복원한다.
    ev = await run_evidence(db, run)
    prev_fp = current.get("last_evidence_fingerprint")
    progressed = bool(ev["effective"] and ev["fingerprint"] != prev_fp) or bool(current.get("state_checkpoint_progressed"))
    rec = dict(current)
    if ev["effective"]:
        rec["last_evidence_fingerprint"] = ev["fingerprint"]
        files = [f for f in ev["files"] if f not in state.get("changed_files", [])]
        tests = [t for t in ev["tests"] if t not in state.get("validation", [])]
        if files or tests:
            state = {**state, "changed_files": (state.get("changed_files", []) + files)[:100],
                     "validation": (state.get("validation", []) + tests)[:100]}
            rec["state"] = state
            rec["revision"] = current["revision"] + 1
    rec["state_checkpoint_progressed"] = False
    if progressed:
        rec["unchanged_continuations"] = 0
        for counter in ("transient_retries", "transient_total", "usage_waits", "waiting_until"):
            rec.pop(counter, None)
    rec["updated_at"] = utcnow().isoformat()
    failed = run.status == RunStatus.failed
    kind = failure_kind(run.error) if failed else None

    if failed and kind == "permanent":
        reason = f"{run.engine} Run {run.id} failed: {run.error or 'unknown failure'}"
        _block(rec, state, "permanent_failure", reason)
        row.value = rec
        events.append(db, run, "work.blocked", {"reason": reason[:2000], "failure_kind": kind})
        return None
    if await _owner_exists(db, sess, run):
        # 예약이 소유자이면 다음 예약 시각을 대기로 보여 준다. 다른 소유자(실행 중 Run·작업자)만 있으면 그대로 둔다.
        if due := min((r.due_at for r in await _owning_schedules(db, sess.id)), default=None):
            rec["waiting_until"] = due.isoformat()
        row.value = rec
        return None

    until: float | None = None
    extra: dict = {}
    if failed:
        sig = error_signature(run.error)
        if kind in ("usage_limit", "model_capacity"):
            waits = rec.get("usage_waits", 0)
            if waits >= MAX_USAGE_WAITS:
                _block(rec, state, "usage_limit_exhausted",
                       f"{run.engine} still limited after {MAX_USAGE_WAITS} waits: {run.error or kind}")
                row.value = rec
                events.append(db, run, "work.blocked", {"reason": rec["state"]["blocker"], "failure_kind": kind})
                return None
            until = await _usage_wait_until(db, sess, run, waits)
            rec["usage_waits"] = waits + 1
            rec["waiting_until"] = datetime.fromtimestamp(until, UTC).isoformat()
            extra = {"transient": True, "failure_kind": kind, "wait_until": rec["waiting_until"]}
            header = (f"[미완료 목표 자동 이어가기] 직전 시도가 {kind} 로 중단됐다 ({(run.error or '')[:200]}). "
                      f"{rec['waiting_until']} 이후 재개한다(대기 {waits + 1}/{MAX_USAGE_WAITS}). 동일 실패 반복을 피하고 폴백 계정·엔진으로 계속하라.")
        else:
            retries = dict(rec.get("transient_retries") or {})
            total = rec.get("transient_total", 0)
            if retries.get(sig, 0) >= MAX_TRANSIENT_RETRIES or total >= MAX_TRANSIENT_TOTAL:
                _block(rec, state, "transient_exhausted",
                       f"{run.engine} Run {run.id} failed repeatedly without progress: {run.error or 'unknown failure'}")
                row.value = rec
                events.append(db, run, "work.blocked", {"reason": rec["state"]["blocker"], "failure_kind": kind})
                return None
            retries[sig] = retries.get(sig, 0) + 1
            rec["transient_retries"] = retries
            rec["transient_total"] = total + 1
            until = time.time() + TRANSIENT_BACKOFF_SECONDS * (2 ** total)
            extra = {"transient": True, "failure_kind": kind}
            header = (f"[미완료 목표 자동 이어가기] 직전 시도에서 일시적 {kind} 오류 발생 ({(run.error or 'transient failure')[:200]}). "
                      f"오류 지문 {sig} (시도 {retries[sig]}/{MAX_TRANSIENT_RETRIES}). 동일 실패 반복을 피하고 대체 방법으로 계속하라.")
        prompt = _continuation_prompt(state, rec, header=header)
    else:
        # 진전이 있으면 위에서 0 으로 돌렸다. 그 뒤 이어가는 후속부터 센다: 진전 뒤 후속 2회까지 허용, 3번째 무진전 턴에 blocked.
        count = rec.get("unchanged_continuations", 0)
        if count >= MAX_UNCHANGED_CONTINUATIONS:
            _block(rec, state, "no_progress",
                   f"No recorded progress after {MAX_UNCHANGED_CONTINUATIONS} automatic continuations; "
                   "inspect execution evidence before resuming")
            row.value = rec
            events.append(db, run, "work.blocked", {"reason": rec["state"]["blocker"]})
            return None
        rec["unchanged_continuations"] = count + 1
        prompt = _continuation_prompt(state, rec, header="[미완료 목표 자동 이어가기] 중간 Gate/턴 종료는 목표 완료가 아니다.",
                                      ev_line=evidence_line(ev))
    continuation = _new_continuation(sess, run, rec, prompt, until=until, extra=extra)
    db.add(continuation)
    await db.flush()
    row.value = rec
    events.append(db, continuation, "run.status", {"status": RunStatus.queued, "work_continuation": True,
                                                   **({"resume_after": rec.get("waiting_until")} if until else {})})
    events.append(db, run, "work.continued", {"run_id": str(continuation.id), "revision": rec["revision"],
                                              **({"transient": True, "failure_kind": kind} if failed else {})})
    if kind in ("usage_limit", "model_capacity"):
        events.append(db, run, "work.waiting", {"until": rec["waiting_until"], "failure_kind": kind})
    return continuation.id
