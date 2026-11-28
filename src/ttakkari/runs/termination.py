"""Run 종료 원인 기록과 판정(2단계 안정화 Track C).

원칙은 하나다. 사용자 의도는 **취소를 요청한 쪽이 요청 시점에 남긴 기록**으로만 안다. 엔진 결과 문자열·종료 코드·Run.error 는
원인이 아니라 결과라서 의도를 추정하는 데 쓰지 않는다. 요청 기록이 없으면 사용자 중단이 아니라 원인 미확인이다.

기록 위치(스키마 변경 없음):
- 요청 시점: 감사 로그 `run.cancel_requested`(target=run). 요청자가 자기 트랜잭션과 별개로 먼저 커밋한다. Run 이벤트는 그 Run 을
  실행하는 태스크만 쓰므로(runs/events.py) 요청자가 RunEvent 를 직접 쓰지 않는다.
- 종료 시점: Run 을 마감하는 쪽이 `run.cancel_requested` 이벤트로 요청을 옮겨 적고, `run.finished` 이벤트의 `termination` 과
  `Run.usage["termination"]` 에 판정 결과를 남긴다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.audit import record
from ttakkari.models import AuditLog, Run, RunEvent, RuntimeCommand, utcnow

log = logging.getLogger("ttakkari.termination")

REQUEST_ACTION = "run.cancel_requested"
LEGACY_USER_ACTION = "run.cancel"  # 요청 기록 도입 전 API 가 요청 뒤에 남기던 감사 로그. 사용자 API 호출이었다.
RESTART_INTERRUPTION = "서버 종료로 중단"  # RunManager 가 서버 종료 때 직접 쓰는 값(엔진이 쓰는 문자열이 아니다).
ENGINE_INTERRUPTED = "interrupted"

# 의도를 알 수 있는 요청 범위. global 은 "전부 중지"라 목표도 함께 멈추는 명시 중단이다.
SCOPES = ("run", "worker", "global", "continuation")
_AUDIT_ACTOR = {"user": "user", "orchestrator": "agent", "schedule": "system", "system": "system"}

# 원인별 복구 정책: (recovery, auto_recoverable)
_RECOVERY: dict[str, tuple[str, bool]] = {
    "user_stop": ("none", False),
    "global_stop": ("none", False),
    "planner_stop": ("none", False),
    "system_stop": ("none", False),
    "scheduled_stop": ("review", False),
    "cancel_unattributed": ("review", False),
    "engine_interrupted": ("automatic", True),
    "unknown": ("conditional", True),
    "server_shutdown": ("automatic", True),
    "generation_drain": ("automatic", True),
    "lease_lost": ("conditional", True),
    "timeout": ("automatic", True),
    "usage_limit": ("automatic", True),
    "model_capacity": ("automatic", True),
    "process_signal": ("automatic", True),
    "engine_failure": ("automatic", True),
    "permanent_failure": ("none", False),
}
_GOAL_ACTION = {"user_stop": "pause", "global_stop": "pause", "planner_stop": "isolate", "system_stop": "isolate",
                "scheduled_stop": "review", "cancel_unattributed": "review"}


@dataclass(frozen=True)
class CancelRequest:
    """취소를 요청하는 쪽이 채운다. user_intent 는 인증된 사용자 API 호출일 때만 True 다."""

    actor: str  # user | orchestrator | schedule | system
    source: str  # 호출 경로 이름(예: api.run_cancel)
    reason: str = ""
    user_intent: bool = False
    scope: str = "run"  # run | worker | global | continuation
    actor_id: str | None = None  # 요청한 기획 세션·예약 id

    def detail(self, *, run_id: str, session_id: str, requested_at: str | None = None) -> dict[str, Any]:
        # 키 이름이 session_id 이면 이벤트 저장 때 마스킹돼(oplog/scrub) 대상 세션을 잃는다. 그래서 session 이라 부른다.
        return {"run_id": run_id, "session": session_id, "actor": self.actor, "actor_id": self.actor_id,
                "source": self.source, "scope": self.scope, "reason": (self.reason or "")[:1000],
                "user_intent": bool(self.user_intent),
                "requested_at": requested_at or datetime.now(UTC).isoformat()}


async def record_request(db: AsyncSession, run: Run | None, req: CancelRequest) -> None:
    """요청 시점 기록. 호출자가 커밋한다. 취소 동작보다 먼저 커밋돼야 마감 쪽이 읽을 수 있다."""
    if run is None:
        return
    record(db, actor=_AUDIT_ACTOR.get(req.actor, "system"), action=REQUEST_ACTION, target_type="run",
           target_id=run.id, detail=req.detail(run_id=str(run.id), session_id=str(run.session_id)))
    await db.flush()


async def _requests_by_run(db: AsyncSession, run_ids: list) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {str(r): [] for r in run_ids}
    legacy: dict[str, list[dict]] = {str(r): [] for r in run_ids}
    if not run_ids:
        return out
    rows = await db.scalars(select(AuditLog).where(
        AuditLog.action.in_((REQUEST_ACTION, LEGACY_USER_ACTION)), AuditLog.target_type == "run",
        AuditLog.target_id.in_([str(r) for r in run_ids])).order_by(AuditLog.id))
    for row in rows:
        if row.action == REQUEST_ACTION and row.outcome == "ok":
            out[row.target_id].append(dict(row.detail or {}))
        elif row.action == LEGACY_USER_ACTION and row.outcome == "ok":
            legacy[row.target_id].append({"run_id": row.target_id, "session": None, "actor": "user", "actor_id": None,
                                          "source": "api.run_cancel", "scope": "run", "user_intent": True,
                                          "reason": "", "legacy_audit": True,
                                          "requested_at": row.at.replace(tzinfo=UTC).isoformat() if row.at else None})
    for rid, found in out.items():
        if not found:
            out[rid] = legacy[rid]  # 새 기록이 있으면 같은 요청의 옛 감사 줄은 쓰지 않는다
    return out


async def load_requests(db: AsyncSession, run: Run) -> list[dict]:
    return (await _requests_by_run(db, [run.id]))[str(run.id)]


def explicit_stops(requests: list[dict]) -> list[dict]:
    """목표까지 멈추는 명시 중단 요청: 사용자 API 호출이거나 전역 중지."""
    return [r for r in requests if r.get("user_intent") or r.get("scope") == "global"]


def _pick(requests: list[dict]) -> dict | None:
    if not requests:
        return None
    stops = explicit_stops(requests)
    return (stops or requests)[-1]


def _cancel_cause(req: dict) -> str:
    if req.get("scope") == "global":
        return "global_stop"
    if req.get("user_intent"):
        return "user_stop"
    if req.get("actor") == "orchestrator":
        return "planner_stop"
    if req.get("actor") == "schedule":
        return "scheduled_stop"
    return "system_stop"


def classify(status: Any, *, error: str | None, requests: list[dict], facts: dict[str, Any]) -> dict[str, Any]:
    """종료 원인 판정. 입력은 요청 기록과 서버가 관측한 사실(facts)뿐이고 error 문장은 분류에 쓰지 않는다(failed 의 한도·용량
    분류는 기존 failure_kind 를 그대로 쓴다).

    facts: cancel_flag(핸들의 취소 요청 플래그), cancel_command(영속 cancel 명령 존재), engine_error(엔진이 낸 error 이벤트),
    timed_out, shutting_down, draining, lease_lost, exit_code.
    """
    status = getattr(status, "value", status)
    req = _pick(requests)
    evidence: list[str] = []
    if req:
        evidence.append("cancel_request")
    if facts.get("cancel_flag"):
        evidence.append("cancel_flag")
    if facts.get("cancel_command"):
        evidence.append("cancel_command")
    engine_interrupted = str(facts.get("engine_error") or "").strip().lower() == ENGINE_INTERRUPTED
    if engine_interrupted:
        evidence.append("engine_error:interrupted")
    cause = "unknown"
    if status == "cancelled":
        if req:
            cause = _cancel_cause(req)
        elif facts.get("cancel_flag") or facts.get("cancel_command"):
            cause = "cancel_unattributed"  # 누군가 취소를 요청했지만 주체·출처가 남지 않았다
        elif engine_interrupted:
            cause = "engine_interrupted"
    elif status == "interrupted":
        if facts.get("lease_lost"):
            cause = "lease_lost"
        elif facts.get("shutting_down") or error == RESTART_INTERRUPTION:
            cause = "generation_drain" if facts.get("draining") else "server_shutdown"
    elif status == "failed":
        from ttakkari.orchestrator.goal_signals import failure_kind
        kind = failure_kind(error)
        exit_code = facts.get("exit_code")
        if facts.get("timed_out"):
            cause = "timeout"
        elif kind in ("usage_limit", "model_capacity"):
            cause = kind
        elif isinstance(exit_code, int) and exit_code < 0:
            cause = "process_signal"
        else:
            cause = "permanent_failure" if kind == "permanent" else "engine_failure"
    recovery, auto = _RECOVERY.get(cause, ("conditional", True))
    return {"cause": cause, "user_intent": bool(req.get("user_intent")) if req else False,
            "initiator": {"actor": req.get("actor"), "actor_id": req.get("actor_id")} if req else None,
            "source": req.get("source") if req else None, "requested_at": req.get("requested_at") if req else None,
            "reason": req.get("reason") if req else None, "scope": req.get("scope") if req else None,
            "request_count": len(requests), "auto_recoverable": auto, "recovery": recovery, "evidence": evidence}


def goal_action(term: dict[str, Any]) -> str:
    """pause(목표 일시중지+자동 후속 취소) | isolate(목표 유지, 단위 중단만) | review(검토 대상으로 멈춤) | recover(복구)."""
    return _GOAL_ACTION.get(term.get("cause"), "recover")


def process_info(facts: dict[str, Any]) -> dict[str, Any]:
    code = facts.get("exit_code")
    started = code is not None or bool(facts.get("process_started"))
    return {"started": started, "exit_code": code, "signal": -code if isinstance(code, int) and code < 0 else None,
            "terminated": (True if code is not None else facts.get("process_terminated")) if started else None,
            "group_alive": facts.get("group_alive")}


async def collect_facts(db: AsyncSession, run: Run, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """핸들이 준 사실에 DB 로 확인되는 사실(엔진 error 이벤트, 영속 cancel 명령)을 보탠다."""
    facts = dict(base or {})
    if "engine_error" not in facts:
        msg = await db.scalar(select(RunEvent.payload["message"].as_string()).where(
            RunEvent.run_id == run.id, RunEvent.type == "error").order_by(RunEvent.seq.desc()).limit(1))
        if msg:
            facts["engine_error"] = msg[:200]
    if not facts.get("cancel_command"):
        facts["cancel_command"] = bool(await db.scalar(select(RuntimeCommand.id).where(
            RuntimeCommand.run_id == run.id, RuntimeCommand.kind == "cancel").limit(1)))
    return facts


async def finalize(db: AsyncSession, run: Run, status: Any, error: str | None,
                   base_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run 을 마감하는 쪽이 부른다. 요청을 이벤트로 옮겨 적고 판정을 usage 에 남긴 뒤 판정을 돌려준다. 성공은 판정하지 않는다."""
    from ttakkari.runs import events
    status = getattr(status, "value", status)
    # 조회가 Run 행 UPDATE 를 일찍 flush 하면 동시에 끝나는 다른 Run 의 세션 잠금과 순서가 엇갈려 교착한다. 마감 흐름의
    # 잠금 순서(세션 잠금 → Run 쓰기)를 지키려고 판정 조회 동안은 flush 하지 않는다.
    with db.no_autoflush:
        requests = await load_requests(db, run)
        for req in requests:
            events.append(db, run, "run.cancel_requested", req)
        if status == "succeeded":
            return {}
        facts = await collect_facts(db, run, base_facts)
    term = classify(status, error=error, requests=requests, facts=facts)
    term.update(status=status, target={"run_id": str(run.id), "session": str(run.session_id)},
                process=process_info(facts), resolved_at=utcnow().isoformat())
    run.usage = {**(run.usage or {}), "termination": term}
    return term


async def resolve(db: AsyncSession, run: Run) -> dict[str, Any]:
    """저장된 판정이 있으면 그대로, 없으면(옛 Run) 요청 기록과 이벤트로 도출한다. 의도는 도출 때도 요청 기록으로만 정한다."""
    stored = (run.usage or {}).get("termination")
    if isinstance(stored, dict) and stored.get("cause"):
        return stored
    facts = await collect_facts(db, run)
    return classify(run.status, error=run.error, requests=await load_requests(db, run), facts=facts)


def simple_record(cause: str, *, actor: str, source: str, reason: str, run: Run, scope: str = "continuation") -> dict[str, Any]:
    """프로세스가 없는 Run 을 시스템이 직접 마감할 때의 기록."""
    recovery, auto = _RECOVERY[cause]
    return {"cause": cause, "user_intent": False, "initiator": {"actor": actor, "actor_id": None}, "source": source,
            "requested_at": datetime.now(UTC).isoformat(), "reason": reason[:1000], "scope": scope, "request_count": 0,
            "auto_recoverable": auto, "recovery": recovery, "evidence": ["system_action"], "status": "cancelled",
            "target": {"run_id": str(run.id), "session": str(run.session_id)},
            "process": process_info({}), "resolved_at": utcnow().isoformat()}


async def cancellation_stats(db: AsyncSession, *, since: datetime | None = None, limit: int = 20000) -> dict[str, Any]:
    """기존 Run 기록만으로 cancelled Run 의 종료 원인 분포와 미확인 비율을 센다(§12). 저장된 판정이 없는 옛 Run 도 센다.

    미확인 = 원인을 못 가른 `unknown` 과 취소 요청은 있었으나 주체가 없는 `cancel_unattributed`.
    """
    query = select(Run).where(Run.status == "cancelled").order_by(Run.created_at.desc()).limit(limit)
    if since is not None:
        query = query.where(Run.created_at >= since)
    runs = list(await db.scalars(query))
    ids = [r.id for r in runs]
    requests: dict[str, list[dict]] = {}
    errors: dict[Any, str] = {}
    commands: set = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        requests.update(await _requests_by_run(db, chunk))
        for rid, seq, msg in (await db.execute(
                select(RunEvent.run_id, RunEvent.seq, RunEvent.payload["message"].as_string()).where(
                    RunEvent.run_id.in_(chunk), RunEvent.type == "error").order_by(RunEvent.seq))).all():
            errors[rid] = msg or ""
        commands.update(await db.scalars(select(RuntimeCommand.run_id).where(
            RuntimeCommand.run_id.in_(chunk), RuntimeCommand.kind == "cancel")))
    by_cause: dict[str, int] = {}
    unattributed: list[str] = []
    for run in runs:
        stored = (run.usage or {}).get("termination")
        if isinstance(stored, dict) and stored.get("cause"):
            cause = stored["cause"]
        else:
            cause = classify("cancelled", error=run.error, requests=requests.get(str(run.id), []),
                             facts={"engine_error": errors.get(run.id), "cancel_command": run.id in commands})["cause"]
        by_cause[cause] = by_cause.get(cause, 0) + 1
        if cause in ("unknown", "cancel_unattributed"):
            unattributed.append(str(run.id))
    unknown = len(unattributed)
    return {"cancelled": len(runs), "by_cause": dict(sorted(by_cause.items())), "unknown": unknown,
            "unknown_ratio": round(unknown / len(runs), 4) if runs else 0.0, "unattributed_runs": unattributed[:50]}
