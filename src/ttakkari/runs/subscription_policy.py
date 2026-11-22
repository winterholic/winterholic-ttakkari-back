"""Clock-injected subscription reset policy. No I/O and no periodic provider polling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ttakkari.runs.usage import Usage, Window

NORMAL_SECONDS = 12 * 3600
RESET_DELAY_SECONDS = 10
VERIFY_DELAYS = (30, 60, 120, 240, 450)
VERIFY_MAX_SECONDS = 15 * 60
WEEK_SECONDS = 7 * 24 * 3600
UNUSED_WINDOW_TOLERANCE_SECONDS = 120
ANTIGRAVITY_WEEKLY = "agy_gemini_models_weekly"
ANTIGRAVITY_SHORT = "agy_gemini_models_5h"
SUBSCRIPTION_ENGINES = {"codex", "antigravity"}


def default_state(provider: str) -> dict:
    return {"provider": provider, "auto_enabled": provider == "codex"}


def antigravity_has_capacity(observation: Usage) -> bool:
    windows = {w.key: w for w in observation.windows}
    return (not observation.error and observation.source == "antigravity_cli"
            and observation.updated_at is not None
            and all(key in windows and windows[key].resets_at is not None
                    and windows[key].resets_at > observation.updated_at
                    and windows[key].remaining_percent is not None
                    and windows[key].remaining_percent > 0
                    for key in (ANTIGRAVITY_WEEKLY, ANTIGRAVITY_SHORT)))


def _unstarted_antigravity_window(weekly: Window, observed_at: float) -> bool:
    return (weekly.remaining_percent == 100 and weekly.resets_at is not None
            and abs(weekly.resets_at - (observed_at + WEEK_SECONDS)) < UNUSED_WINDOW_TOLERANCE_SECONDS)


@dataclass
class Decision:
    state: dict[str, Any]
    activate: bool = False
    cycle: int | None = None


def _unused_new_window(weekly: Window, observation: Usage) -> bool:
    # 2026-11-21 production: an unused window reports resetsAt as observation time + length; earlier means it started.
    return (weekly.percent == 0 and observation.ordinary_usage_allowed is True
            and abs(weekly.resets_at - (observation.updated_at + WEEK_SECONDS)) < UNUSED_WINDOW_TOLERANCE_SECONDS)


def plan(prior: dict, observation: Usage, *, now: float, user_used: bool = False) -> Decision:
    provider = prior.get("provider", "codex")
    antigravity = provider == "antigravity"
    state = {**default_state(provider), **prior}
    state["last_checked_at"] = now
    state["last_error"] = observation.error
    state["next_check_at"] = now + NORMAL_SECONDS
    state["next_activation_at"] = None
    weekly_key = ANTIGRAVITY_WEEKLY if antigravity else "seven_day"
    weekly = next((w for w in observation.windows if w.key == weekly_key), None)
    source = "antigravity_cli" if antigravity else "api"
    fresh = (not observation.error and observation.source == source and weekly is not None
             and observation.updated_at is not None)
    old_reset = state.get("reset_at")
    new_reset = weekly.resets_at if fresh else None
    unstarted = antigravity and fresh and _unstarted_antigravity_window(weekly, observation.updated_at)
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
            confirmed = False
            if state.get("dispatched_cycle") == old_reset:
                result = "already_dispatched"
            elif user_used:
                result = "already_used"
            elif antigravity:
                if current and new_reset and new_reset > old_reset:
                    if unstarted:
                        confirmed = antigravity_has_capacity(observation)
                    else:
                        result = ("already_used" if weekly.remaining_percent is not None
                                  and weekly.remaining_percent < 100 else "already_active_or_unverifiable")
            elif current and new_reset and new_reset > old_reset:
                if _unused_new_window(weekly, observation):
                    confirmed = True
                else:
                    # Zero is rounded by the service. A window that already started might
                    # have been opened by a tiny external request; never guess it unused.
                    result = "already_used" if weekly.percent > 0 else "already_active_or_unverifiable"
            elif (current and new_reset == old_reset and weekly.percent == 0
                  and observation.ordinary_usage_allowed is True):
                confirmed = True
            if confirmed:
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

    # An unused Antigravity window reports a rolling observation + 7 days.
    # Retain the previous real boundary for manual activation and retries.
    if fresh and not unstarted and new_reset is not None and (not due or new_reset > old_reset):
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
