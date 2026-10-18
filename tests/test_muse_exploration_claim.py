"""Muse exploration reservation is shared state: one winner per interval across processes and restarts.

Independent review of a30cffc: the reservation time lived only in process memory, so two generations
(separate processes) each explored the same Muse account.
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting
from ttakkari.runs import profiles, usage

BACK = Path(__file__).resolve().parents[1]
MUSE = "muse-spark-1.3"

CHILD = """
import asyncio, sys, time, uuid
from ttakkari.runs import profiles

async def main():
    p = profiles.Profile(uuid.UUID(sys.argv[1]), "child", "muse", model="muse-spark-1.3")
    while time.time() < float(sys.argv[2]):
        pass
    print(len(await profiles.claim_muse_probe([p])))

asyncio.run(main())
"""


def muse(aid=None, name="muse"):
    return profiles.Profile(aid or uuid.uuid4(), name, "muse", model=MUSE)


def unknown(aid):
    usage.seed(aid, usage.Usage(windows=[usage.Window("five_hour", "5h", 0, int(time.time() + 3600)),
                                         usage.Window("seven_day", "7d", 10, int(time.time() + 86400))],
                                source="muse_cli", updated_at=time.time() - 3600, login="ok"), refresh=True)


async def stored(aid):
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, profiles.MUSE_PROBE_KEY.format(aid))
        return None if row is None else row.value["at"]


async def reopen_probe(aid):
    """Age the shared reservation past the interval (what time passing does), and forget the local copy."""
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, profiles.MUSE_PROBE_KEY.format(aid))
        row.value = {"at": time.time() - profiles.MUSE_PROBE_INTERVAL - 1}
        await db.commit()
    profiles.reset_probes()


async def test_one_reservation_per_interval_then_reopens(client, auth):
    p = muse()
    unknown(p.id)
    assert await profiles.claim_muse_probe([p]) == {p.id}
    assert await profiles.claim_muse_probe([p]) == frozenset()
    await reopen_probe(p.id)
    assert await profiles.claim_muse_probe([p]) == {p.id}


async def test_reservation_survives_restart_and_a_lost_claim_learns_the_stored_time(client, auth):
    p = muse()
    unknown(p.id)
    assert await profiles.claim_muse_probe([p]) == {p.id}
    at = await stored(p.id)
    profiles.reset_probes()  # a restarted process has no memory of the reservation
    assert profiles.muse_probe_eligible(p), "the local copy is advisory only"
    assert await profiles.claim_muse_probe([p]) == frozenset()
    assert profiles._probe_last[p.id] == at, "the loser adopts the stored reservation time"
    assert not profiles.muse_probe_eligible(p)


async def test_concurrent_claims_in_one_process_have_exactly_one_winner(client, auth):
    p = muse()
    unknown(p.id)
    results = await asyncio.gather(*(profiles.claim_muse_probe([p]) for _ in range(12)))
    assert sum(bool(r) for r in results) == 1


async def test_separate_processes_cannot_both_explore_the_same_account(client, auth):
    """The reviewer's counterexample: each generation used to keep its own in-memory reservation."""
    p = muse()
    unknown(p.id)
    start = time.time() + 4  # let both interpreters finish importing before the race
    env = {**os.environ, "PYTHONPATH": str(BACK / "src")}
    children = [await asyncio.create_subprocess_exec(
        sys.executable, "-c", CHILD, str(p.id), str(start), env=env, cwd=BACK,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for _ in range(3)]
    while time.time() < start:
        await asyncio.sleep(0.05)
    parent_wins = len(await profiles.claim_muse_probe([p]))
    outputs = await asyncio.wait_for(asyncio.gather(*(child.communicate() for child in children)), 60)
    for child, (_out, err) in zip(children, outputs, strict=True):
        assert child.returncode == 0, err.decode()
    wins = parent_wins + sum(int(out.decode().strip()) for out, _err in outputs)
    assert wins == 1, f"{wins} processes won the same reservation: {outputs}"


async def test_only_one_of_several_unconfirmed_muse_accounts_is_reserved(client, auth):
    a, b = muse(name="a"), muse(name="b")
    unknown(a.id)
    unknown(b.id)
    assert await profiles.claim_muse_probe([a, b]) == {a.id}
    assert await stored(b.id) is None, "the account that is not explored keeps its slot"
    assert await profiles.claim_muse_probe([a, b]) == {b.id}


async def test_ineligible_accounts_never_consume_a_reservation(client, auth):
    known, logged_out, cooling, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    usage.seed(known, usage.Usage(windows=[usage.Window("five_hour", "5h", 5, int(time.time() + 3600)),
                                           usage.Window("seven_day", "7d", 5, int(time.time() + 86400))],
                                  source="muse_cli", updated_at=time.time(), login="ok"), refresh=True)
    usage.seed(logged_out, usage.Usage(login="missing", error="login", updated_at=time.time()), refresh=True)
    unknown(cooling)
    profiles.cool_down(cooling, 600)
    pool = [muse(known), muse(logged_out), muse(cooling), profiles.Profile(other, "c", "codex", model="gpt-6-luna")]
    assert await profiles.claim_muse_probe(pool) == frozenset()
    for aid in (known, logged_out, cooling, other):
        assert await stored(aid) is None


async def test_database_failure_means_no_exploration(client, auth, monkeypatch):
    p = muse()
    unknown(p.id)

    def broken():
        raise OSError("db down")

    monkeypatch.setattr(profiles, "sessionmaker", broken)
    assert await profiles.claim_muse_probe([p]) == frozenset()
    assert p.id not in profiles._probe_last


def test_reservation_key_fits_the_existing_app_settings_column_and_keeps_old_generations_unaffected():
    key = profiles.MUSE_PROBE_KEY.format(uuid.uuid4())
    assert len(key) <= 60  # app_settings.key is String(60); no schema change is needed
    assert key != profiles.COOLDOWN_KEY and not key.startswith(profiles.COOLDOWN_KEY)
