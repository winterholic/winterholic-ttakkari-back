"""ADR 0013 알려진 한계 정리: 동시에 넘긴 같은 일의 기획 중복 방지, 끝난 기획 세션 자동 보관."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from helpers import run_events, start_run, wait_run
from test_desk import _all_done, _desk, _leads, _wait

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, utcnow
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import lead_archive


async def _delegated(client, auth, run_id: str) -> dict:
    return next(e["payload"] for e in await run_events(client, auth, run_id) if e["type"] == "desk.delegated")


async def _send_together(client, auth, desk_id: str, *prompts: str) -> list[dict]:
    started = await asyncio.gather(*(start_run(client, auth, desk_id, p) for p in prompts))
    return [await wait_run(client, auth, r["id"]) for r in started]


# ---- 중복 기획 방지

async def test_concurrent_same_title_makes_one_lead(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    # 띄어쓰기·대소문자·구두점만 다른 같은 제목
    runs = await _send_together(client, auth, desk["id"], "다크모드 1 DESK_TITLE:Dark_Mode_고치기",
                                "다크모드 2 DESK_TITLE:dark-mode__고치기.")
    assert all(r["status"] == "succeeded" for r in runs)
    leads = await _leads(client, auth, desk["id"])
    assert len(leads) == 1, [x["title"] for x in leads]
    payloads = [await _delegated(client, auth, r["id"]) for r in runs]
    assert {p["lead_id"] for p in payloads} == {leads[0]["id"]}
    assert sorted(bool(p.get("dedup")) for p in payloads) == [False, True]
    lead_runs = (await client.get(f"/api/sessions/{leads[0]['id']}/runs", headers=auth)).json()
    assert sorted(r["prompt"].split("\n")[1][:6] for r in lead_runs) == ["다크모드 1", "다크모드 2"]
    await _wait(lambda: _all_done(client, auth, leads[0]["id"]), what="기획 턴")


async def test_concurrent_different_titles_make_two_leads(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    runs = await _send_together(client, auth, desk["id"], "DESK_TITLE:다크모드_고치기", "DESK_TITLE:로그인_고치기")
    assert all(r["status"] == "succeeded" for r in runs)
    leads = await _leads(client, auth, desk["id"])
    assert sorted(x["title"] for x in leads) == ["다크모드 고치기", "로그인 고치기"]
    assert not any([(await _delegated(client, auth, r["id"])).get("dedup") for r in runs])
    for lead in leads:
        await _wait(lambda lead=lead: _all_done(client, auth, lead["id"]), what="기획 턴")


async def test_same_title_after_window_makes_new_lead(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "lead_dedup_seconds", 0)
    desk = await _desk(client, auth, make_dir)
    for _ in range(2):
        await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_TITLE:같은_일"))["id"])
    leads = await _leads(client, auth, desk["id"])
    assert len(leads) == 2
    for lead in leads:
        await _wait(lambda lead=lead: _all_done(client, auth, lead["id"]), what="기획 턴")


# ---- 끝난 기획 자동 보관

async def _lead_with_worker(client, auth, make_dir, worker_prompt: str = "write") -> tuple[dict, dict, dict]:
    desk = await _desk(client, auth, make_dir)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_DELEGATE"))["id"])
    lead = (await _leads(client, auth, desk["id"]))[0]
    await _wait(lambda: _all_done(client, auth, lead["id"]), what="기획 턴")
    r = await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": worker_prompt, "engine": "claude"},
                          headers={"Authorization": f"Bearer {issue_token(lead['id'])}"})
    assert r.status_code == 201, r.text
    worker = (await client.get(f"/api/sessions/{r.json()['worker_id']}", headers=auth)).json()
    return desk, lead, worker


async def _archived(sid: str) -> bool:
    async with sessionmaker()() as db:
        return (await db.get(ChatSession, sid)).archived_at is not None


async def _sweep(hours: float) -> int:
    return await lead_archive.archive_idle_leads(now=utcnow() + timedelta(hours=hours))


async def _settle(client, auth, lead: dict, worker: dict) -> None:
    # 작업 보고가 기획에 Run 을 다시 넣으므로 둘 다 조용해질 때까지 기다린다.
    for _ in range(3):
        await _wait(lambda: _all_done(client, auth, worker["id"]), what="작업 세션")
        await _wait(lambda: _all_done(client, auth, lead["id"]), what="기획 보고 처리")
        await asyncio.sleep(0.3)


async def test_idle_lead_is_archived_with_workers_after_deadline(client, auth, make_dir):
    desk, lead, worker = await _lead_with_worker(client, auth, make_dir)
    await _settle(client, auth, lead, worker)
    hours = get_settings().lead_archive_after_hours

    assert await _sweep(hours - 1) == 0
    assert not await _archived(lead["id"]) and not await _archived(worker["id"])

    assert await _sweep(hours + 1) == 1
    assert await _archived(lead["id"]) and await _archived(worker["id"])
    assert not await _archived(desk["id"])
    # 접수 오른쪽 패널·작업판에서 빠진다
    assert await _leads(client, auth, desk["id"]) == []
    # 보관된 기획에 사용자가 직접 보내면 기존대로 409
    r = await client.post(f"/api/sessions/{lead['id']}/runs", json={"prompt": "더 해줘"}, headers=auth)
    assert r.status_code == 409
    # 다시 돌려도 이미 보관된 것은 세지 않는다
    assert await _sweep(hours + 1) == 0


async def test_active_lead_run_blocks_archive(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "SLEEP DESK_DELEGATE"))["id"])
    lead = (await _leads(client, auth, desk["id"]))[0]
    lead_run = (await client.get(f"/api/sessions/{lead['id']}/runs", headers=auth)).json()[0]
    assert await _sweep(1000) == 0 and not await _archived(lead["id"])
    await client.post(f"/api/runs/{lead_run['id']}/cancel", headers=auth)
    await wait_run(client, auth, lead_run["id"])


async def test_active_worker_run_blocks_archive(client, auth, make_dir):
    _desk_s, lead, worker = await _lead_with_worker(client, auth, make_dir, worker_prompt="SLEEP")
    wrun = (await client.get(f"/api/sessions/{worker['id']}/runs", headers=auth)).json()[0]
    assert wrun["status"] in {"queued", "running"}
    assert await _sweep(1000) == 0
    assert not await _archived(lead["id"]) and not await _archived(worker["id"])
    await client.post(f"/api/runs/{wrun['id']}/cancel", headers=auth)
    await wait_run(client, auth, wrun["id"])


async def test_archive_skips_non_lead_sessions(client, auth, make_dir, new_session):
    ws, chat = await new_session()
    main = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"},
                              headers=auth)).json()
    async with sessionmaker()() as db:
        # 부모 없는 작업 세션, 옛 메인 아래 작업 세션
        lone = ChatSession(workspace_id=ws["id"], title="lone", engine="claude", kind="worker")
        under_main = ChatSession(workspace_id=ws["id"], title="w", engine="claude", kind="worker", parent_id=main["id"])
        db.add_all([lone, under_main])
        await db.commit()
        ids = [chat["id"], main["id"], str(lone.id), str(under_main.id)]
    assert await _sweep(1000) == 0
    assert not any([await _archived(i) for i in ids])
