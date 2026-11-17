"""Real PostgreSQL integration: reservations, concurrency, recovery and dispatch fence."""
import asyncio
import importlib

from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting, EngineAccount, ScheduledAction
from ttakkari.runs.usage import Usage, Window


def monitor():
    name = "ttakkari.runs.subscription_monitor"
    assert importlib.util.find_spec(name) is not None, "durable subscription monitor is not implemented"
    return importlib.import_module(name)


async def account(name="subscription test"):
    async with sessionmaker()() as db:
        a = EngineAccount(name=name, engine="codex", enabled=True)
        db.add(a)
        await db.commit()
        return a.id


def sample(at, reset=1000, percent=20):
    return Usage(windows=[Window("five_hour", "5시간", 0, 18000),
                          Window("seven_day", "주간", percent, reset)],
                 source="api", updated_at=at, login="ok", plan="plus", ordinary_usage_allowed=True)


async def test_ensure_persists_one_reservation_and_recovers_it_after_restart():
    m, aid = monitor(), await account()
    await asyncio.gather(m.ensure(aid, now=500), m.ensure(aid, now=500))
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(ScheduledAction).where(ScheduledAction.status == "pending")))
        assert len(rows) == 1 and rows[0].due_at.timestamp() == 500
        await db.delete(rows[0])
        await db.commit()
    await m.ensure(aid, now=600)
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(ScheduledAction).where(ScheduledAction.status == "pending")))
        assert len(rows) == 1 and rows[0].due_at.timestamp() == 600


async def test_check_replaces_exact_timer_and_old_leased_payload_is_ignored(monkeypatch):
    m, aid = monitor(), await account()
    calls = []
    value = {"reset": 1000}

    async def read(*args, **kwargs):
        calls.append("read")
        return sample(500, reset=value["reset"])

    monkeypatch.setattr(m, "read_usage", read)
    await m.ensure(aid, now=500)
    first = await m.get_state(aid)
    await m.check(aid, schedule_id=first["schedule_id"], now=500)
    state = await m.get_state(aid)
    assert state["next_check_at"] == 1010
    value["reset"] = 2000
    await m.check(aid, now=600)
    changed = await m.get_state(aid)
    assert changed["next_check_at"] == 2010
    assert changed["schedule_id"] != state["schedule_id"]
    result = await m.check(aid, schedule_id=state["schedule_id"], now=1010)
    assert result["status"] == "superseded" and len(calls) == 2


async def test_concurrent_checks_dispatch_once_and_persist_before_external_call(monkeypatch):
    m, aid = monitor(), await account()
    now = {"value": 500}
    dispatched = []

    async def read(*args, **kwargs):
        return sample(now["value"], reset=605810 if dispatched else 1000,
                      percent=0 if now["value"] > 1000 else 20)

    async def activate(*args, **kwargs):
        committed = await m.get_state(aid)
        assert committed["dispatched_cycle"] == 1000
        dispatched.append("exec")
        await asyncio.sleep(0.01)
        return {"status": "succeeded", "tokens": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(m, "read_usage", read)
    monkeypatch.setattr(m, "activate", activate)
    await m.ensure(aid, now=500)
    await m.check(aid, now=500)
    now["value"] = 1010
    await asyncio.gather(m.check(aid, now=1010), m.check(aid, now=1010))
    assert dispatched == ["exec"]
    state = await m.get_state(aid)
    assert state["last_result"]["status"] == "succeeded"
    assert state["last_result"]["before"]["windows"] and state["last_result"]["after"]["windows"]
    from ttakkari.runs import usage
    assert next(w for w in usage.cached(aid).windows if w.key == "seven_day").resets_at == 605810


async def test_unknown_crash_after_dispatch_marker_is_never_retried(monkeypatch):
    m, aid = monitor(), await account()
    await m.ensure(aid, now=500)
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, m.state_key(aid))
        row.value = {**row.value, "reset_at": 1000, "dispatched_cycle": 1000,
                     "dispatch_started_at": 1010, "handled_reset_at": 1000,
                     "last_result": {"status": "dispatching"}}
        await db.commit()

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not retry uncertain inference")

    async def read(*args, **kwargs):
        return sample(2000, reset=605810, percent=0)

    monkeypatch.setattr(m, "read_usage", read)
    monkeypatch.setattr(m, "activate", forbidden)
    await m.ensure(aid, now=2000, recover=True)
    await m.check(aid, now=2000)
    assert (await m.get_state(aid))["last_result"]["status"] == "unknown"


async def test_set_off_cancels_activation_and_disabled_account_is_not_queried(monkeypatch):
    m, aid = monitor(), await account()
    await m.ensure(aid, now=500)
    await m.set_enabled(aid, False, now=500)
    assert (await m.get_state(aid))["auto_enabled"] is False
    async with sessionmaker()() as db:
        a = await db.get(EngineAccount, aid)
        a.enabled = False
        await db.commit()

    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled account was queried")

    monkeypatch.setattr(m, "read_usage", forbidden)
    assert (await m.check(aid, now=600))["status"] == "account_disabled"


async def test_collector_delegates_codex_to_existing_persistent_scheduler(monkeypatch):
    from ttakkari.runs.usage_collector import Collector
    m, aid = monitor(), await account()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Codex must not be polled by the high-frequency collector")

    collector = Collector(fetch=forbidden)
    await collector.tick(now=500)
    await collector.tick(now=600)
    state = await m.get_state(aid)
    assert state.get("schedule_id"), "Codex has no persistent reservation"
    assert state["next_check_at"] == 500


async def test_collector_repairs_a_failed_canonical_job_without_a_new_provider_poll():
    from ttakkari.runs.usage_collector import Collector
    m, aid = monitor(), await account()
    collector = Collector()
    await collector.tick(now=500)
    state = await m.get_state(aid)
    async with sessionmaker()() as db:
        job = await db.get(ScheduledAction, __import__("uuid").UUID(state["schedule_id"]))
        job.status = "failed"
        await db.commit()
    await collector.tick(now=600)
    repaired = await m.get_state(aid)
    assert repaired["schedule_id"] != state["schedule_id"]
    assert repaired["next_check_at"] == 600 + 12 * 3600


async def test_existing_scheduler_executes_subscription_action(monkeypatch):
    from ttakkari.schedules import service
    m, aid = monitor(), await account()
    assert m.ACTION in service.SUPPORTED_ACTIONS, "subscription action is not registered"
    seen = []

    async def check(account_id, **kwargs):
        seen.append((account_id, kwargs))
        return {"status": "observed"}

    monkeypatch.setattr(m, "check", check)
    item = ScheduledAction(id=__import__("uuid").uuid4(), action_type=m.ACTION,
                           payload={"account_id": str(aid)})
    from datetime import UTC, datetime
    result = await service.execute_action(item, now=datetime.fromtimestamp(500, UTC))
    assert result == {"status": "observed"}
    assert seen == [(aid, {"now": 500, "schedule_id": str(item.id)})]


async def test_subscription_api_exposes_state_toggle_and_two_manual_actions(client, auth, monkeypatch):
    m, aid = monitor(), await account()
    base = f"/api/accounts/{aid}/subscription"
    response = await client.get(base, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "codex"
    toggled = await client.patch(base, json={"auto_enabled": False}, headers=auth)
    assert toggled.status_code == 200 and toggled.json()["auto_enabled"] is False
    seen = []

    async def check(account_id, **kwargs):
        seen.append((account_id, kwargs.get("manual_activation", False)))
        return {"status": "already_used"}

    monkeypatch.setattr(m, "check", check)
    for action in ("refresh", "activate"):
        response = await client.post(base + "/" + action, headers=auth)
        assert response.status_code == 200 and response.json()["action_result"]["status"] == "already_used"
    assert seen == [(aid, False), (aid, True)]
    assert (await client.get(base)).status_code == 401


async def test_subscription_api_rejects_non_codex_and_missing_accounts(client, auth):
    import uuid
    async with sessionmaker()() as db:
        a = EngineAccount(name="other provider", engine="claude")
        db.add(a)
        await db.commit()
        aid = a.id
    assert (await client.get(f"/api/accounts/{aid}/subscription", headers=auth)).status_code == 400
    assert (await client.get(f"/api/accounts/{uuid.uuid4()}/subscription", headers=auth)).status_code == 404


async def test_subscription_result_uses_existing_notification_preferences_and_dedupe(monkeypatch):
    m = monitor()
    from ttakkari.push import preferences, service
    assert hasattr(service.PushKind, "subscription"), "subscription notification type is not registered"
    assert any(row[0] == service.PushKind.subscription for row in preferences.CATALOG)
    assert hasattr(m, "notify_result"), "subscription results are not connected to notifications"
    calls = []

    async def notify(*args, **kwargs):
        calls.append((args, kwargs))
        return {"sent": 0}

    monkeypatch.setattr(service, "notify", notify)
    aid = await account()
    await m.notify_result(aid, 1000, {"status": "succeeded"})
    assert calls[0][0][0] == service.PushKind.subscription
    assert calls[0][1]["dedupe_key"] == f"subscription:{aid}:1000"
    assert calls[0][1]["url"] == "/accounts"


async def test_manual_activation_can_retry_an_undispatched_disabled_cycle(monkeypatch):
    m, aid = monitor(), await account()
    current = {"now": 500, "reset": 1000, "percent": 20}
    calls = []

    async def read(*args, **kwargs):
        return sample(current["now"], reset=current["reset"], percent=current["percent"])

    async def activate(*args, **kwargs):
        calls.append("exec")
        current["reset"] = 605810
        return {"status": "succeeded"}

    monkeypatch.setattr(m, "read_usage", read)
    monkeypatch.setattr(m, "activate", activate)
    await m.set_enabled(aid, False, now=500)
    await m.check(aid, now=500)
    current.update(now=1010, percent=0)
    await m.check(aid, now=1010)
    assert not calls
    await m.check(aid, now=1020, manual_activation=True)
    assert calls == ["exec"], "manual activation must be able to run an unused cycle skipped while off"
    await m.check(aid, now=1030, manual_activation=True)
    assert calls == ["exec"], "manual activation cannot bypass the durable dispatch marker"


async def test_running_legacy_user_request_across_reset_suppresses_activation(new_session, monkeypatch):
    import uuid
    from datetime import UTC, datetime

    from ttakkari.models import Run, RunStatus
    m, aid = monitor(), await account()
    current = {"now": 500}

    async def read(*args, **kwargs):
        return sample(current["now"], percent=0 if current["now"] > 1000 else 20)

    async def forbidden(*args, **kwargs):
        raise AssertionError("a user request already running on the account must suppress activation")

    monkeypatch.setattr(m, "read_usage", read)
    monkeypatch.setattr(m, "activate", forbidden)
    await m.ensure(aid, now=500)
    await m.check(aid, now=500)
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        db.add(Run(workspace_id=uuid.UUID(ws["id"]), session_id=uuid.UUID(sess["id"]), prompt="user request",
                   engine="codex", profile="subscription test", status=RunStatus.running,
                   started_at=datetime.fromtimestamp(900, UTC)))
        await db.commit()
    current["now"] = 1010
    assert (await m.check(aid, now=1010))["status"] == "already_used"


async def test_completed_run_after_rename_is_still_counted_as_user_usage(client, auth, new_session):
    import uuid
    from datetime import UTC, datetime

    from ttakkari.models import Run, RunStatus

    m, aid = monitor(), await account("codex-old")
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        db.add(Run(workspace_id=uuid.UUID(ws["id"]), session_id=uuid.UUID(sess["id"]), prompt="user request",
                   engine="codex", profile="codex-old", status=RunStatus.succeeded,
                   started_at=datetime.fromtimestamp(1100, UTC)))
        await db.commit()

    renamed = await client.patch(f"/api/accounts/{aid}", json={"name": "codex-new"}, headers=auth)
    assert renamed.status_code == 200, renamed.text
    renamed_again = await client.patch(f"/api/accounts/{aid}", json={"name": "codex-latest"}, headers=auth)
    assert renamed_again.status_code == 200, renamed_again.text

    async with sessionmaker()() as db:
        codex_account = await db.get(EngineAccount, aid)
        assert await m._user_used(db, codex_account, 1000) is True
        state = await db.get(AppSetting, m.state_key(aid))
        assert state.value["user_used_profile_aliases"] == ["codex-old", "codex-new"]
        run = await db.scalar(select(Run).where(Run.profile == "codex-old"))
        assert run is not None, "rename handling must preserve the historical Run row"


async def test_planned_low_frequency_observation_is_stale_without_fabricated_failure(client, auth):
    import time

    from ttakkari.runs import usage
    aid = await account()
    observed = sample(time.time() - 3600, reset=int(time.time() + 86400), percent=20)
    usage.seed(aid, observed)
    response = await client.get("/api/accounts/usage/cached", headers=auth)
    row = next(item for item in response.json() if item["id"] == str(aid))
    assert row["stale"] is True
    assert row["error"] is None, "a scheduled 12-hour observation is not a failed read"
