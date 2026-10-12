"""메모·캘린더·한국 공휴일(ADR 0008)."""
from __future__ import annotations

import uuid
from datetime import date, datetime

import pytest
from test_context import _agent, _session

from ttakkari.db import sessionmaker
from ttakkari.models import CalendarEvent
from ttakkari.planner import kr_holidays, reminders
from ttakkari.planner.service import KST
from ttakkari.push.service import PushKind

# 정부 발표(공휴일에 관한 법률·관공서의 공휴일에 관한 규정, 천문연구원 월력요항)와 대조한 기대값.
# 2026: 노동절은 2026-03-31 국회 의결로 5/1 부터 법정공휴일, 제헌절은 2026-02-10 공포·5/11 시행으로 올해부터 공휴일.
HOLIDAYS_2026 = [
    ("2026-01-01", "신정"),
    ("2026-02-16", "설날 전날"), ("2026-02-17", "설날"), ("2026-02-18", "설날 다음날"),
    ("2026-03-01", "삼일절"), ("2026-03-02", "삼일절 대체 휴일"),
    ("2026-05-01", "노동절"), ("2026-05-05", "어린이날"),
    ("2026-05-24", "부처님오신날"), ("2026-05-25", "부처님오신날 대체 휴일"),
    ("2026-06-03", "지방선거일"), ("2026-06-06", "현충일"),
    ("2026-07-17", "제헌절"),
    ("2026-08-15", "광복절"), ("2026-08-17", "광복절 대체 휴일"),
    ("2026-09-24", "추석 전날"), ("2026-09-25", "추석"), ("2026-09-26", "추석 다음날"),
    ("2026-10-03", "개천절"), ("2026-10-05", "개천절 대체 휴일"),
    ("2026-10-09", "한글날"),
    ("2026-12-25", "기독탄신일"),
]
# 2027: 대체공휴일 7일(2/9, 5/3, 7/19, 8/16, 10/4, 10/11, 12/27)
HOLIDAYS_2027 = [
    ("2027-01-01", "신정"),
    ("2027-02-06", "설날 전날"), ("2027-02-07", "설날"), ("2027-02-08", "설날 다음날"), ("2027-02-09", "설날 대체 휴일"),
    ("2027-03-01", "삼일절"),
    ("2027-05-01", "노동절"), ("2027-05-03", "노동절 대체 휴일"), ("2027-05-05", "어린이날"),
    ("2027-05-13", "부처님오신날"),
    ("2027-06-06", "현충일"),
    ("2027-07-17", "제헌절"), ("2027-07-19", "제헌절 대체 휴일"),
    ("2027-08-15", "광복절"), ("2027-08-16", "광복절 대체 휴일"),
    ("2027-09-14", "추석 전날"), ("2027-09-15", "추석"), ("2027-09-16", "추석 다음날"),
    ("2027-10-03", "개천절"), ("2027-10-04", "개천절 대체 휴일"),
    ("2027-10-09", "한글날"), ("2027-10-11", "한글날 대체 휴일"),
    ("2027-12-25", "기독탄신일"), ("2027-12-27", "기독탄신일 대체 휴일"),
]


def _pairs(year: int) -> list[tuple[str, str]]:
    return [(h["date"], h["name"]) for h in kr_holidays.year_holidays(year)]


def test_holidays_2026_pinned():
    assert _pairs(2026) == HOLIDAYS_2026


def test_holidays_2027_pinned():
    assert _pairs(2027) == HOLIDAYS_2027


def test_holidays_substitute_flag_and_range_edges():
    subs = [h["date"] for h in kr_holidays.year_holidays(2027) if h["substitute"]]
    assert subs == ["2027-02-09", "2027-05-03", "2027-07-19", "2027-08-16", "2027-10-04", "2027-10-11", "2027-12-27"]
    assert kr_holidays.year_holidays(kr_holidays.MAX_YEAR + 1) == ()
    assert [h["date"] for h in kr_holidays.holidays_between(date(2026, 12, 30), date(2027, 1, 2))] == ["2027-01-01"]


def test_past_temporary_holidays_and_elections_are_included():
    names = dict(_pairs(2025) + _pairs(2024) + _pairs(2020))
    assert names["2025-01-27"] == "임시공휴일" and names["2025-06-03"] == "대통령 선거일"
    assert names["2024-04-10"] == "국회의원 선거일" and names["2020-08-17"] == "임시공휴일"


async def test_holidays_api(client, auth):
    r = await client.get("/api/calendar/holidays", params={"from": "2026-09-01", "to": "2026-10-31"}, headers=auth)
    assert r.status_code == 200
    assert [h["date"] for h in r.json()["holidays"]] == ["2026-09-24", "2026-09-25", "2026-09-26", "2026-10-03",
                                                         "2026-10-05", "2026-10-09"]
    assert (await client.get("/api/calendar/holidays", params={"from": "2026-01-01", "to": "2028-01-01"},
                             headers=auth)).status_code == 422
    assert (await client.get("/api/calendar/holidays", params={"from": "2026-01-01", "to": "2026-02-01"})).status_code == 401


# ---- 메모

async def test_memo_crud_search_pin_and_tags(client, auth):
    a = (await client.post("/api/memos", json={"title": "장보기", "body": "# 목록\n- 우유\n- 계란", "tags": ["#집", "집", "생활"]},
                           headers=auth)).json()
    assert a["tags"] == ["집", "생활"] and a["rev"] == 1 and a["source"] == "user"
    b = (await client.post("/api/memos", json={"title": "회의", "body": "분기 계획", "tags": ["회사"], "pinned": True},
                           headers=auth)).json()
    listing = (await client.get("/api/memos", headers=auth)).json()["memos"]
    assert [m["id"] for m in listing] == [b["id"], a["id"]]  # 고정이 먼저
    assert "body" not in listing[0] and listing[1]["excerpt"].startswith("# 목록")
    for q, expect in (("우유", [a["id"]]), ("분기", [b["id"]]), ("회사", [b["id"]]), ("없는말", [])):
        got = (await client.get("/api/memos", params={"q": q}, headers=auth)).json()["memos"]
        assert [m["id"] for m in got] == expect, q
    assert (await client.get("/api/memos", params={"q": "100%"}, headers=auth)).json()["memos"] == []
    tags = (await client.get("/api/memos/tags", headers=auth)).json()["tags"]
    assert {t["name"]: t["count"] for t in tags} == {"집": 1, "생활": 1, "회사": 1}
    assert [m["id"] for m in (await client.get("/api/memos", params={"tag": "집"}, headers=auth)).json()["memos"]] == [a["id"]]
    p = (await client.patch(f"/api/memos/{a['id']}", json={"body": "수정됨", "pinned": True, "rev": 1}, headers=auth)).json()
    assert p["rev"] == 2 and p["body"] == "수정됨" and p["pinned"] is True
    assert (await client.get(f"/api/memos/{a['id']}", headers=auth)).json()["body"] == "수정됨"
    assert (await client.delete(f"/api/memos/{a['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/memos/{a['id']}", headers=auth)).status_code == 404


async def test_memo_autosave_conflict_returns_409(client, auth):
    m = (await client.post("/api/memos", json={"body": "처음"}, headers=auth)).json()
    ok = await client.patch(f"/api/memos/{m['id']}", json={"body": "기기 A", "rev": 1}, headers=auth)
    assert ok.status_code == 200
    stale = await client.patch(f"/api/memos/{m['id']}", json={"body": "기기 B", "rev": 1}, headers=auth)
    assert stale.status_code == 409 and stale.json()["code"] == "conflict"
    assert (await client.get(f"/api/memos/{m['id']}", headers=auth)).json()["body"] == "기기 A"


# ---- 일정

async def test_event_timed_allday_and_timezone(client, auth):
    t = (await client.post("/api/calendar/events", json={"title": "치과", "start": "2026-10-14T09:30"}, headers=auth)).json()
    assert t["start"] == "2026-10-14T09:30" and t["end"] == "2026-10-14T10:30" and t["all_day"] is False
    utc = (await client.post("/api/calendar/events", json={"title": "UTC 입력", "start": "2026-10-14T00:00:00Z",
                                                           "end": "2026-10-14T01:00:00Z"}, headers=auth)).json()
    assert utc["start"] == "2026-10-14T09:00"  # Asia/Seoul 로 환산
    d = (await client.post("/api/calendar/events", json={"title": "출장", "all_day": True, "start": "2026-10-20",
                                                         "end": "2026-10-22"}, headers=auth)).json()
    assert (d["start"], d["end"]) == ("2026-10-20", "2026-10-22")  # 끝 날짜 포함
    got = (await client.get("/api/calendar/events", params={"from": "2026-10-21", "to": "2026-10-21"}, headers=auth)).json()["events"]
    assert [e["title"] for e in got] == ["출장"]
    day = (await client.get("/api/calendar/events", params={"from": "2026-10-23", "to": "2026-10-23"}, headers=auth)).json()["events"]
    assert day == []
    bad = await client.post("/api/calendar/events", json={"title": "x", "start": "2026-10-14T10:00", "end": "2026-10-14T09:00"}, headers=auth)
    assert bad.status_code == 422
    assert (await client.post("/api/calendar/events", json={"title": "x", "start": "내일"}, headers=auth)).status_code == 422


async def test_event_patch_and_delete(client, auth):
    e = (await client.post("/api/calendar/events", json={"title": "점심", "start": "2026-10-14T12:00", "end": "2026-10-14T13:00"},
                           headers=auth)).json()
    p = (await client.patch(f"/api/calendar/events/{e['id']}", json={"title": "점심 약속", "start": "2026-10-14T12:30"},
                            headers=auth)).json()
    assert p["title"] == "점심 약속" and p["start"] == "2026-10-14T12:30" and p["end"] == "2026-10-14T13:00"
    # 종일 전환은 시작·끝을 같이 받아야 한다.
    assert (await client.patch(f"/api/calendar/events/{e['id']}", json={"all_day": True}, headers=auth)).status_code == 422
    ad = (await client.patch(f"/api/calendar/events/{e['id']}", json={"all_day": True, "start": "2026-10-14", "end": "2026-10-14"},
                             headers=auth)).json()
    assert ad["all_day"] is True and ad["start"] == "2026-10-14"
    assert (await client.delete(f"/api/calendar/events/{e['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/calendar/events/{e['id']}", headers=auth)).status_code == 404


async def test_rrule_input_rejected_but_stored_rules_still_expand(client, auth):
    # 반복 입력은 보류(ADR 0008). 생성·수정 모두 422.
    base = {"title": "주간 회의", "start": "2026-10-05T10:00", "end": "2026-10-05T11:00"}
    assert (await client.post("/api/calendar/events", json={**base, "rrule": "FREQ=WEEKLY"}, headers=auth)).status_code == 422
    e = (await client.post("/api/calendar/events", json=base, headers=auth)).json()
    assert e["recurring"] is False and e["rrule"] is None
    assert (await client.patch(f"/api/calendar/events/{e['id']}", json={"rrule": "FREQ=DAILY"}, headers=auth)).status_code == 422
    # 이미 반복으로 저장된 일정(보류 전 데이터)은 그대로 펼쳐 보여 준다.
    async with sessionmaker()() as db:
        ev = await db.get(CalendarEvent, uuid.UUID(e["id"]))
        ev.rrule = "FREQ=WEEKLY;BYDAY=MO;COUNT=4"
        await db.commit()
    got = (await client.get("/api/calendar/events", params={"from": "2026-10-01", "to": "2026-11-30"}, headers=auth)).json()["events"]
    assert [x["start"] for x in got] == ["2026-10-05T10:00", "2026-10-12T10:00", "2026-10-19T10:00", "2026-10-26T10:00"]
    assert all(x["recurring"] for x in got)
    # 제목 수정은 되고, 반복 일정에 알림은 걸 수 없다.
    assert (await client.patch(f"/api/calendar/events/{e['id']}", json={"title": "회의"}, headers=auth)).status_code == 200
    assert (await client.patch(f"/api/calendar/events/{e['id']}", json={"reminder": "10m"}, headers=auth)).status_code == 422
    assert (await client.get("/api/calendar/events", params={"from": "2026-01-01", "to": "2028-12-31"}, headers=auth)).status_code == 422


# ---- 일정 알림

def _kst(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=KST)


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake(kind, title, body="", **kw):
        calls.append({"kind": kind, "title": title, "body": body, **kw})
        return {"sent": 1, "removed": 0, "failed": 0}

    monkeypatch.setattr(reminders, "notify", fake)
    return calls


async def test_reminder_options_and_schedule(client, auth):
    off = (await client.post("/api/calendar/events", json={"title": "기본", "start": "2026-10-14T09:30"}, headers=auth)).json()
    assert off["reminder"] is None and off["remind_at"] is None  # 기본은 꺼짐
    t = (await client.post("/api/calendar/events", json={"title": "치과", "start": "2026-10-14T09:30", "reminder": "30m"},
                           headers=auth)).json()
    assert (t["reminder"], t["remind_at"]) == ("30m", "2026-10-14T09:00")
    d = (await client.post("/api/calendar/events", json={"title": "생일", "all_day": True, "start": "2026-10-20",
                                                         "reminder": "prev_day_9am"}, headers=auth)).json()
    assert d["remind_at"] == "2026-10-19T09:00"
    day = (await client.patch(f"/api/calendar/events/{d['id']}", json={"reminder": "day_9am"}, headers=auth)).json()
    assert day["remind_at"] == "2026-10-20T09:00"
    # 종류에 맞지 않는 옵션, 모르는 옵션은 422
    assert (await client.post("/api/calendar/events", json={"title": "x", "all_day": True, "start": "2026-10-20",
                                                            "reminder": "10m"}, headers=auth)).status_code == 422
    assert (await client.post("/api/calendar/events", json={"title": "x", "start": "2026-10-20T10:00",
                                                            "reminder": "5m"}, headers=auth)).status_code == 422
    # 시각을 옮기면 다시 계산하고, null 이면 끈다.
    moved = (await client.patch(f"/api/calendar/events/{t['id']}", json={"start": "2026-10-15T14:00", "end": "2026-10-15T15:00"},
                                headers=auth)).json()
    assert moved["remind_at"] == "2026-10-15T13:30"
    cleared = (await client.patch(f"/api/calendar/events/{t['id']}", json={"reminder": None}, headers=auth)).json()
    assert cleared["reminder"] is None and cleared["remind_at"] is None


async def test_send_due_once_late_catch_up_and_skip(client, auth, sent):
    mk = lambda title, start, rem: client.post("/api/calendar/events", json={"title": title, "start": start, "reminder": rem,
                                                                              "location": "강남"}, headers=auth)
    a = (await mk("회의", "2026-10-14T10:00", "10m")).json()
    await mk("점심", "2026-10-14T12:00", "1h")
    gone = (await mk("지운 일정", "2026-10-14T10:00", "10m")).json()
    await client.delete(f"/api/calendar/events/{gone['id']}", headers=auth)
    assert await reminders.send_due(_kst("2026-10-14T09:49")) == 0
    assert await reminders.send_due(_kst("2026-10-14T09:50")) == 1
    assert sent[0]["kind"] == PushKind.calendar_reminder and sent[0]["title"] == "회의"
    assert sent[0]["body"] == "10월 14일(수) 10:00 · 10분 후 · 강남"
    assert sent[0]["url"] == f"/calendar?event={a['id']}&date=2026-10-14" and sent[0]["tag"] == f"event-{a['id']}"
    # 같은 시각에 다시 훑어도(겹친 스캔·재시작) 두 번 보내지 않는다.
    assert await reminders.send_due(_kst("2026-10-14T09:51")) == 0
    assert (await client.get(f"/api/calendar/events/{a['id']}", headers=auth)).json()["reminder_sent"] is True
    # 서버가 꺼져 있었다가 늦게 떠도 일정 시작 뒤 1시간 안이면 보낸다.
    assert await reminders.send_due(_kst("2026-10-14T12:30")) == 1 and sent[1]["title"] == "점심"
    # 그보다 늦으면 보내지 않고 건너뛴 것으로 표시한다.
    c = (await mk("저녁", "2026-10-14T18:00", "at_start")).json()
    assert await reminders.send_due(_kst("2026-10-14T19:00")) == 0
    assert (await client.get(f"/api/calendar/events/{c['id']}", headers=auth)).json()["reminder_sent"] is True
    # 시각을 옮기면 다시 보낸다.
    await client.patch(f"/api/calendar/events/{a['id']}", json={"start": "2030-01-01T10:00", "end": "2030-01-01T11:00"}, headers=auth)
    assert await reminders.send_due(_kst("2030-01-01T10:00")) == 1 and sent[-1]["title"] == "회의"
    assert len(sent) == 3


async def test_reminder_past_event_is_not_sent(client, auth, sent):
    # 이미 지난 일정에 알림을 켜도 보내지 않는다(만드는 시각 기준).
    e = (await client.post("/api/calendar/events", json={"title": "지난 일", "start": "2020-01-01T10:00", "reminder": "10m"},
                           headers=auth)).json()
    assert e["reminder_sent"] is True
    assert await reminders.send_due() == 0 and sent == []


# ---- 에이전트 도구

async def test_agent_memo_tools_follow_forget_policy(client, auth, make_dir):
    h = _agent((await _session(client, auth, make_dir))["id"])
    mine = (await client.post("/api/agent/memos", json={"title": "이거 메모", "body": "내용", "tags": ["x"]}, headers=h)).json()
    assert mine["source"] == "agent"
    user = (await client.post("/api/memos", json={"title": "내 메모", "body": "원문"}, headers=auth)).json()
    # 사용자 메모: 덮어쓰기·삭제 금지, 덧붙이기·태그는 허용
    assert (await client.patch(f"/api/agent/memos/{user['id']}", json={"body": "덮어쓰기"}, headers=h)).status_code == 403
    ok = await client.patch(f"/api/agent/memos/{user['id']}", json={"append": "추가", "tags": ["봄"]}, headers=h)
    assert ok.status_code == 200 and ok.json()["body"] == "원문\n\n추가" and ok.json()["tags"] == ["봄"]
    assert (await client.patch(f"/api/agent/memos/{user['id']}", json={"append": "x", "rev": 1}, headers=h)).status_code == 409
    assert (await client.delete(f"/api/agent/memos/{user['id']}", headers=h)).status_code == 403
    # 에이전트 메모: 수정 가능, 삭제는 보관 처리(복구 가능한 소프트 삭제)
    assert (await client.patch(f"/api/agent/memos/{mine['id']}", json={"body": "고침"}, headers=h)).json()["body"] == "고침"
    assert (await client.delete(f"/api/agent/memos/{mine['id']}", headers=h)).json() == {"archived": True}
    assert (await client.get(f"/api/agent/memos/{mine['id']}", headers=h)).status_code == 404
    found = (await client.get("/api/agent/memos", params={"q": "원문"}, headers=h)).json()["memos"]
    assert [m["id"] for m in found] == [user["id"]]
    assert (await client.get("/api/agent/memos", headers=auth)).status_code == 401  # 사용자 토큰으로는 못 쓴다


async def test_agent_event_tools_and_next_week_listing(client, auth, make_dir):
    h = _agent((await _session(client, auth, make_dir))["id"])
    mine = (await client.post("/api/agent/events", json={"title": "에이전트 일정", "start": "2026-10-13T15:00", "reminder": "1h"},
                              headers=h)).json()
    assert mine["remind_at"] == "2026-10-13T14:00"
    assert (await client.post("/api/agent/events", json={"title": "x", "start": "2026-10-13T15:00", "rrule": "FREQ=DAILY"},
                              headers=h)).status_code == 422
    user = (await client.post("/api/calendar/events", json={"title": "사용자 일정", "all_day": True, "start": "2026-10-14"}, headers=auth)).json()
    got = (await client.get("/api/agent/events", params={"from": "2026-10-12", "to": "2026-10-18"}, headers=h)).json()
    assert [e["title"] for e in got["events"]] == ["에이전트 일정", "사용자 일정"]
    assert got["holidays"] == []
    hol = (await client.get("/api/agent/events", params={"from": "2026-10-05", "to": "2026-10-11"}, headers=h)).json()["holidays"]
    assert [x["date"] for x in hol] == ["2026-10-05", "2026-10-09"]
    assert (await client.patch(f"/api/agent/events/{user['id']}", json={"title": "고침"}, headers=h)).json()["title"] == "고침"
    assert (await client.delete(f"/api/agent/events/{user['id']}", headers=h)).status_code == 403
    assert (await client.delete(f"/api/agent/events/{mine['id']}", headers=h)).json() == {"archived": True}
    left = (await client.get("/api/calendar/events", params={"from": "2026-10-12", "to": "2026-10-18"}, headers=auth)).json()["events"]
    assert [e["id"] for e in left] == [user["id"]]
