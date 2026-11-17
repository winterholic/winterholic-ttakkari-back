"""Durable subscription checks on the existing ScheduledAction queue.

EngineAccount's row lock serializes state creation, rescheduling and dispatch
claims across processes. A committed dispatch marker fences uncertain external
outcomes: a crash after that marker never replays the model request.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select

from ttakkari import generation
from ttakkari.db import sessionmaker
from ttakkari.models import (
    AppSetting,
    EngineAccount,
    Run,
    RunStatus,
    RuntimeLease,
    ScheduledAction,
)
from ttakkari.runs import profiles, usage
from ttakkari.runs.codex_subscription import read_usage
from ttakkari.runs.subscription_policy import NORMAL_SECONDS, plan

ACTION = "subscription_check"
DISPATCH_UNKNOWN_SECONDS = 120
log = logging.getLogger(__name__)


def state_key(account_id: uuid.UUID) -> str:
    return f"subscription:{account_id}"


async def _locked(db, account_id):
    account = await db.get(EngineAccount, account_id, with_for_update=True, populate_existing=True)
    if account is None or account.engine != "codex":
        raise ValueError("Codex account not found")
    row = await db.get(AppSetting, state_key(account_id), populate_existing=True)
    if row is None:
        row = AppSetting(key=state_key(account_id), value={"provider": "codex", "auto_enabled": True})
        db.add(row)
        await db.flush()
    return account, row


async def get_state(account_id: uuid.UUID) -> dict:
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, state_key(account_id))
        return dict(row.value) if row else {"provider": "codex", "auto_enabled": True}


async def _reserve(db, account_id, state, due, *, executing_id=None):
    old_id = state.get("schedule_id")
    if old_id and old_id != str(executing_id):
        old = await db.get(ScheduledAction, uuid.UUID(old_id))
        if old and old.status in ("pending", "leased"):
            old.status = "cancelled"
    item = ScheduledAction(action_type=ACTION, payload={"account_id": str(account_id)},
                           due_at=datetime.fromtimestamp(due, UTC), status="pending", max_attempts=3,
                           description="Codex 구독 상태 확인")
    db.add(item)
    await db.flush()
    state["schedule_id"] = str(item.id)
    state["next_check_at"] = due
    await generation.wake(db)


async def ensure(account_id: uuid.UUID, *, now: float, recover: bool = False) -> dict:
    async with sessionmaker()() as db:
        account, row = await _locked(db, account_id)
        state = dict(row.value)
        if not (profiles.is_on(account) or profiles.is_on(account, "work")):
            return state
        item = await db.get(ScheduledAction, uuid.UUID(state["schedule_id"])) if state.get("schedule_id") else None
        usable = item is not None and item.status in ("pending", "leased")
        # Startup refresh is coalesced across near-simultaneous generations.
        recovery_due = recover and now - state.get("last_checked_at", 0) > 60
        if not usable or (recovery_due and item.status == "pending" and item.due_at.timestamp() > now):
            # A repeatedly failed handler must not create an endless immediate
            # provider retry loop. Missing jobs and startup recovery are immediate.
            due = now + NORMAL_SECONDS if item is not None and item.status == "failed" and not recover else now
            await _reserve(db, account_id, state, due)
        row.value = state
        await db.commit()
        return state


async def set_enabled(account_id: uuid.UUID, enabled: bool, *, now: float) -> dict:
    async with sessionmaker()() as db:
        _, row = await _locked(db, account_id)
        state = {**row.value, "auto_enabled": enabled, "next_activation_at": None}
        if enabled and state.get("handled_outcome") == "auto_disabled" and state.get("dispatched_cycle") != state.get("handled_reset_at"):
            state.pop("handled_reset_at", None)
            state.pop("handled_outcome", None)
        # Recheck on either change; an old leased payload becomes superseded.
        await _reserve(db, account_id, state, now)
        row.value = state
        await db.commit()
        return state


async def _user_used(db, account: EngineAccount, boundary: int | None) -> bool:
    if boundary is None:
        return False
    since = datetime.fromtimestamp(boundary, UTC)
    running = select(RuntimeLease.run_id).where(RuntimeLease.account_id == account.id)
    row = await db.get(AppSetting, state_key(account.id))
    profile_names = {account.name, *(row.value.get("user_used_profile_aliases", []) if row else [])}
    return await db.scalar(select(Run.id).where(
        Run.engine == "codex",
        or_((Run.profile.in_(profile_names)) & or_(Run.started_at >= since, Run.status == RunStatus.running),
            (Run.id.in_(running)) & (Run.status == RunStatus.running)),
    ).limit(1)) is not None


async def activate(config_dir: str | None, *, preflight: usage.Usage) -> dict:
    from ttakkari.runs.codex_subscription import activate_subscription
    return await activate_subscription(config_dir, preflight=preflight)


async def notify_result(account_id: uuid.UUID, cycle: int, outcome: dict) -> None:
    from ttakkari.push.service import PushKind, notify
    title = "Codex 주간 활성화 요청 완료" if outcome.get("status") == "succeeded" else "Codex 주간 활성화 확인 필요"
    try:
        await notify(PushKind.subscription, title, "계정 관리에서 실행 결과와 예약을 확인해 주세요.",
                     url="/accounts", dedupe_key=f"subscription:{account_id}:{cycle}")
    except Exception as exc:  # noqa: BLE001 -- delivery must not replay the model call
        # Delivery failure must not replay a completed inference request.
        log.warning("subscription notification failed: %s", type(exc).__name__)


async def check(account_id: uuid.UUID, *, now: float, schedule_id: str | None = None,
                manual_activation: bool = False) -> dict:
    async with sessionmaker()() as db:
        account, row = await _locked(db, account_id)
        state = dict(row.value)
        if schedule_id is not None and state.get("schedule_id") != str(schedule_id):
            return {"status": "superseded"}
        if (manual_activation and state.get("handled_outcome") in {"auto_disabled", "verification_exhausted"}
                and state.get("dispatched_cycle") != state.get("handled_reset_at")):
            # Explicit user retry may reopen a never-dispatched cycle, while the
            # irreversible inference fence remains authoritative.
            for field in ("handled_reset_at", "handled_outcome", "verify_attempts", "verify_started_at"):
                state.pop(field, None)
        if not (profiles.is_on(account) or profiles.is_on(account, "work")):
            state["next_check_at"] = state["next_activation_at"] = None
            row.value = state
            await db.commit()
            return {"status": "account_disabled"}
        result = state.get("last_result") or {}
        recovered_unknown = False
        if result.get("status") == "dispatching":
            if now - state.get("dispatch_started_at", now) < DISPATCH_UNKNOWN_SECONDS:
                await _reserve(db, account_id, state, now + DISPATCH_UNKNOWN_SECONDS, executing_id=schedule_id)
                row.value = state
                await db.commit()
                return {"status": "already_dispatching"}
            state["last_result"] = {**result, "status": "unknown", "reason": "dispatch outcome unavailable after restart"}
            recovered_unknown = True
        observed = await read_usage(account.config_dir)
        # Cache consumers see the same observation; the normal usage sink owns history.
        usage.publish_live_observation(account_id, observed)
        user_used = await _user_used(db, account, state.get("reset_at"))
        original_enabled = state.get("auto_enabled", True)
        decision = plan({**state, "auto_enabled": original_enabled or manual_activation}, observed,
                        now=now, user_used=user_used)
        state = {**decision.state, "auto_enabled": original_enabled}
        if not original_enabled:
            state["next_activation_at"] = None
        if decision.activate and any(w.percent >= 100 for w in observed.windows):
            decision.activate = False
            state["last_check_outcome"] = "rate_limit"
        if not decision.activate:
            await _reserve(db, account_id, state, state["next_check_at"], executing_id=schedule_id)
            row.value = state
            await db.commit()
            if recovered_unknown:
                await notify_result(account_id, state["dispatched_cycle"], state["last_result"])
            elif state["last_check_outcome"] == "verification_exhausted":
                await notify_result(account_id, state["handled_reset_at"], {"status": "verification_exhausted"})
            return {"status": state["last_check_outcome"]}
        # COMMIT BEFORE INFERENCE. Do not clear this marker after unknown errors.
        state["dispatched_cycle"] = state["handled_reset_at"] = decision.cycle
        state["handled_outcome"] = "dispatched"
        state["dispatch_started_at"] = now
        state["last_result"] = {"status": "dispatching", "cycle": decision.cycle,
                                "started_at": now, "before": observed.as_dict()}
        await _reserve(db, account_id, state, now + NORMAL_SECONDS, executing_id=schedule_id)
        row.value = state
        config_dir = account.config_dir
        await db.commit()

    outcome = await activate(config_dir, preflight=observed)
    after = await read_usage(config_dir)
    usage.publish_live_observation(account_id, after)
    async with sessionmaker()() as db:
        account, row = await _locked(db, account_id)
        state = dict(row.value)
        state["last_result"] = {**state["last_result"], **outcome, "after": after.as_dict()}
        # Use the actual post-call clock: never backdate an observation to its timer.
        finished = after.updated_at or now
        next_decision = plan(state, after, now=finished)
        state = next_decision.state
        await _reserve(db, account_id, state, state["next_check_at"])
        row.value = state
        await db.commit()
    await notify_result(account_id, decision.cycle, outcome)
    return outcome
