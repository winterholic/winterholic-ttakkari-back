"""날씨 브리핑 엔진 선택: auto(Codex·Claude) → Antigravity → 데이터 기반 발행."""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    EngineAccount,
    Run,
    RunEvent,
    RunOrigin,
    RunStatus,
    WeatherBriefing,
)
from ttakkari.runs import profiles
from ttakkari.weather import service
from ttakkari.weather.service import parse_model_result

DAY = date(2026, 10, 16)
VALID = {"summary": ["수원은 흐립니다.", "오후 기온이 오릅니다."], "umbrella": "강수확률은 낮습니다.",
         "outfit": "겉옷을 챙기세요.", "commute": "08~09시와 18~19시를 가정합니다.",
         "daypart": "오전부터 저녁까지 흐립니다.", "week": "주말 기온을 확인하세요."}


def _forecast():
    from test_weather_api import sample_forecast
    return sample_forecast()


@pytest.fixture
def queued(monkeypatch):
    """queue_run 을 가짜로 바꿔 에이전트를 띄우지 않고 어떤 세션·모델로 들어왔는지만 본다."""
    calls: list[dict] = []

    async def fake(db, session, prompt, *, origin=RunOrigin.user, model=None, effort=None, request_key=None, **_):
        run = Run(session_id=session.id, workspace_id=session.workspace_id, prompt=prompt, origin=origin,
                  request_key=request_key, engine=session.engine, model=model, effort=effort)
        db.add(run)
        await db.commit()
        calls.append({"engine": session.engine, "model": model, "effort": effort, "key": request_key, "run": run.id})
        return run

    monkeypatch.setattr("ttakkari.api.sessions.queue_run", fake)
    return calls


async def _accounts(*, antigravity: bool = True) -> dict[str, EngineAccount]:
    async with sessionmaker()() as db:
        rows = {a.engine: a for a in await profiles.accounts(db)}
        if antigravity:
            ag = EngineAccount(name="antigravity", engine="antigravity", enabled=True, priority=9)
            db.add(ag)
            await db.commit()
            rows["antigravity"] = ag
        return rows


async def _row(**kw) -> WeatherBriefing:
    async with sessionmaker()() as db:
        row = WeatherBriefing(day=DAY, status="starting", request_source="manual", forecast=_forecast(),
                              source_errors={}, **kw)
        db.add(row)
        await db.commit()
        return row


async def _reload(row_id) -> WeatherBriefing:
    async with sessionmaker()() as db:
        return await db.get(WeatherBriefing, row_id)


def _limit(*accounts: EngineAccount) -> None:
    for a in accounts:
        profiles.cool_down(a.id, 3600)


async def test_route_is_auto_while_any_claude_or_codex_account_is_free():
    accs = await _accounts()
    async with sessionmaker()() as db:
        assert await service.choose_route(db) == ("auto", None)
    _limit(accs["codex"])
    async with sessionmaker()() as db:
        assert await service.choose_route(db) == ("auto", None)  # Claude 가 남아 있다


async def test_route_falls_back_to_antigravity_when_claude_and_codex_are_limited():
    accs = await _accounts()
    _limit(accs["codex"], accs["claude"])
    async with sessionmaker()() as db:
        assert await service.choose_route(db) == ("antigravity", "auto_limited")


async def test_route_is_none_when_every_engine_is_unavailable():
    accs = await _accounts()
    _limit(accs["codex"], accs["claude"], accs["antigravity"])
    async with sessionmaker()() as db:
        assert await service.choose_route(db) == (None, "auto_limited+antigravity_limited")


async def test_route_is_none_without_an_antigravity_account():
    accs = await _accounts(antigravity=False)
    _limit(accs["codex"], accs["claude"])
    async with sessionmaker()() as db:
        assert await service.choose_route(db) == (None, "auto_limited+antigravity_no_account")


async def test_auto_is_the_default_dispatch_and_is_recorded(queued):
    await _accounts()
    row = await _row()
    await service._dispatch_model(row.id, DAY)
    assert [(c["engine"], c["model"], c["effort"]) for c in queued] == [("auto", "gpt-6-luna", "low")]
    saved = await _reload(row.id)
    assert (saved.status, saved.analysis_engine, saved.analysis_model) == ("generating", "auto", "gpt-6-luna")
    assert saved.analysis_route[0]["engine"] == "auto"


async def test_codex_and_claude_limit_dispatches_antigravity_medium(queued):
    accs = await _accounts()
    _limit(accs["codex"], accs["claude"])
    row = await _row()
    await service._dispatch_model(row.id, DAY)
    assert [(c["engine"], c["model"], c["effort"]) for c in queued] == [
        ("antigravity", "gemini-3.8-flash-medium", "medium")]
    saved = await _reload(row.id)
    assert saved.status == "generating"
    assert saved.analysis_route[0]["reason"] == "auto_limited"


async def test_all_engines_unavailable_publishes_data_only_content(queued):
    accs = await _accounts()
    _limit(accs["codex"], accs["claude"], accs["antigravity"])
    row = await _row()
    await service._dispatch_model(row.id, DAY)
    assert queued == []
    saved = await _reload(row.id)
    assert (saved.status, saved.analysis_status) == ("degraded", "failed")
    assert saved.briefing["summary"] and saved.generated_at is not None
    assert "엔진이 없습니다" in saved.failure


async def test_a_previous_codex_session_is_not_reused_for_the_auto_route(queued):
    await _accounts()
    row = await _row()
    await service._dispatch_model(row.id, DAY)
    async with sessionmaker()() as db:
        old = await db.get(ChatSession, (await _reload(row.id)).session_id)
        old.engine = "codex"
        r = await db.get(WeatherBriefing, row.id)
        r.status = "starting"
        await db.commit()
        old_id = old.id
    await service._dispatch_model(row.id, DAY)
    saved = await _reload(row.id)
    assert saved.session_id != old_id
    async with sessionmaker()() as db:
        assert (await db.get(ChatSession, saved.session_id)).engine == "auto"


async def _waiting_run(row: WeatherBriefing) -> None:
    """한도 때문에 초기화를 기다리는 Run(queued + run.waiting)."""
    async with sessionmaker()() as db:
        run = await db.get(Run, row.run_id)
        run.status = RunStatus.queued
        db.add(RunEvent(run_id=run.id, seq=1, type="run.waiting", payload={"until": None}))
        run.last_seq = 1
        await db.commit()


async def test_waiting_auto_run_moves_to_antigravity_instead_of_waiting_for_reset(queued):
    accs = await _accounts()
    row = await _row()
    await service._dispatch_model(row.id, DAY)
    _limit(accs["codex"], accs["claude"])
    generating = await _reload(row.id)
    await _waiting_run(generating)
    await service.advance(row.id)
    saved = await _reload(row.id)
    assert [c["engine"] for c in queued] == ["auto", "antigravity"]
    assert (saved.status, saved.analysis_engine, saved.run_id) == ("generating", "antigravity", queued[1]["run"])
    assert saved.run_id != generating.run_id
    async with sessionmaker()() as db:
        assert (await db.get(Run, generating.run_id)).status == RunStatus.cancelled
    assert [e["stage"] for e in saved.analysis_route] == ["dispatch", "dispatch"]


async def test_waiting_antigravity_run_degrades_with_data_only_content(queued, monkeypatch):
    accs = await _accounts()
    _limit(accs["codex"], accs["claude"])
    row = await _row()
    await service._dispatch_model(row.id, DAY)
    generating = await _reload(row.id)
    assert generating.analysis_engine == "antigravity"
    await _waiting_run(generating)
    async def no_push(_):  # 푸시 구독은 이 테스트의 관심사가 아니다
        return None
    monkeypatch.setattr(service, "_publish", no_push)
    await service.advance(row.id)
    saved = await _reload(row.id)
    assert (saved.status, saved.analysis_status) == ("degraded", "failed")
    assert saved.briefing["summary"]
    assert len(queued) == 1


async def test_completed_run_records_the_engine_and_counterpart_model(queued, monkeypatch):
    await _accounts()
    row = await _row()
    await service._dispatch_model(row.id, DAY)
    generating = await _reload(row.id)
    async with sessionmaker()() as db:
        run = await db.get(Run, generating.run_id)
        run.status, run.engine, run.profile = RunStatus.succeeded, "claude", "claude"
        run.result_text = json.dumps(VALID, ensure_ascii=False)
        await db.commit()
    async def no_push(_):
        return None
    monkeypatch.setattr(service, "_publish", no_push)
    await service.advance(row.id)
    saved = await _reload(row.id)
    assert (saved.status, saved.analysis_status) == ("ready", "succeeded")
    assert (saved.analysis_engine, saved.analysis_model) == ("claude", "sonnet")
    assert saved.analysis_route[-1]["stage"] == "completed"


def test_effective_model_follows_the_manager_counterpart_rule():
    def run(engine, model):
        return Run(engine=engine, model=model)
    assert service.effective_model(run("codex", "gpt-6-luna")) == "gpt-6-luna"
    assert service.effective_model(run("claude", "gpt-6-luna")) == "sonnet"
    assert service.effective_model(run("antigravity", "gemini-3.8-flash-medium")) == "gemini-3.8-flash-medium"


@pytest.mark.parametrize("wrap", [
    lambda body: body,
    lambda body: f"```json\n{body}\n```",
    lambda body: f"요청하신 브리핑입니다. 형식은 {{\"summary\": [...]}} 입니다.\n```json\n{body}\n```\n끝났습니다 {{감사}}",
    lambda body: f"  \n{body}\n\n(참고: 위 내용은 Open-Meteo 기준)",
])
def test_json_extraction_survives_engine_output_styles(wrap):
    assert parse_model_result(wrap(json.dumps(VALID, ensure_ascii=False)), {}) == VALID


def test_json_extraction_trims_overlong_summary_and_rejects_garbage():
    longer = {**VALID, "summary": [f"문장 {i}" for i in range(6)]}
    assert parse_model_result(json.dumps(longer, ensure_ascii=False), {})["summary"] == longer["summary"][:4]
    with pytest.raises(ValueError, match="JSON"):
        parse_model_result("설명만 있고 객체가 없습니다", {})
    with pytest.raises(Exception, match="umbrella"):
        parse_model_result(json.dumps({k: v for k, v in VALID.items() if k != "umbrella"}), {})
    unsafe = {**VALID, "outfit": "노면이 미끄러우니 주의하세요."}
    with pytest.raises(ValueError, match="위험"):
        parse_model_result(json.dumps(unsafe, ensure_ascii=False), {})


async def _stale_codex_locked_row(queued) -> tuple[WeatherBriefing, Run]:
    """구버전(cd0c78e)이 만든 상태: engine=codex 고정 세션 + 한도 대기 Run + 엔진 기록 없는 generating 행."""
    accs = await _accounts()
    row = await _row()
    async with sessionmaker()() as db:
        workspace = await db.scalar(select(ChatSession.workspace_id).limit(1))
        if workspace is None:
            from ttakkari.api.workspaces import ensure_default_workspace
            workspace = (await ensure_default_workspace(db)).id
        session = ChatSession(workspace_id=workspace, title="날씨 브리핑 · old", engine="codex", model="gpt-6-luna",
                              effort="low", pool="personal")
        db.add(session)
        await db.flush()
        run = Run(session_id=session.id, workspace_id=workspace, prompt="p", origin=RunOrigin.system, engine="codex",
                  model="gpt-6-luna", effort="low", request_key=f"weather:{DAY.isoformat()}", status=RunStatus.queued)
        db.add(run)
        await db.flush()
        db.add(RunEvent(run_id=run.id, seq=1, type="run.waiting", payload={"until": None}))
        run.last_seq = 1
        saved = await db.get(WeatherBriefing, row.id)
        saved.status, saved.session_id, saved.run_id = "generating", session.id, run.id
        await db.commit()
    _limit(accs["codex"])
    return await _reload(row.id), run


async def test_stale_codex_locked_run_restarts_on_auto_while_claude_is_free(queued):
    row, stale = await _stale_codex_locked_row(queued)
    await service.advance(row.id)
    saved = await _reload(row.id)
    assert [c["engine"] for c in queued] == ["auto"]
    assert (saved.status, saved.analysis_engine, saved.run_id) == ("generating", "auto", queued[0]["run"])
    async with sessionmaker()() as db:
        assert (await db.get(Run, stale.id)).status == RunStatus.cancelled
        assert (await db.get(ChatSession, saved.session_id)).engine == "auto"
    assert saved.analysis_route[-1]["reason"] == "locked_engine_waiting"


async def test_stale_codex_locked_run_goes_to_antigravity_when_claude_is_limited_too(queued):
    row, _ = await _stale_codex_locked_row(queued)
    _limit(*[a for a in (await _accounts(antigravity=False)).values() if a.engine == "claude"])
    await service.advance(row.id)
    saved = await _reload(row.id)
    assert [c["engine"] for c in queued] == ["antigravity"]
    assert saved.analysis_engine == "antigravity"


async def test_generation_runtime_schedules_the_weather_scan(monkeypatch):
    """운영은 runtime_generation 모드다. 이 경로의 주기 작업에 날씨 scan 이 없으면 06:30 자동 실행도 폴백도 영원히 안 돈다."""
    from ttakkari import generation_service
    from ttakkari.config import get_settings

    calls = []

    async def fake_scan():
        calls.append("scan")

    monkeypatch.setattr(service, "scan", fake_scan)
    jobs = {name: op for name, op, _ in generation_service.maintenance_jobs(get_settings().model_copy(
        update={"reminder_scan_seconds": 30}))}
    assert "weather" in jobs
    await jobs["weather"]()
    assert calls == ["scan"]
