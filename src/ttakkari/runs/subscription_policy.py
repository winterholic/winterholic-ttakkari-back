"""Clock-injected Codex reset policy. No I/O and no periodic provider polling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ttakkari.runs.usage import Usage

NORMAL_SECONDS = 12 * 3600
RESET_DELAY_SECONDS = 10
VERIFY_DELAYS = (30, 60, 120, 240, 450)
VERIFY_MAX_SECONDS = 15 * 60


@dataclass
class Decision:
    state: dict[str, Any]
    activate: bool = False
    cycle: int | None = None


def plan(prior: dict, observation: Usage, *, now: float, user_used: bool = False) -> Decision:
    state = {"provider": "codex", "auto_enabled": True, **prior}
    state["last_checked_at"] = now
    state["last_error"] = observation.error
    state["next_check_at"] = now + NORMAL_SECONDS
    state["next_activation_at"] = None
    weekly = next((w for w in observation.windows if w.key == "seven_day"), None)
    fresh = (not observation.error and observation.source == "api" and weekly is not None
             and observation.updated_at is not None)
    old_reset = state.get("reset_at")
    new_reset = weekly.resets_at if fresh else None
    due = old_reset is not None and old_reset <= now
    handled = old_reset is not None and state.get("handled_reset_at") == old_reset
    decision = Decision(state)

    if due and not handled:
        if now < old_reset + RESET_DELAY_SECONDS:
            state["next_check_at"] = old_reset + RESET_DELAY_SECONDS
            state["last_check_outcome"] = "waiting_for_reset"
        else:
            current = fresh and observation.updated_at >= old_reset
            result = None
            if state.get("dispatched_cycle") == old_reset:
                result = "already_dispatched"
            elif user_used:
                result = "already_used"
            elif current and new_reset and new_reset > old_reset:
                # Zero is rounded by the service. A future new window might have
                # been opened by a tiny external request; never guess it unused.
                result = "already_used" if weekly.percent > 0 else "already_active_or_unverifiable"
            elif (current and new_reset == old_reset and weekly.percent == 0
                  and observation.ordinary_usage_allowed is True):
                if state["auto_enabled"]:
                    decision.activate, decision.cycle = True, old_reset
                    result = "reset_confirmed"
                else:
                    result = "auto_disabled"

            if result is not None:
                state["last_check_outcome"] = result
                # Dispatch is not recorded here; the durable service must commit
                # its attempt marker before crossing the external inference boundary.
                if not decision.activate:
                    state["handled_reset_at"] = old_reset
                    state["handled_outcome"] = result
                state.pop("verify_attempts", None)
                state.pop("verify_started_at", None)
            else:
                attempts = state.get("verify_attempts", 0)
                started = state.get("verify_started_at", now)
                if attempts >= len(VERIFY_DELAYS) or now - started >= VERIFY_MAX_SECONDS:
                    state["handled_reset_at"] = old_reset
                    state["handled_outcome"] = "verification_exhausted"
                    state["last_check_outcome"] = "verification_exhausted"
                else:
                    state["verify_started_at"] = started
                    state["verify_attempts"] = attempts + 1
                    state["next_check_at"] = min(now + VERIFY_DELAYS[attempts], started + VERIFY_MAX_SECONDS)
                    state["last_check_outcome"] = "awaiting_reset_confirmation"
    else:
        state["last_check_outcome"] = "observed" if fresh else "observation_failed"

    if fresh and new_reset is not None and (not due or new_reset > old_reset):
        if old_reset != new_reset:
            state.pop("verify_attempts", None)
            state.pop("verify_started_at", None)
        state["reset_at"] = new_reset
    target = state.get("reset_at")
    if target is not None and target + RESET_DELAY_SECONDS > now and state.get("handled_reset_at") != target:
        state["next_check_at"] = min(state["next_check_at"], target + RESET_DELAY_SECONDS)
        if state["auto_enabled"]:
            state["next_activation_at"] = target + RESET_DELAY_SECONDS
    state["last_observation"] = observation.as_dict()
    return decision
