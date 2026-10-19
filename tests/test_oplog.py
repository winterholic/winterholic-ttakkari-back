"""운영 로그·오류 이슈(ADR 0033): 그룹핑·마스킹·회귀·샘플링·인입·조회 API·크기 상한."""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import (
    OpsEvent,
    OpsIngestCursor,
    OpsIssue,
    OpsIssueStat,
    Run,
    RunEvent,
)
from ttakkari.oplog import deploy_logs, ingest
from ttakkari.oplog.normalize import fingerprint
from ttakkari.oplog.store import EVENTS_PER_ISSUE_PER_HOUR, EventIn, record
from ttakkari.orchestrator.runtime import issue_token

SECRET = "sk-ant-api03-ZZtopsecretvalue1234567890"
PW = "hunter2supersecret"
SHA = "b808e353c81de1d0e74b2009931720fa58cbd514"
SHA2 = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _clean_files():
    home = get_settings().home
    for sub in ("deploy", "logs"):
        shutil.rmtree(home / sub, ignore_errors=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    ingest._last_sync = 0.0
    yield


async def _agent_headers(client, auth, make_dir, pool="personal"):
    ws = (await client.post("/api/workspaces", json={"name": "ops", "root_path": str(make_dir())}, headers=auth)).json()
    s = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "pool": pool}, headers=auth)).json()
    return {"Authorization": f"Bearer {issue_token(uuid.UUID(s['id']))}"}


def _event(msg="boom", *, fp="fp-a", when=None, **kw) -> EventIn:
    return EventIn(source=kw.pop("source", "run"), level=kw.pop("level", "error"), message=msg, fingerprint=fingerprint(fp),
                   occurred_at=when or datetime.now(UTC), **kw)


async def _rec(ev: EventIn):
    async with sessionmaker()() as db:
        res = await record(db, ev)
        await db.commit()
        return res


def _job(status="failed", sha=SHA, error="release command failed; see x.log", log="", created=None):
    from ttakkari import deployment

    store = deployment.JobStore(deployment.state_dir())
    job = store.request(sha)
    job = store.update(job["id"], status=status, error=error, **({"created_at": created} if created else {}))
    (store.jobs / f"{job['id']}.log").write_text(log)
    return job


def _set_job_times(job_id: str, *, created: datetime, updated: datetime) -> None:
    from ttakkari import deployment

    path = deployment.JobStore(deployment.state_dir()).jobs / f"{job_id}.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps(data | {"created_at": created.isoformat(), "updated_at": updated.isoformat()}))


PYTEST_LOG = f"""Resolved 120 packages in 1.2s
============================= test session starts ==============================
collected 40 items

tests/test_x.py ..F

=================================== FAILURES ===================================
____________________________ test_dep ____________________________

    def test_dep():
>       assert call() == 1
E       AssertionError: assert 2 == 1

tests/test_x.py:12: AssertionError
connecting with password={PW} and Authorization: Bearer {SECRET}
=========================== short test summary info ============================
FAILED tests/test_x.py::test_dep - AssertionError: assert 2 == 1
!!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 2 passed in 3.21s
"""


# ---- 그룹핑·회귀·샘플링

async def test_events_with_same_fingerprint_group_into_one_issue_with_counts():
    t0 = datetime.now(UTC) - timedelta(hours=3)
    await _rec(_event("first", when=t0, release=SHA))
    await _rec(_event("second", when=t0 + timedelta(hours=2), release=SHA2))
    await _rec(_event("other", fp="fp-b"))
    async with sessionmaker()() as db:
        issues = list(await db.scalars(select(OpsIssue).order_by(OpsIssue.id)))
        assert [i.event_count for i in issues] == [2, 1]
        a = issues[0]
        assert a.first_seen == t0 and a.last_seen == t0 + timedelta(hours=2)
        assert (a.first_release, a.last_release) == (SHA, SHA2)


async def test_resolved_issue_reopens_on_new_event_but_not_on_late_old_log():
    now = datetime.now(UTC)
    first = await _rec(_event(when=now - timedelta(hours=5)))
    async with sessionmaker()() as db:
        issue = await db.get(OpsIssue, first.issue_id)
        issue.status, issue.resolved_at = "resolved", now - timedelta(hours=1)
        await db.commit()
    late_old = await _rec(_event("old line ingested late", when=now - timedelta(hours=3)))
    assert not late_old.regressed
    again = await _rec(_event("again", when=now))
    assert again.regressed
    async with sessionmaker()() as db:
        issue = await db.get(OpsIssue, first.issue_id)
        assert (issue.status, issue.regressed_count) == ("unresolved", 1)
        ev = await db.scalar(select(OpsEvent).where(OpsEvent.message == "again"))
        assert ev.tags["regression"] is True


async def test_ignored_issue_keeps_counting_without_reopening():
    first = await _rec(_event())
    async with sessionmaker()() as db:
        (await db.get(OpsIssue, first.issue_id)).status = "ignored"
        await db.commit()
    await _rec(_event("again"))
    async with sessionmaker()() as db:
        issue = await db.get(OpsIssue, first.issue_id)
        assert (issue.status, issue.event_count) == ("ignored", 2)


async def test_flood_is_sampled_but_counts_and_hourly_stats_stay_exact():
    when = datetime.now(UTC).replace(minute=5, second=0, microsecond=0)
    total = EVENTS_PER_ISSUE_PER_HOUR + 12
    for i in range(total):
        await _rec(_event(f"flood {i}", when=when + timedelta(seconds=i)))
    async with sessionmaker()() as db:
        issue = await db.scalar(select(OpsIssue))
        stored = await db.scalar(text("select count(*) from ops_events"))
        stat = await db.scalar(select(OpsIssueStat.count).where(OpsIssueStat.issue_id == issue.id))
        assert issue.event_count == total and stat == total
        assert stored == EVENTS_PER_ISSUE_PER_HOUR


async def test_duplicate_dedupe_key_is_ignored():
    assert await _rec(_event(dedupe_key="k1")) is not None
    assert await _rec(_event(dedupe_key="k1")) is None
    async with sessionmaker()() as db:
        assert (await db.scalar(select(OpsIssue))).event_count == 1


# ---- 비밀값: DB·API·MCP 어디에도 원문이 남지 않는다

async def _dump_all_tables() -> str:
    async with sessionmaker()() as db:
        rows = []
        for table in ("ops_issues", "ops_events", "ops_issue_stats", "ops_ingest_cursors", "ops_event_receipts"):
            rows += [str(tuple(r)) for r in (await db.execute(text(f"select * from {table}"))).all()]
    return "\n".join(rows)


async def test_secrets_never_reach_the_database():
    await _rec(_event(
        f"login failed password={PW} Authorization: Bearer {SECRET}", title=f"title token={PW}",
        stack=f"Traceback...\nRuntimeError: key {SECRET}", excerpt=f"Cookie: refresh={PW}\n--token {PW}",
        tags={"api_key": PW, "note": f"secret={PW}", "ok": "fine"},
        breadcrumbs=[{"message": f"connect postgresql://u:{PW}@h/db"}, f"Bearer {SECRET}"]))
    dump = await _dump_all_tables()
    assert PW not in dump and SECRET not in dump and "ZZtopsecret" not in dump
    assert "[REDACTED]" in dump and "fine" in dump


# ---- 배포 job 인입 + 로그 조회

async def test_failed_deploy_job_becomes_an_issue_with_stage_and_masked_excerpt(client, auth, make_dir):
    job = _job(log=PYTEST_LOG)
    _job(status="succeeded", sha=SHA2, error=None, log="ok\n", created=(datetime.now(UTC) + timedelta(seconds=5)).isoformat())
    h = await _agent_headers(client, auth, make_dir)

    r = await client.get("/api/agent/ops-logs/issues", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["source"] == "deploy" and item["release"] == SHA[:8]
    assert item["title"].startswith("deploy pytest failed")
    assert "test_dep" in item["title"]

    detail = (await client.get(f"/api/agent/ops-logs/issues/{item['id']}", headers=h)).json()
    ev = detail["latest_event"]
    assert ev["tags"]["job_id"] == job["id"] and ev["tags"]["stage"] == "pytest"
    assert "AssertionError" in ev["excerpt"] and ev["breadcrumbs"]
    for body in (json.dumps(detail), await _dump_all_tables()):
        assert PW not in body and SECRET not in body

    # 같은 job을 다시 인입해도 늘지 않는다
    await ingest.sync_all(force=True)
    assert (await client.get("/api/agent/ops-logs/issues", headers=h)).json()["items"][0]["count"] == 1


async def test_same_cause_on_different_deploys_groups_and_new_sha_is_tagged():
    _job(log=PYTEST_LOG, sha=SHA, created="2026-10-10T00:00:00+00:00")
    _job(log=PYTEST_LOG.replace("3.21s", "9.99s"), sha=SHA2, created="2026-10-11T00:00:00+00:00")
    await ingest.sync_all(force=True)
    async with sessionmaker()() as db:
        issue = await db.scalar(select(OpsIssue))
        assert issue.event_count == 2 and {issue.first_release, issue.last_release} == {SHA, SHA2}


async def test_deploy_log_modes_resolve_refs_and_mask(client, auth, make_dir):
    job = _job(log=PYTEST_LOG)
    h = await _agent_headers(client, auth, make_dir)
    base = "/api/agent/ops-logs/deploys"

    for ref in ("latest_failed", "latest", job["id"], job["id"][:8], SHA[:10]):
        r = await client.get(f"{base}/{ref}/log", headers=h)
        assert r.status_code == 200, (ref, r.text)
        body = r.json()
        assert body["job_id"] == job["id"] and body["stage"] == "pytest"
        assert "test_dep" in body["cause"]
        assert any("AssertionError" in s["text"] for s in body["sections"])
        assert PW not in r.text and SECRET not in r.text

    tail = (await client.get(f"{base}/latest/log", params={"mode": "tail", "lines": 3}, headers=h)).json()
    assert tail["text"].count("\n") == 2 and "passed" in tail["text"]
    rng = (await client.get(f"{base}/latest/log", params={"mode": "range", "offset": 1, "lines": 5}, headers=h)).json()
    assert rng["from_line"] == 1 and rng["next_offset"] == 6 and rng["text"].startswith("1| Resolved")

    listing = (await client.get(base, headers=h)).json()["items"]
    assert listing[0]["status"] == "failed" and listing[0]["stage"] == "pytest" and listing[0]["revision"] == SHA[:8]


@pytest.mark.parametrize("ref", ["../../etc/passwd", "..%2F..%2Fetc", "zzzz", "nonexistent0", "x" * 80])
async def test_deploy_log_ref_cannot_escape_the_jobs_directory(client, auth, make_dir, ref):
    _job(log="hello")
    h = await _agent_headers(client, auth, make_dir)
    r = await client.get(f"/api/agent/ops-logs/deploys/{ref}/log", headers=h)
    assert r.status_code in (404, 422) and "root:" not in r.text


async def test_stage_is_inferred_from_log_when_error_text_has_no_stage():
    migrate = _job(log="Running upgrade 0038 -> 0039\nsqlalchemy.exc.ProgrammingError: relation x\n", sha=SHA2,
                   created="2026-10-12T00:00:00+00:00")
    lines, _ = deploy_logs.read_log(migrate["id"])
    assert deploy_logs.infer_stage(migrate, lines) == "migrate"
    staged = {"error": "release stage npm ci failed (exit 1); last 12 log lines:\nx"}
    assert deploy_logs.infer_stage(staged, []) == "npm ci"


async def test_large_deploy_log_is_bounded(client, auth, make_dir):
    noisy = "\n".join(f"line {i} ERROR something broke {'x' * 400}" for i in range(5000))
    _job(log=noisy)
    h = await _agent_headers(client, auth, make_dir)
    for mode in ("summary", "errors", "tail"):
        r = await client.get("/api/agent/ops-logs/deploys/latest/log", params={"mode": mode, "lines": 200}, headers=h)
        assert r.status_code == 200 and len(r.text) <= 16000, (mode, len(r.text))
    small = await client.get("/api/agent/ops-logs/deploys/latest/log", params={"max_chars": 500}, headers=h)
    assert len(small.text) <= 500 and small.json()["truncated"] is True


# ---- Run 실패·폴백

async def test_failed_runs_and_fallback_events_are_ingested_with_breadcrumbs_and_release(new_session):
    ws, sess = await new_session()
    now = datetime.now(UTC)
    job = _job(status="succeeded", sha=SHA, error=None, log="")
    _set_job_times(job["id"], created=now - timedelta(hours=2), updated=now - timedelta(hours=2))
    async with sessionmaker()() as db:
        runs = []
        for n in (1, 2):
            run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]), prompt="x", engine="antigravity",
                      model="flash", status="failed", finished_at=now - timedelta(minutes=n), last_seq=0,
                      error=f"unknown option --effort high (attempt {n}) token={PW}")
            db.add(run)
            runs.append(run)
        await db.flush()
        for run in runs:
            for seq, (typ, payload) in enumerate([("run.status", {"status": "running"}),
                                                  ("log", {"stream": "stderr", "text": f"Bearer {SECRET}"}),
                                                  ("engine.fallback", {"from_engine": "antigravity", "from_model": "flash", "to_engine": "claude",
                                                                      "to_model": "opus", "reason": "usage_limit", "detail": "limit 5h"})], 1):
                db.add(RunEvent(run_id=run.id, seq=seq, type=typ, payload=payload, created_at=now - timedelta(minutes=3)))
        await db.commit()
    assert (await ingest.sync_all(force=True)) is not None
    async with sessionmaker()() as db:
        run_issue = await db.scalar(select(OpsIssue).where(OpsIssue.source == "run"))
        assert run_issue.event_count == 2  # (attempt N)이 달라도 한 이슈
        ev = await db.scalar(select(OpsEvent).where(OpsEvent.issue_id == run_issue.id).order_by(OpsEvent.id))
        assert ev.tags["engine"] == "antigravity" and ev.release == SHA
        assert any("engine.fallback" in str(b) for b in ev.breadcrumbs)
        assert (await db.scalar(select(OpsIssue).where(OpsIssue.source == "worker"))).level == "warning"
    dump = await _dump_all_tables()
    assert PW not in dump and SECRET not in dump
    first = await _count_events()
    await ingest.sync_all(force=True)
    assert await _count_events() == first


async def _count_events() -> int:
    async with sessionmaker()() as db:
        return await db.scalar(text("select count(*) from ops_events"))


# ---- 프로세스 로그 파일

BACKEND_LOG = f"""INFO:     127.0.0.1:5000 - "GET /api/health HTTP/1.1" 200 OK
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "/x/src/ttakkari/api/foo.py", line 5, in handler
    raise ValueError("covered by the api handler")
ValueError: covered by the api handler
2026-10-17 10:01:00,000 ERROR push notification failed for run 42
Traceback (most recent call last):
  File "/x/src/ttakkari/push/service.py", line 77, in send
    await post(headers={{"Authorization": "Bearer {SECRET}"}})
httpx.ConnectError: connection refused password={PW}
2026-10-17 10:02:00,000 INFO fine again
"""


DEPLOYER_LOG = f"""2026-10-17 10:00:00,123 ERROR deployment {uuid.uuid4()} failed
Traceback (most recent call last):
  File "/x/src/ttakkari/deployment.py", line 10, in process
    raise RuntimeError("covered by the job event")
RuntimeError: covered by the job event
2026-10-17 10:00:01,000 INFO next
"""


async def test_process_log_blocks_are_ingested_once_incrementally_and_skip_covered_blocks():
    log = get_settings().home / "logs" / "backend.log"
    log.write_text(BACKEND_LOG)
    deployer = log.with_name("deployer.log")
    deployer.write_text(DEPLOYER_LOG)  # job 이벤트가 이미 다루므로 건너뛴다
    import os
    old = datetime.now(UTC).timestamp() - 60
    os.utime(log, (old, old))
    os.utime(deployer, (old, old))
    await ingest.sync_all(force=True)
    async with sessionmaker()() as db:
        issues = list(await db.scalars(select(OpsIssue).where(OpsIssue.source == "process")))
        assert [i.title for i in issues] == ["httpx.ConnectError: connection refused password=[REDACTED]"]
        ev = await db.scalar(select(OpsEvent))
        assert issues[0].culprit == "ttakkari/push/service.py:send"
        assert ev.occurred_at.astimezone().strftime("%H:%M") == "10:01" and ev.tags["time_estimated"] is False
    dump = await _dump_all_tables()
    assert PW not in dump and SECRET not in dump

    await ingest.sync_all(force=True)
    assert await _count_events() == 1  # 멱등
    async with sessionmaker()() as db:
        cursor = await db.get(OpsIngestCursor, "file:backend.log")
        assert cursor.position == log.stat().st_size  # 다음엔 이어서 읽는다(처음부터 다시 읽지 않는다)

    with log.open("a") as f:
        f.write("2026-10-17 11:00:00,000 ERROR something new happened id 77\n")
    os.utime(log, (old + 1, old + 1))
    await ingest.sync_all(force=True)
    assert await _count_events() == 2

    log.unlink()  # 로그 교체(로테이션): 새 inode 는 처음부터 읽는다
    log.write_text("2026-10-17 12:00:00,000 ERROR something new happened id 78\n")
    os.utime(log, (old + 2, old + 2))
    await ingest.sync_all(force=True)
    async with sessionmaker()() as db:
        issue = await db.scalar(select(OpsIssue).where(OpsIssue.title.like("something new happened%")))
        assert issue.event_count == 2  # id 77·78 은 같은 이슈


async def test_unfinished_trailing_traceback_waits_for_more_lines():
    log = get_settings().home / "logs" / "backend.log"
    log.write_text("2026-10-17 10:00:00,000 ERROR boom\nTraceback (most recent call last):\n  File \"/x/src/a.py\", line 1, in f\n    x()\n")
    await ingest.sync_all(force=True)
    assert await _count_events() == 0  # 방금 쓰는 중인 블록은 보류
    with log.open("a") as f:
        f.write("ValueError: late\n2026-10-17 10:00:05,000 INFO next\n")
    await ingest.sync_all(force=True)
    assert await _count_events() == 1


# ---- FastAPI 미처리 예외 훅

async def test_unhandled_api_exception_is_captured_with_masked_message(client):
    from ttakkari.main import app

    async def boom():
        raise RuntimeError(f"db password={PW} failed")

    before = len(app.router.routes)
    app.add_api_route("/api/_ops_boom/{token}", boom, methods=["GET"])
    try:
        with pytest.raises(RuntimeError):
            await client.get("/api/_ops_boom/" + "a" * 40)
    finally:
        del app.router.routes[before:]  # 전역 앱에 남으면 뒤에 도는 test_route_inventory 가 분류 없는 라우트로 실패한다
    async with sessionmaker()() as db:
        issue = await db.scalar(select(OpsIssue).where(OpsIssue.source == "api"))
        ev = await db.scalar(select(OpsEvent).where(OpsEvent.issue_id == issue.id))
    assert issue.title.startswith("RuntimeError") and PW not in issue.title
    assert ev.tags["method"] == "GET" and "a" * 40 not in str(ev.tags) and "<id>" in ev.tags["path"]
    assert PW not in await _dump_all_tables()


# ---- 조회 API: 검색·페이지·상한·접근 제어·상태 변경

async def _seed_many(n=60):
    base = datetime.now(UTC) - timedelta(minutes=n)
    for i in range(n):
        await _rec(_event(f"unique failure kind {i}", fp=f"k{i}", when=base + timedelta(minutes=i), source="run" if i % 2 else "deploy",
                          level="warning" if i % 5 == 0 else "error", excerpt="e" * 4000, stack="s" * 4000,
                          release=SHA if i % 3 else SHA2, tags={"engine": "claude" if i % 2 else "codex"}))


async def test_list_search_filters_and_pagination(client, auth, make_dir):
    await _seed_many(30)
    h = await _agent_headers(client, auth, make_dir)
    get = lambda path, **p: client.get(f"/api/agent/ops-logs{path}", params=p, headers=h)

    page1 = (await get("/issues", limit=10)).json()
    assert page1["total"] == 30 and len(page1["items"]) == 10 and page1["next_cursor"] == 10
    page2 = (await get("/issues", limit=10, cursor=10)).json()
    assert not {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]}
    assert (await get("/issues", limit=10, cursor=20)).json()["next_cursor"] is None

    assert (await get("/issues", source="deploy", limit=50)).json()["total"] == 15
    assert (await get("/issues", level="warning", limit=50)).json()["total"] == 6
    assert (await get("/issues", release=SHA2[:7], limit=50)).json()["total"] == 10
    assert (await get("/issues", tag="engine:codex", limit=50)).json()["total"] == 15
    assert (await get("/issues", q="kind 7", limit=50)).json()["total"] == 1
    assert (await get("/issues", sort="count", limit=1)).status_code == 200

    hits = (await get("/events", q="KIND 12", since_hours=24)).json()
    assert hits["total"] == 1 and "kind 12" in hits["items"][0]["text"]
    assert (await get("/events", since_hours=1, until_hours=1)).json()["total"] == 0
    assert (await get("/issues", status="bogus")).status_code == 422
    assert (await get("/issues", tag="nocolon")).status_code == 422
    assert (await get("/issues", limit=500)).status_code == 422


async def test_responses_never_exceed_the_cap_even_for_huge_events(client, auth, make_dir):
    await _seed_many(50)
    h = await _agent_headers(client, auth, make_dir)
    for path, params in [("/issues", {"limit": 50, "max_chars": 16000}), ("/events", {"limit": 50, "max_chars": 16000}),
                         ("/issues/1", {"events": 10, "max_chars": 16000}), ("/issues", {"limit": 50}), ("/issues/1", {})]:
        r = await client.get(f"/api/agent/ops-logs{path}", params=params, headers=h)
        assert r.status_code == 200 and len(r.text) <= 16000, (path, len(r.text))
    default = await client.get("/api/agent/ops-logs/issues", params={"limit": 50}, headers=h)
    assert len(default.text) <= 6000 and default.json()["truncated"] is True
    assert (await client.get("/api/agent/ops-logs/issues", params={"max_chars": 99999}, headers=h)).status_code == 422


async def test_issue_detail_update_note_and_regression_cycle(client, auth, make_dir):
    first = await _rec(_event("disk full", release=SHA, stack="Traceback...\nOSError: disk full", breadcrumbs=[{"message": "step 1"}]))
    h = await _agent_headers(client, auth, make_dir)
    iid = f"LOG-{first.issue_id}"
    detail = (await client.get(f"/api/agent/ops-logs/issues/{iid}", headers=h)).json()
    assert detail["latest_event"]["stack"].endswith("OSError: disk full") and detail["latest_event"]["breadcrumbs"] == [{"message": "step 1"}]
    assert detail["releases"] == [{"release": SHA[:8], "events": 1}]

    r = await client.patch(f"/api/agent/ops-logs/issues/{iid}", headers=h,
                           json={"status": "resolved", "release": SHA2, "note": f"cleared cache; token={PW}"})
    assert r.status_code == 200 and r.json()["issue"]["status"] == "resolved" and PW not in r.text
    assert (await client.get("/api/agent/ops-logs/issues", headers=h)).json()["total"] == 0
    assert (await client.get("/api/agent/ops-logs/issues", params={"status": "resolved"}, headers=h)).json()["total"] == 1

    await _rec(_event("disk full", release=SHA2))
    after = (await client.get(f"/api/agent/ops-logs/issues/{iid}", headers=h)).json()["issue"]
    assert after["status"] == "unresolved" and after["regressed"] == 1
    assert after["notes"][0]["by"] == "agent" and PW not in json.dumps(after)

    assert (await client.get("/api/agent/ops-logs/issues/LOG-9999", headers=h)).status_code == 404
    assert (await client.get("/api/agent/ops-logs/issues/abc", headers=h)).status_code == 422
    assert (await client.patch(f"/api/agent/ops-logs/issues/{iid}", headers=h, json={"status": "deleted"})).status_code == 422


async def test_trend_counts_by_hour_and_day(client, auth):
    now = datetime.now(UTC)
    for i in range(3):
        await _rec(_event(f"a{i}", when=now - timedelta(minutes=i)))
    await _rec(_event("b", fp="other", when=now - timedelta(hours=26)))
    hourly = (await client.get("/api/ops-logs/trend", params={"hours": 48}, headers=auth)).json()
    assert hourly["total"] == 4 and len(hourly["points"]) >= 48
    daily = (await client.get("/api/ops-logs/trend", params={"hours": 72, "unit": "day"}, headers=auth)).json()
    assert daily["total"] == 4 and len(daily["points"]) in (3, 4)


async def test_access_control_user_agent_and_company_sessions(client, auth, make_dir):
    await _rec(_event())
    assert (await client.get("/api/ops-logs/issues")).status_code == 401
    assert (await client.get("/api/agent/ops-logs/issues")).status_code == 401
    assert (await client.get("/api/ops-logs/issues", headers=auth)).status_code == 200
    assert (await client.get("/api/agent/ops-logs/issues", headers=auth)).status_code in (401, 403)
    work = await _agent_headers(client, auth, make_dir, pool="work")
    for path in ("/issues", "/events", "/deploys", "/deploys/latest/log", "/trend"):
        assert (await client.get(f"/api/agent/ops-logs{path}", headers=work)).status_code == 403, path
    assert (await client.patch("/api/agent/ops-logs/issues/1", headers=work, json={"status": "ignored"})).status_code == 403
    assert (await client.patch("/api/ops-logs/issues/1", headers=auth, json={"status": "ignored", "note": "user"})).status_code == 200


async def test_responses_are_masked_even_if_a_secret_slipped_into_storage(client, auth, make_dir):
    """저장 단계 마스킹이 뚫렸다고 가정하고 응답 단계가 따로 막는지 본다."""
    res = await _rec(_event("clean message"))
    async with sessionmaker()() as db:
        await db.execute(text("update ops_events set excerpt=:x, stack=:y, message=:z where issue_id=:i"),
                         {"x": f"Authorization: Bearer {SECRET}", "y": f"token={PW}", "z": f"leak {SECRET}", "i": res.issue_id})
        await db.execute(text("update ops_issues set title=:t where id=:i"), {"t": f"title {SECRET}", "i": res.issue_id})
        await db.commit()
    h = await _agent_headers(client, auth, make_dir)
    for path in ("/issues", "/events", f"/issues/{res.issue_id}"):
        r = await client.get(f"/api/agent/ops-logs{path}", headers=h)
        assert SECRET not in r.text and PW not in r.text, path


def test_mcp_tool_responses_are_capped_by_the_server_guard(monkeypatch):
    import asyncio

    from ttakkari.orchestrator import mcp_server

    async def fake_call(*args, **kwargs):
        return json.dumps({"items": [{"t": "z" * 5000} for _ in range(200)]})

    monkeypatch.setattr(mcp_server, "_call", fake_call)
    out = asyncio.run(mcp_server.ops_list_issues())
    assert len(out) <= 16000 and json.loads(out)["truncated"] is True


async def test_sampled_out_event_is_counted_once_even_after_retries():
    when = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for i in range(EVENTS_PER_ISSUE_PER_HOUR):
        await _rec(_event(when=when, dedupe_key=f'flood:{i}'))
    event = _event('not retained', when=when, dedupe_key='flood:sampled')
    result = await _rec(event)
    assert result is not None and not result.stored
    assert await _rec(event) is None
    async with sessionmaker()() as db:
        assert (await db.scalar(select(OpsIssue))).event_count == EVENTS_PER_ISSUE_PER_HOUR + 1
        assert await db.scalar(select(OpsIssueStat.count)) == EVENTS_PER_ISSUE_PER_HOUR + 1


async def test_regression_keeps_diagnostic_evidence_after_sample_budget_exhausted():
    when = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for i in range(EVENTS_PER_ISSUE_PER_HOUR):
        first = await _rec(_event(when=when, dedupe_key=f'fill:{i}'))
    async with sessionmaker()() as db:
        issue = await db.get(OpsIssue, first.issue_id)
        issue.status, issue.resolved_at = 'resolved', when
        await db.commit()
    result = await _rec(_event('regression evidence', when=when + timedelta(seconds=1), release=SHA2))
    assert result.regressed and result.stored
    async with sessionmaker()() as db:
        assert await db.scalar(select(OpsEvent.id).where(OpsEvent.message == 'regression evidence')) is not None


async def test_size_limited_pagination_never_skips_unreturned_rows():
    from ttakkari.oplog import views
    await _seed_many(18)
    for query in (views.list_issues, views.search_events):
        got, cursor = [], 0
        async with sessionmaker()() as db:
            for _ in range(20):
                page = await query(db, limit=10, cursor=cursor, max_chars=1400)
                assert page['items'], page
                got.extend(row.get('id', row.get('event_id')) for row in page['items'])
                if page['next_cursor'] is None:
                    break
                assert page['next_cursor'] == cursor + len(page['items'])
                cursor = page['next_cursor']
        assert len(got) == len(set(got)) == 18


async def test_runs_with_the_same_finish_time_are_not_lost_at_the_batch_boundary(new_session):
    ws, sess = await new_session()
    when = (datetime.now(UTC) - timedelta(minutes=1)).replace(microsecond=0)
    async with sessionmaker()() as db:
        for i in range(205):
            db.add(Run(session_id=uuid.UUID(sess['id']), workspace_id=uuid.UUID(ws['id']), prompt='x', engine='codex',
                       status='failed', finished_at=when, last_seq=0, error='shared failure'))
        await db.commit()
    for _ in range(3):
        await ingest.sync_all(force=True)
    async with sessionmaker()() as db:
        assert (await db.scalar(select(OpsIssue).where(OpsIssue.source == 'run'))).event_count == 205


async def test_late_committed_run_event_below_the_id_cursor_is_still_ingested(new_session):
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(sess['id']), workspace_id=uuid.UUID(ws['id']), prompt='x', engine='codex', status='completed', last_seq=0)
        db.add(run)
        await db.flush()
        run_id = run.id
        db.add(RunEvent(id=20, run_id=run_id, seq=2, type='engine.fallback', payload={'reason': 'usage_limit'}))
        await db.commit()
    await ingest.sync_all(force=True)
    async with sessionmaker()() as db:
        db.add(RunEvent(id=10, run_id=run_id, seq=1, type='engine.fallback', payload={'reason': 'usage_limit'}))
        await db.commit()
    await ingest.sync_all(force=True)
    async with sessionmaker()() as db:
        assert (await db.scalar(select(OpsIssue))).event_count == 2


async def test_integrity_failures_are_visible_and_retryable_not_silently_discarded():
    from sqlalchemy.exc import IntegrityError
    async with sessionmaker()() as db:
        event = _event('invalid database value')
        event.fingerprint = None
        with pytest.raises(IntegrityError):
            await record(db, event)
        await db.rollback()


async def test_concurrent_retries_increment_the_issue_once():
    import asyncio
    event = _event('concurrent', dedupe_key='one-source-event')
    results = await asyncio.gather(*(_rec(event) for _ in range(6)))
    assert sum(result is not None for result in results) == 1
    async with sessionmaker()() as db:
        assert (await db.scalar(select(OpsIssue))).event_count == 1


async def test_failed_transaction_does_not_leave_a_processed_receipt():
    from sqlalchemy.exc import IntegrityError
    event = _event(dedupe_key='retry-after-error')
    event.fingerprint = None
    async with sessionmaker()() as db:
        with pytest.raises(IntegrityError):
            await record(db, event)
        await db.rollback()
    assert await _rec(_event(dedupe_key='retry-after-error')) is not None
    assert await _rec(_event(dedupe_key='retry-after-error')) is None


async def test_sync_step_failure_rolls_back_only_that_source(monkeypatch):
    async def deploy(db):
        return int(await record(db, _event('deploy', fp='deploy', dedupe_key='test-deploy')) is not None)

    async def runs(db):
        await record(db, _event('failed step', fp='rollback', dedupe_key='test-failed'))
        raise RuntimeError('synthetic source failure')

    async def process(db):
        return int(await record(db, _event('process', fp='process', dedupe_key='test-process')) is not None)

    monkeypatch.setattr(ingest, 'sync_deploy_jobs', deploy)
    monkeypatch.setattr(ingest, 'sync_runs', runs)
    monkeypatch.setattr(ingest, 'sync_process_logs', process)
    assert await ingest.sync_all(force=True) == {'deploy': 1, 'process': 1}
    async with sessionmaker()() as db:
        assert set(await db.scalars(select(OpsIssue.title))) == {'deploy', 'process'}
    assert await ingest.sync_all(force=True) == {'deploy': 0, 'process': 0}


async def test_titles_and_breadcrumbs_are_scrubbed_before_they_are_clipped():
    # 토큰 형식/환경값이 아닌 평문 비밀: 닫는 따옴표가 잘리면 문자열 마스킹도 실패한다.
    secret = 'ordinary-private-fragment-' * 20
    block = ['2026-10-17 10:00:00,000 ERROR error', 'Traceback (most recent call last):',
             '  File "/x/src/handler.py", line 3, in run', f'RuntimeError: password="{secret}"']
    event = ingest._block_event('backend.log', 1, 0, block, datetime.now(UTC), False, [])
    crumb = ingest._crumb(RunEvent(type='log', payload={'stream': 'stderr', 'text': f'password="{secret}"'}, created_at=datetime.now(UTC)))
    event.breadcrumbs = [crumb]
    await _rec(event)
    dump = await _dump_all_tables()
    assert 'ordinary-private-fragment' not in dump
