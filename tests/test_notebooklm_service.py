"""NotebookLM 서비스·REST·MCP 통합 검사(ADR 0045). 실제 DB(전용 테스트 DB)와 가짜 백엔드를 쓴다. Google 요청은 없다."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.google_account import login
from ttakkari.google_account.service import service as accounts
from ttakkari.models import (
    ChatSession,
    GoogleAccount,
    NotebookLMAccount,
    NotebookLMJob,
    NotebookLMSource,
    Notification,
    Workspace,
)
from ttakkari.notebooklm import errors, metrics, redact, session
from ttakkari.notebooklm.ports import (
    Capability,
    Citation,
    NotebookDetail,
    NotebookLMBackend,
    NotebookSummary,
    QueryResult,
    SourceInfo,
    SourceRef,
    Span,
)
from ttakkari.notebooklm.service import NotebookLMService, service
from ttakkari.orchestrator.runtime import issue_token

SID = "g.a000-fake-sid-value-0123456789abcdefghij"
TS = "fake-sidts-value-0123456789abcdefghijk"


def _cookies():
    base = {"domain": ".google.com", "path": "/", "expires": 4102444800, "httpOnly": True, "secure": True}
    return [{**base, "name": "SID", "value": SID}, {**base, "name": "__Secure-1PSIDTS", "value": TS}]


class FakeBackend:
    """NotebookLMBackend 가짜 구현. ops 별로 던질 예외(또는 호출 가능 객체)를 순서대로 지정한다."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.plan: dict[str, list] = {}
        self.sources: dict[str, list[SourceInfo]] = {"nb1": []}
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.seq = 0

    def count(self, op: str) -> int:
        return sum(1 for c, _ in self.calls if c == op)

    async def _step(self, op: str, *args):
        self.calls.append((op, args))
        planned = self.plan.get(op)
        if planned:
            nxt = planned.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            if callable(nxt):
                return await nxt()

    async def verify_session(self):
        await self._step("verify")

    async def list_notebooks(self):
        await self._step("list_notebooks")
        return [NotebookSummary(id="nb1", title="첫 노트북", source_count=len(self.sources["nb1"]))]

    async def get_notebook(self, notebook_id):
        await self._step("get_notebook", notebook_id)
        return NotebookDetail(id=notebook_id, title="첫 노트북", sources=list(self.sources.get(notebook_id, [])))

    async def create_notebook(self, title):
        await self._step("create_notebook", title)
        return NotebookSummary(id="nb-new", title=title)

    async def rename_notebook(self, notebook_id, title):
        await self._step("rename_notebook", notebook_id, title)
        return NotebookSummary(id=notebook_id, title=title)

    async def delete_notebook(self, notebook_id):
        await self._step("delete_notebook", notebook_id)

    async def list_sources(self, notebook_id):
        await self._step("list_sources", notebook_id)
        return list(self.sources.get(notebook_id, []))

    async def _add(self, op, notebook_id, kind, url, title):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        await self._step(op, notebook_id, url or title)
        self.seq += 1
        src = SourceInfo(id=f"src{self.seq}", title=title or url, kind=kind, url=url, status="processing")
        self.sources.setdefault(notebook_id, []).append(src)
        return src

    async def add_url(self, notebook_id, url, *, title=None):
        return await self._add("add_url", notebook_id, "web_page", url, title)

    async def add_text(self, notebook_id, title, text):
        return await self._add("add_text", notebook_id, "pasted_text", None, title)

    async def add_file(self, notebook_id, path, *, title=None):
        return await self._add("add_file", notebook_id, "pdf", None, title)

    async def wait_source_ready(self, notebook_id, source_id, *, timeout):
        await self._step("wait_source_ready", source_id)
        return SourceInfo(id=source_id, title="t", status="ready")

    async def delete_source(self, notebook_id, source_id):
        await self._step("delete_source", source_id)
        self.sources[notebook_id] = [s for s in self.sources.get(notebook_id, []) if s.id != source_id]

    async def ask(self, notebook_id, question, *, conversation_id=None, source_ids=None):
        await self._step("ask", question)
        return QueryResult(
            notebook_id=notebook_id, query=question, answer="분석 [1]", conversation_id="cv", turn_number=1,
            citations=[Citation(number=1, source_id="s1", quoted_text="원문 발췌", source_span=Span(start=1, end=4),
                                answer_span=Span(start=3, end=6))],
            sources=[SourceRef(id="s1", citation_numbers=[1])], created_at=datetime.now(UTC).isoformat())

    def capabilities(self):
        return [Capability(name="notebooks", available=True), Capability(name="audio_overview", available=False, reason="미지원")]

    async def aclose(self):
        self.calls.append(("aclose", ()))


@pytest.fixture
def fake(monkeypatch):
    backend = FakeBackend()
    svc_before = (service._factory, dict(service._backends))
    service.set_backend_factory(lambda _a: backend)
    service._sems.clear()
    service._last_verify.clear()
    metrics.reset()
    redact.clear_secrets()
    st = get_settings()
    monkeypatch.setattr(st, "notebooklm_read_retry_base_seconds", 0.0)
    monkeypatch.setattr(st, "notebooklm_read_retry_cap_seconds", 0.0)
    session.save_cookies("primary", _cookies())
    yield backend
    session.delete("primary")
    service._factory, _ = svc_before
    service._backends.clear()
    login.manager = login.LoginManager()


async def _state(account="primary") -> NotebookLMAccount | None:
    async with sessionmaker()() as db:
        return await db.get(NotebookLMAccount, account)


async def _account(account="primary") -> GoogleAccount | None:
    async with sessionmaker()() as db:
        return await db.get(GoogleAccount, account)


async def _wait_job(client, auth, job_id, until=("succeeded", "failed", "cancelled", "unknown"), seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        job = (await client.get(f"/api/notebooklm/jobs/{job_id}", headers=auth)).json()
        if job["status"] in until:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job stuck: {job}")


# ------------------------------------------------------------------ 상태·인증

async def test_status_without_session_makes_no_request(client, auth):
    session.delete("primary")
    spy = FakeBackend()
    service.set_backend_factory(lambda _a: spy)
    try:
        r = await client.get("/api/notebooklm/auth/status", headers=auth)
    finally:
        service.set_backend_factory(None)
    body = r.json()
    assert spy.calls == []  # 상태 조회는 백엔드 메서드(=Google 요청)를 부르지 않는다
    assert r.status_code == 200 and body["state"] == "login_required" and body["session"]["stored"] is False
    assert body["session"]["encrypted"] is False and "0600" in body["session"]["protection"]


async def test_status_lists_supported_and_unavailable_features_without_secrets(client, auth, fake):
    r = await client.get("/api/notebooklm/auth/status", headers=auth)
    body = r.json()
    assert body["state"] == "session_saved" and fake.calls == []  # 파일만 있고 접속 확인 전, 요청 없음
    caps = {c["name"]: c for c in body["capabilities"]}
    assert caps["audio_overview"]["available"] is False
    assert SID not in r.text and TS not in r.text and set(body["session"]["cookie_names"]) == {"SID", "__Secure-1PSIDTS"}
    assert "profile" not in json.dumps(body).lower().replace("profile_", "")


async def test_verify_marks_ready_and_is_rate_limited(client, auth, fake):
    r = await client.post("/api/notebooklm/auth/verify", headers=auth)
    assert r.status_code == 200 and r.json()["state"] == "ready" and fake.count("verify") == 1
    again = await client.post("/api/notebooklm/auth/verify", headers=auth)
    assert again.status_code == 429 and again.json()["code"] == "too_soon" and "Retry-After" in again.headers
    assert fake.count("verify") == 1


async def test_login_required_blocks_calls_before_google(client, auth):
    session.delete("primary")
    spy = FakeBackend()
    service.set_backend_factory(lambda _a: spy)
    try:
        r = await client.get("/api/notebooklm/notebooks", headers=auth)
    finally:
        service.set_backend_factory(None)
    assert r.status_code == 409 and r.json()["code"] == "auth_required" and spy.calls == []


async def test_login_window_endpoints_use_manager(client, auth, fake, monkeypatch):
    started = {}

    async def fake_start(account_id, on_done=None, feature=None):
        started["id"] = account_id
        return login.LoginState(account_id=account_id, state="waiting", message="창이 열렸습니다.")

    monkeypatch.setattr(login.manager, "start", fake_start)
    r = await client.post("/api/notebooklm/auth/login-window", headers=auth)
    assert r.status_code == 202 and r.json()["state"] == "waiting" and started["id"] == "primary"
    monkeypatch.setattr(login.manager, "start", lambda *a, **k: (_ for _ in ()).throw(errors.Busy("이미 열려 있습니다.")))
    async def busy(*a, **k):
        raise errors.Busy("이미 열려 있습니다.")
    monkeypatch.setattr(login.manager, "start", busy)
    r = await client.post("/api/notebooklm/auth/login-window", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "busy"

    completed = {}

    async def fake_complete(account_id):
        completed["id"] = account_id
        return login.LoginState(account_id=account_id, state="done", message="로그인이 끝났습니다.")

    monkeypatch.setattr(login.manager, "complete", fake_complete)
    rc = await client.post("/api/notebooklm/auth/login-window/complete", headers=auth)
    assert rc.status_code == 200 and rc.json()["state"] == "done" and completed["id"] == "primary"


async def test_session_expiry_marks_reauth_and_notifies_once(client, auth, fake):
    fake.plan["list_notebooks"] = [errors.AuthRequired("expired")]
    r = await client.get("/api/notebooklm/notebooks", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "auth_required"
    row = await _account()  # 재인증 필요는 계정 수준 상태다
    assert row.state == "reauth_required" and row.reauth_notified_at is not None
    assert ("aclose", ()) in fake.calls  # 만료된 클라이언트를 닫았다
    before = fake.count("list_notebooks")
    for _ in range(3):  # 이후 호출은 Google 에 나가지 않고 알림도 더 만들지 않는다
        again = await client.get("/api/notebooklm/notebooks", headers=auth)
        assert again.status_code == 409
    assert fake.count("list_notebooks") == before
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(Notification).where(Notification.kind == "push.google_account")))
    assert len(rows) == 1 and "로그인" in rows[0].title
    status = (await client.get("/api/notebooklm/auth/status", headers=auth)).json()
    assert status["state"] == "reauth_required" and status["reauth_required"] is True

    # 다시 로그인하면 해제되고 접속 확인으로 ready 가 된다
    await accounts._on_login_done(login.LoginState(account_id="primary", state="done"))
    assert (await _account()).state == "ready" and (await _account()).reauth_notified_at is None
    assert (await _state()).state == "ready"
    assert (await client.get("/api/notebooklm/notebooks", headers=auth)).status_code == 200


async def test_concurrent_session_expiry_sends_one_notification(fake, monkeypatch):
    import ttakkari.push.service as push_service

    original_row = accounts._row
    barrier = asyncio.Barrier(2)
    calls = 0

    async def synchronized_row(db, account_id):
        row = await original_row(db, account_id)
        await barrier.wait()
        return row

    async def fake_notify(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {"sent": 0, "removed": 0, "failed": 0}

    monkeypatch.setattr(accounts, "_row", synchronized_row)
    monkeypatch.setattr(push_service, "notify", fake_notify)
    await asyncio.gather(accounts.mark_reauth("primary", "expired", feature="notebooklm"),
                         accounts.mark_reauth("primary", "expired", feature="notebooklm"))
    assert calls == 1


# ------------------------------------------------------------------ 재시도 정책

async def test_reads_retry_with_backoff_up_to_three_times(client, auth, fake):
    fake.plan["list_notebooks"] = [errors.Transient("x"), errors.Timeout("y"), errors.Upstream("z")]
    r = await client.get("/api/notebooklm/notebooks", headers=auth)
    assert r.status_code == 200 and fake.count("list_notebooks") == 4
    assert metrics.snapshot()["counters"]["retries"] == 3

    fake.calls.clear()
    fake.plan["list_notebooks"] = [errors.Transient("1")] * 5
    r = await client.get("/api/notebooklm/notebooks", headers=auth)
    assert r.status_code == 503 and r.json()["code"] == "transient"
    assert fake.count("list_notebooks") == 4  # 처음 1회 + 재시도 3회가 상한


async def test_non_retryable_read_errors_are_not_retried(client, auth, fake):
    fake.plan["get_notebook"] = [errors.NotFound("none")]
    r = await client.get("/api/notebooklm/notebooks/zzz", headers=auth)
    assert r.status_code == 404 and fake.count("get_notebook") == 1
    fake.plan["get_notebook"] = [errors.UpstreamRejected("no")]
    assert (await client.get("/api/notebooklm/notebooks/zzz", headers=auth)).status_code == 502
    assert fake.count("get_notebook") == 2


async def test_writes_are_never_retried_and_report_unknown_outcome(client, auth, fake):
    fake.plan["create_notebook"] = [errors.Transient("conn", outcome_unknown=True)]
    r = await client.post("/api/notebooklm/notebooks", headers=auth, json={"title": "새 노트북"})
    assert r.status_code == 503 and r.json()["outcome_unknown"] is True
    assert fake.count("create_notebook") == 1
    fake.plan["rename_notebook"] = [errors.Timeout("t", outcome_unknown=True)]
    r = await client.patch("/api/notebooklm/notebooks/nb1", headers=auth, json={"title": "이름"})
    assert r.status_code == 504 and fake.count("rename_notebook") == 1


async def test_query_is_treated_as_write_and_not_retried(client, auth, fake):
    fake.plan["ask"] = [errors.Transient("conn", outcome_unknown=True)]
    r = await client.post("/api/notebooklm/notebooks/nb1/query", headers=auth, json={"question": "무엇?"})
    assert r.status_code == 503 and fake.count("ask") == 1


# ------------------------------------------------------------------ 429·쿨다운·보호 신호

async def test_rate_limit_starts_cooldown_and_stops_requests(client, auth, fake):
    fake.plan["list_notebooks"] = [errors.RateLimited("limit", retry_after=7)]
    r = await client.get("/api/notebooklm/notebooks", headers=auth)
    assert r.status_code == 429 and r.headers["Retry-After"] == "7" and r.json()["retry_after"] == 7
    assert fake.count("list_notebooks") == 1  # 429 는 읽기라도 즉시 재시도하지 않는다
    row = await _state()
    assert row.state == "cooldown" and row.cooldown_until > datetime.now(UTC)

    blocked = await client.get("/api/notebooklm/notebooks", headers=auth)
    assert blocked.status_code == 429 and blocked.json()["code"] == "cooldown" and int(blocked.headers["Retry-After"]) <= 7
    assert fake.count("list_notebooks") == 1  # 쿨다운 중에는 백엔드에 닿지 않는다
    status = (await client.get("/api/notebooklm/auth/status", headers=auth)).json()
    assert status["state"] == "cooldown" and status["retry_after"] <= 7 and status["cooldown_until"]

    async with sessionmaker()() as db:  # 쿨다운이 끝나면 다시 동작한다
        row = await db.get(NotebookLMAccount, "primary")
        row.cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert (await client.get("/api/notebooklm/notebooks", headers=auth)).status_code == 200


async def test_waiting_request_rechecks_cooldown_before_sending(client, auth, fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "notebooklm_concurrency", 1)
    service._sems.clear()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def rate_limited():
        first_entered.set()
        await release_first.wait()
        raise errors.RateLimited("limit", retry_after=60)

    fake.plan["list_notebooks"] = [rate_limited]
    first = asyncio.create_task(service.list_notebooks())
    await first_entered.wait()
    waiting = asyncio.create_task(service.list_notebooks())
    await asyncio.sleep(0.05)
    assert not waiting.done()

    release_first.set()
    with pytest.raises(errors.RateLimited):
        await first
    with pytest.raises(errors.Cooldown):
        await waiting
    assert fake.count("list_notebooks") == 1


@pytest.mark.parametrize(
    ("signal", "blocked_error"),
    [
        (errors.RateLimited("limit", retry_after=60), errors.Cooldown),
        (errors.ProtectionSignal("protection"), errors.ProtectionSignal),
    ],
)
async def test_late_success_does_not_clear_account_block(client, auth, fake, monkeypatch, signal, blocked_error):
    monkeypatch.setattr(get_settings(), "notebooklm_concurrency", 2)
    service._sems.clear()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def late_success():
        first_entered.set()
        await release_first.wait()

    async def block_account():
        raise signal

    fake.plan["list_notebooks"] = [late_success, block_account]
    first = asyncio.create_task(service.list_notebooks())
    await first_entered.wait()
    second = asyncio.create_task(service.list_notebooks())
    with pytest.raises(type(signal)):
        await second

    release_first.set()
    await first
    with pytest.raises(blocked_error):
        await service.list_notebooks()
    assert fake.count("list_notebooks") == 2


async def test_rate_limit_without_retry_after_uses_default_cooldown(client, auth, fake):
    fake.plan["ask"] = [errors.RateLimited("limit")]
    r = await client.post("/api/notebooklm/notebooks/nb1/query", headers=auth, json={"question": "q"})
    assert r.status_code == 429 and r.json()["retry_after"] == get_settings().notebooklm_default_cooldown_seconds


async def test_protection_signal_blocks_until_human_resumes(client, auth, fake):
    fake.plan["list_notebooks"] = [errors.ProtectionSignal("captcha")]
    r = await client.get("/api/notebooklm/notebooks", headers=auth)
    assert r.status_code == 423 and r.json()["code"] == "protection_signal"
    assert fake.count("list_notebooks") == 1
    for _ in range(2):
        assert (await client.get("/api/notebooklm/notebooks", headers=auth)).status_code == 423
    assert fake.count("list_notebooks") == 1  # 자동 재시도 없음
    resumed = await client.post("/api/notebooklm/auth/resume", headers=auth)
    assert resumed.status_code == 200 and resumed.json()["state"] != "blocked"
    assert (await client.get("/api/notebooklm/notebooks", headers=auth)).status_code == 200


async def test_concurrency_limit_per_account(client, auth, fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "notebooklm_concurrency", 2)
    monkeypatch.setattr(get_settings(), "notebooklm_request_timeout_seconds", 5)
    service._sems.clear()
    active, peak = 0, 0

    async def slow():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1

    fake.plan["list_notebooks"] = [slow] * 6
    results = await asyncio.gather(*[client.get("/api/notebooklm/notebooks", headers=auth) for _ in range(6)])
    assert all(r.status_code == 200 for r in results) and peak == 2


# ------------------------------------------------------------------ 소스: 중복 방지·job

async def test_duplicate_url_registered_once(client, auth, fake):
    a = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth,
                          json={"url": "https://Example.com/a/?utm_source=x"})
    assert a.status_code == 202 and a.json()["duplicate"] is False
    job = await _wait_job(client, auth, a.json()["job"]["id"])
    assert job["status"] == "succeeded" and job["result"]["ready"] is True
    assert fake.count("add_url") == 1

    b = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/a"})
    assert b.status_code == 200 and b.json()["duplicate"] is True and b.json()["job"] is None
    assert b.json()["source"]["id"] == "src1" and fake.count("add_url") == 1

    other = await client.post("/api/notebooklm/notebooks/nb2/sources/url", headers=auth, json={"url": "https://example.com/a"})
    assert other.status_code == 202  # 다른 노트북은 별개
    await _wait_job(client, auth, other.json()["job"]["id"])


async def test_concurrent_identical_adds_send_one_request(client, auth, fake):
    fake.gate = asyncio.Event()
    body = {"url": "https://example.com/race"}
    first = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json=body)
    assert first.status_code == 202
    await asyncio.wait_for(fake.entered.wait(), 3)
    second = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json=body)
    assert second.status_code == 409 and second.json()["code"] == "duplicate"
    fake.gate.set()
    await _wait_job(client, auth, first.json()["job"]["id"])
    assert fake.count("add_url") == 1


async def test_text_and_validation(client, auth, fake):
    r = await client.post("/api/notebooklm/notebooks/nb1/sources/text", headers=auth, json={"title": "메모", "text": "본문 내용"})
    assert r.status_code == 202
    await _wait_job(client, auth, r.json()["job"]["id"])
    dup = await client.post("/api/notebooklm/notebooks/nb1/sources/text", headers=auth, json={"title": "다른 제목", "text": "본문 내용\r\n"})
    assert dup.status_code == 200 and dup.json()["duplicate"] is True
    bad = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "http://localhost/x"})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid"
    assert fake.count("add_text") == 1 and fake.count("add_url") == 0


async def test_failed_definitive_write_releases_reservation(client, auth, fake):
    fake.plan["add_url"] = [errors.UpstreamRejected("거절")]
    a = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/x"})
    job = await _wait_job(client, auth, a.json()["job"]["id"])
    assert job["status"] == "failed" and job["error_code"] == "upstream_rejected"
    b = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/x"})
    assert b.status_code == 202  # 확실히 실패했으므로 다시 등록할 수 있다
    assert (await _wait_job(client, auth, b.json()["job"]["id"]))["status"] == "succeeded"


async def test_unknown_write_outcome_blocks_blind_resend_then_reconciles(client, auth, fake):
    fake.plan["add_url"] = [errors.Timeout("timeout", outcome_unknown=True)]
    a = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/u"})
    job = await _wait_job(client, auth, a.json()["job"]["id"])
    assert job["status"] == "unknown" and job["result"]["outcome_unknown"] is True
    assert fake.count("add_url") == 1  # 자동 재전송 없음

    # 원격에 실제로는 등록돼 있었다면 목록 대조로 확정한다(다시 보내지 않음)
    fake.sources["nb1"].append(SourceInfo(id="remote9", title="u", kind="web_page", url="https://example.com/u/"))
    again = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/u"})
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["source"]["id"] == "remote9"
    assert fake.count("add_url") == 1

    # 원격에 없으면 사람이 확인 후 force 로만 다시 등록한다
    fake.plan["add_url"] = [errors.Transient("conn", outcome_unknown=True)]
    b = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/v"})
    assert (await _wait_job(client, auth, b.json()["job"]["id"]))["status"] == "unknown"
    blocked = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/v"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "duplicate"
    forced = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth,
                               json={"url": "https://example.com/v", "force": True})
    assert forced.status_code == 202
    assert (await _wait_job(client, auth, forced.json()["job"]["id"]))["status"] == "succeeded"


async def test_source_processing_timeout_still_succeeds_with_note(client, auth, fake):
    fake.plan["wait_source_ready"] = [errors.Timeout("slow")] * 5
    a = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/slow"})
    job = await _wait_job(client, auth, a.json()["job"]["id"])
    assert job["status"] == "succeeded" and job["result"]["ready"] is False and "확인" in job["result"]["note"]
    assert fake.count("wait_source_ready") == 1  # 처리 대기는 이미 긴 대기라 재시도로 늘리지 않는다
    async with sessionmaker()() as db:
        row = await db.scalar(select(NotebookLMSource))
    assert row.status == "added" and row.remote_source_id == "src1"


async def test_delete_source_and_notebook_clear_ledger(client, auth, fake):
    a = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/d"})
    await _wait_job(client, auth, a.json()["job"]["id"])
    assert (await client.delete("/api/notebooklm/notebooks/nb1/sources/src1", headers=auth)).status_code == 204
    again = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/d"})
    assert again.status_code == 202  # 삭제 후에는 다시 등록할 수 있다
    await _wait_job(client, auth, again.json()["job"]["id"])
    assert (await client.delete("/api/notebooklm/notebooks/nb1", headers=auth)).status_code == 204
    async with sessionmaker()() as db:
        assert list(await db.scalars(select(NotebookLMSource))) == []


# ------------------------------------------------------------------ job 상태 전이·취소·복구

async def test_job_cancel_during_write_is_unknown_outcome(client, auth, fake):
    fake.gate = asyncio.Event()
    a = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/c"})
    jid = a.json()["job"]["id"]
    await asyncio.wait_for(fake.entered.wait(), 3)
    running = await _wait_job(client, auth, jid, until=("running",))
    assert running["status"] == "running" and running["started_at"]
    c = await client.post(f"/api/notebooklm/jobs/{jid}/cancel", headers=auth)
    assert c.status_code == 200 and c.json()["status"] == "cancelled" and c.json()["result"]["outcome_unknown"] is True
    async with sessionmaker()() as db:
        assert (await db.scalar(select(NotebookLMSource))).status == "unknown"
    assert (await client.post(f"/api/notebooklm/jobs/{jid}/cancel", headers=auth)).status_code == 409  # 끝난 작업


async def test_job_cancel_during_wait_keeps_added_source(client, auth, fake):
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    fake.plan["wait_source_ready"] = [hold]
    a = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/w"})
    jid = a.json()["job"]["id"]
    for _ in range(100):
        if fake.count("wait_source_ready"):
            break
        await asyncio.sleep(0.02)
    c = await client.post(f"/api/notebooklm/jobs/{jid}/cancel", headers=auth)
    assert c.json()["status"] == "cancelled" and c.json()["result"]["source"]["id"] == "src1"
    async with sessionmaker()() as db:
        assert (await db.scalar(select(NotebookLMSource))).status == "added"


async def test_restart_recovery_marks_running_unknown_and_queued_failed(client, auth, fake):
    old = datetime.now(UTC) - timedelta(hours=1)
    running, queued, fresh = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sessionmaker()() as db:
        for jid, status, hb, key in ((running, "running", old, "url:r"), (queued, "queued", old, "url:q"),
                                     (fresh, "running", datetime.now(UTC), "url:f")):
            db.add(NotebookLMJob(id=jid, account_id="primary", kind="source_add_url", notebook_id="nb1", status=status,
                                 params={}, heartbeat_at=hb))
            db.add(NotebookLMSource(account_id="primary", notebook_id="nb1", dedupe_key=key, kind="url", status="reserved", job_id=jid))
        await db.commit()
    n = await service.recover_jobs(stale_seconds=120)  # 배포 세대 겹침: 하트비트가 신선한 job 은 건드리지 않는다
    assert n == 2
    got = {j: (await client.get(f"/api/notebooklm/jobs/{j}", headers=auth)).json() for j in (running, queued, fresh)}
    assert got[running]["status"] == "unknown" and got[running]["error_code"] == "interrupted"
    assert got[queued]["status"] == "failed" and got[queued]["error_code"] == "shutdown"
    assert got[fresh]["status"] == "running"
    async with sessionmaker()() as db:
        keys = {r.dedupe_key: r.status for r in await db.scalars(select(NotebookLMSource))}
    assert keys == {"url:r": "unknown", "url:f": "reserved"}  # 시작 전 중단은 예약을 풀고, 전송 중이던 건 결과 불명
    assert await service.recover_jobs() == 1  # 기동 직후(단일 프로세스)에는 상태만 보고 모두 정리
    assert (await client.get(f"/api/notebooklm/jobs/{fresh}", headers=auth)).json()["status"] == "unknown"


async def test_job_lookup_errors(client, auth, fake):
    assert (await client.get(f"/api/notebooklm/jobs/{uuid.uuid4()}", headers=auth)).status_code == 404
    assert (await client.get("/api/notebooklm/jobs/not-a-uuid", headers=auth)).status_code == 404
    assert (await client.get("/api/notebooklm/jobs", headers=auth)).json() == []


# ------------------------------------------------------------------ 질의·원장 내보내기

async def test_query_returns_separated_structure_and_ledger_export(client, auth, fake):
    fake.sources["nb1"] = [SourceInfo(id="s1", title="문서 제목", kind="web_page", url="https://example.com/doc")]
    r = await client.post("/api/notebooklm/notebooks/nb1/query", headers=auth,
                          json={"question": "핵심은?", "export_for_ledger": True})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "분석 [1]" and set(body) >= {"answer", "citations", "sources", "ledger_export"}
    c = body["citations"][0]
    assert c["source_id"] == "s1" and c["quoted_text"] == "원문 발췌"
    assert c["source_span"] == {"start": 1, "end": 4} and c["answer_span"] == {"start": 3, "end": 6}
    assert body["sources"][0]["title"] == "문서 제목" and body["sources"][0]["url"] == "https://example.com/doc"
    led = body["ledger_export"]
    assert led["assertions"] == [] and led["analysis"]["promoted"] is False
    assert led["source_units"][0]["title"] == "문서 제목" and led["source_units"][0]["excerpts"][0]["text"] == "원문 발췌"
    assert "분석 [1]" not in json.dumps(led["source_units"], ensure_ascii=False)
    plain = await client.post("/api/notebooklm/notebooks/nb1/query", headers=auth, json={"question": "q"})
    assert "ledger_export" not in plain.json()


async def test_query_validation(client, auth, fake):
    assert (await client.post("/api/notebooklm/notebooks/nb1/query", headers=auth, json={"question": ""})).status_code == 422
    assert fake.count("ask") == 0


# ------------------------------------------------------------------ 비동기 질의(연결 끊김과 무관한 job)

async def test_async_query_job_completes_with_separated_result(client, auth, fake):
    fake.sources["nb1"] = [SourceInfo(id="s1", title="문서 제목", kind="web_page", url="https://example.com/doc")]
    r = await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth,
                          json={"question": "핵심은?", "export_for_ledger": True})
    assert r.status_code == 202
    started = r.json()["job"]
    assert started["kind"] == "query" and started["status"] in ("queued", "running")
    assert started["params"]["question_preview"] == "핵심은?"
    job = await _wait_job(client, auth, started["id"])
    assert job["status"] == "succeeded" and job["finished_at"]
    q = job["result"]["query"]
    assert q["answer"] == "분석 [1]" and q["conversation_id"] == "cv"
    assert q["citations"][0]["quoted_text"] == "원문 발췌"
    assert q["sources"][0]["title"] == "문서 제목" and q["sources"][0]["url"] == "https://example.com/doc"
    assert job["result"]["ledger_export"]["assertions"] == []
    plain = await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "q2"})
    done = await _wait_job(client, auth, plain.json()["job"]["id"])
    assert "ledger_export" not in done["result"]
    assert fake.count("ask") == 2


async def test_async_query_runs_detached_from_request(client, auth, fake):
    """시작 요청이 끝나도(연결이 끊겨도) 작업은 서버에서 이어진다. ask 를 막아 두면 202 가 먼저 돌아온다."""
    gate = asyncio.Event()

    async def hold():
        await gate.wait()

    fake.plan["ask"] = [hold]
    r = await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "긴 분석"})
    assert r.status_code == 202  # 작업이 끝나기를 기다리지 않는다
    jid = r.json()["job"]["id"]
    running = await _wait_job(client, auth, jid, until=("running",))
    assert running["status"] == "running"
    gate.set()  # 시작 요청은 이미 끝났지만 작업은 이어져 끝까지 간다
    job = await _wait_job(client, auth, jid)
    assert job["status"] == "succeeded" and job["result"]["query"]["answer"] == "분석 [1]"


async def test_async_query_cancel_marks_cancelled_without_ledger(client, auth, fake):
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def hold():
        entered.set()
        await gate.wait()

    fake.plan["ask"] = [hold]
    jid = (await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "q"})).json()["job"]["id"]
    await _wait_job(client, auth, jid, until=("running",))
    await asyncio.wait_for(entered.wait(), 3)  # running 상태 저장만으로 실제 ask 전송 시작을 보장하지 않는다
    c = await client.post(f"/api/notebooklm/jobs/{jid}/cancel", headers=auth)
    assert c.status_code == 200
    assert c.json()["status"] == "cancelled" and c.json()["result"]["outcome_unknown"] is True
    async with sessionmaker()() as db:  # 질의 job 은 소스 원장을 만들지 않는다
        assert list(await db.scalars(select(NotebookLMSource))) == []
    assert (await client.post(f"/api/notebooklm/jobs/{jid}/cancel", headers=auth)).status_code == 409  # 끝난 작업


async def test_async_query_failure_is_failed_and_unknown_outcome_is_unknown(client, auth, fake):
    fake.plan["ask"] = [errors.Upstream("boom")]
    bad = await _wait_job(client, auth, (await client.post(
        "/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "q"})).json()["job"]["id"])
    assert bad["status"] == "failed" and bad["error_code"] == "upstream_error"
    fake.plan["ask"] = [errors.Transient("conn", outcome_unknown=True)]
    unknown = await _wait_job(client, auth, (await client.post(
        "/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "q"})).json()["job"]["id"])
    assert unknown["status"] == "unknown" and unknown["result"]["outcome_unknown"] is True


async def test_async_query_validation_and_immediate_errors_make_no_job(client, auth, fake):
    assert (await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "  "})).status_code == 422
    fake.plan["ask"] = [errors.RateLimited("limit", retry_after=30)]
    r = await client.post("/api/notebooklm/notebooks/nb1/query", headers=auth, json={"question": "q"})
    assert r.status_code == 429  # 먼저 쿨다운을 만든다
    blocked = await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "q"})
    assert blocked.status_code == 429 and blocked.json()["code"] == "cooldown"
    assert (await client.get("/api/notebooklm/jobs", headers=auth)).json() == []  # 막힌 상태면 job 을 만들지 않는다


async def test_restart_recovery_marks_query_jobs_with_query_message(client, auth, fake):
    old = datetime.now(UTC) - timedelta(hours=1)
    running, queued = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(NotebookLMJob(id=running, account_id="primary", kind="query", notebook_id="nb1", status="running",
                             params={"question_preview": "q"}, heartbeat_at=old))
        db.add(NotebookLMJob(id=queued, account_id="primary", kind="query", notebook_id="nb1", status="queued",
                             params={"question_preview": "q"}, heartbeat_at=old))
        await db.commit()
    assert await service.recover_jobs() == 2
    got = {j: (await client.get(f"/api/notebooklm/jobs/{j}", headers=auth)).json() for j in (running, queued)}
    assert got[running]["status"] == "unknown" and got[running]["error_code"] == "interrupted"
    assert "다시 질문" in got[running]["error"] and "소스 목록" not in got[running]["error"]
    assert got[queued]["status"] == "failed" and got[queued]["error_code"] == "shutdown"
    assert "다시 질문" in got[queued]["error"]


# ------------------------------------------------------------------ 비밀 마스킹(응답·로그·예외)

async def test_errors_and_logs_never_contain_cookie_values(client, auth, fake, caplog):
    caplog.set_level("DEBUG")
    redact.register_secrets([SID, TS])
    leaky = errors.Upstream(f"upstream said Cookie: SID={SID}; __Secure-1PSIDTS={TS} csrf_token=abcdefghijkl")
    fake.plan["list_notebooks"] = [leaky] * 5
    r = await client.get("/api/notebooklm/notebooks", headers=auth)
    assert r.status_code == 502
    status = await client.get("/api/notebooklm/auth/status", headers=auth)
    row = await _state()
    blobs = [r.text, status.text, caplog.text, json.dumps(metrics.snapshot()), row.message or "", row.last_error_code or ""]
    for blob in blobs:
        assert SID not in blob and TS not in blob and "abcdefghijkl" not in blob


async def test_job_error_text_is_masked(client, auth, fake):
    redact.register_secrets([SID])
    fake.plan["add_url"] = [errors.UpstreamRejected(f"bad SID={SID}")]
    a = await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/m"})
    job = await _wait_job(client, auth, a.json()["job"]["id"])
    assert SID not in json.dumps(job)


# ------------------------------------------------------------------ Protocol 교체 가능성

class AltBackend(FakeBackend):
    """다른 구현(예: 허용되는 브라우저 구현). Protocol 만 맞추면 서비스·REST 는 그대로 동작한다."""

    name = "alt-browser"

    async def list_notebooks(self):
        return [NotebookSummary(id="alt1", title="브라우저 구현의 노트북")]

    def capabilities(self):
        return [Capability(name="notebooks", available=True), Capability(name="sources.file", available=False, reason="미지원")]


async def test_backend_is_swappable_without_touching_upper_layers(client, auth, fake):
    alt = AltBackend()
    assert isinstance(alt, NotebookLMBackend)
    service.set_backend_factory(lambda _a: alt)
    r = await client.get("/api/notebooklm/notebooks", headers=auth)
    assert r.json() == [{"id": "alt1", "title": "브라우저 구현의 노트북", "source_count": 0, "created_at": None,
                         "updated_at": None, "is_owner": True}]
    status = (await client.get("/api/notebooklm/auth/status", headers=auth)).json()
    assert status["backend"] == "alt-browser" and {c["name"]: c["available"] for c in status["capabilities"]}["sources.file"] is False
    assert isinstance(NotebookLMService(lambda _a: alt)._backend("primary"), AltBackend)


# ------------------------------------------------------------------ 업로드

async def test_upload_creates_job_and_cleans_temp(client, auth, fake):
    r = await client.post("/api/notebooklm/notebooks/nb1/sources/upload", headers=auth,
                          files={"file": ("보고서.pdf", b"%PDF-1.4 fake", "application/pdf")}, data={"title": "보고서"})
    assert r.status_code == 202
    job = await _wait_job(client, auth, r.json()["job"]["id"])
    assert job["status"] == "succeeded" and fake.count("add_file") == 1
    await asyncio.sleep(0.05)
    assert not any(session.uploads_dir().glob("*/*"))  # 임시 업로드 폴더 정리
    dup = await client.post("/api/notebooklm/notebooks/nb1/sources/upload", headers=auth,
                            files={"file": ("copy.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert dup.status_code == 200 and dup.json()["duplicate"] is True and fake.count("add_file") == 1
    big = get_settings().notebooklm_upload_max_bytes
    assert big > 1024


async def test_upload_size_limit(client, auth, fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "notebooklm_upload_max_bytes", 1024)
    r = await client.post("/api/notebooklm/notebooks/nb1/sources/upload", headers=auth,
                          files={"file": ("big.pdf", b"x" * 2048, "application/pdf")})
    assert r.status_code == 413
    assert not any(session.uploads_dir().glob("*/*"))
    empty = await client.post("/api/notebooklm/notebooks/nb1/sources/upload", headers=auth,
                              files={"file": ("e.pdf", b"", "application/pdf")})
    assert empty.status_code == 422


# ------------------------------------------------------------------ 접근 범위·MCP

async def test_requires_login_token(client, fake):
    assert (await client.get("/api/notebooklm/notebooks")).status_code == 401
    assert (await client.get("/api/agent/notebooklm/notebooks")).status_code in (401, 403)


@pytest.fixture
async def sessions(make_dir):
    root = make_dir()
    async with sessionmaker()() as db:
        ws = Workspace(id=uuid.uuid4(), name=root.name, root_path=str(root))
        db.add(ws)
        await db.flush()
        mk = lambda title, pool: ChatSession(id=uuid.uuid4(), workspace_id=ws.id, title=title, engine="codex", pool=pool,
                                             created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        personal, work = mk("p", "personal"), mk("w", "work")
        db.add_all([personal, work])
        await db.commit()
    return personal, work


async def test_agent_routes_are_personal_only_and_hide_login_actions(client, fake, sessions):
    personal, work = sessions
    p = {"Authorization": f"Bearer {issue_token(personal.id)}"}
    w = {"Authorization": f"Bearer {issue_token(work.id)}"}
    assert (await client.get("/api/agent/notebooklm/auth/status", headers=w)).status_code == 403
    assert (await client.get("/api/agent/notebooklm/notebooks", headers=p)).status_code == 200
    # 로그인 창·세션 삭제·재개·업로드는 에이전트에 열지 않는다
    assert (await client.post("/api/agent/notebooklm/auth/login-window", headers=p)).status_code in (404, 405)
    assert (await client.post("/api/agent/notebooklm/auth/login-window/complete", headers=p)).status_code in (404, 405)
    assert (await client.delete("/api/agent/notebooklm/auth/session", headers=p)).status_code in (404, 405)
    assert (await client.post("/api/agent/notebooklm/auth/resume", headers=p)).status_code in (404, 405)
    assert (await client.post("/api/agent/notebooklm/notebooks/nb1/sources/upload", headers=p,
                              files={"file": ("a.pdf", b"x", "application/pdf")})).status_code in (404, 405)


async def test_mcp_tools_call_agent_api_end_to_end(client, fake, sessions, monkeypatch):
    from ttakkari.orchestrator import mcp_server

    personal, _ = sessions
    headers = {"Authorization": f"Bearer {issue_token(personal.id)}"}

    async def agent_call(method, path, body=None, **params):
        r = await client.request(method, f"/api/agent{path}", headers=headers, json=body,
                                 params={k: v for k, v in params.items() if v is not None} or None)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.is_error:
            return json.dumps({"error": r.status_code, **data}, ensure_ascii=False)
        return json.dumps(data if r.status_code != 204 else {"ok": True}, ensure_ascii=False)

    monkeypatch.setattr(mcp_server, "_agent", agent_call)
    status = json.loads(await mcp_server.notebooklm_status())
    assert status["state"] == "session_saved" and SID not in json.dumps(status)
    listed = json.loads(await mcp_server.notebooklm_list())
    assert listed[0]["id"] == "nb1"
    created = json.loads(await mcp_server.notebooklm_create("MCP 노트북"))
    assert created["title"] == "MCP 노트북"
    assert json.loads(await mcp_server.notebooklm_get("nb1"))["id"] == "nb1"

    added = json.loads(await mcp_server.notebooklm_add_source("nb1", "url", url="https://example.com/mcp"))
    jid = added["job"]["id"]
    job = {}
    for _ in range(100):
        job = json.loads(await mcp_server.notebooklm_job(jid))
        if job["status"] in ("succeeded", "failed", "unknown"):
            break
        await asyncio.sleep(0.02)
    assert job["status"] == "succeeded"
    dup = json.loads(await mcp_server.notebooklm_add_source("nb1", "youtube", url="https://example.com/mcp"))
    assert dup["duplicate"] is True

    answer = json.loads(await mcp_server.notebooklm_query("nb1", "무엇?", export_for_ledger=True))
    assert answer["answer"] == "분석 [1]" and answer["citations"][0]["quoted_text"] == "원문 발췌"
    assert answer["ledger_export"]["assertions"] == []

    refused = json.loads(await mcp_server.notebooklm_delete("nb1"))  # confirm 없이는 호출하지 않는다
    assert refused["code"] == "confirm_required" and fake.count("delete_notebook") == 0
    assert fake.count("delete_source") == 0
    await mcp_server.notebooklm_delete_source("nb1", "src1", confirm=True)
    assert fake.count("delete_source") == 1

    fake.plan["ask"] = [errors.RateLimited("limit", retry_after=30)]
    limited = json.loads(await mcp_server.notebooklm_query("nb1", "또?"))
    assert limited["error"] == 429 and limited["retry_after"] == 30 and limited["code"] == "rate_limited"


def test_mcp_tool_metadata_matches_assistant_catalog_and_policy():
    from ttakkari.api.assistant import TOOLS
    from ttakkari.orchestrator.tool_policy import AUTO_APPROVED_TOOLS, READ_ONLY_TOOLS

    names = {t["name"] for t in TOOLS if t["name"].startswith("notebooklm_")}
    expected = {"notebooklm_status", "notebooklm_list", "notebooklm_get", "notebooklm_create", "notebooklm_rename",
                "notebooklm_delete", "notebooklm_add_source", "notebooklm_delete_source", "notebooklm_query",
                "notebooklm_job", "notebooklm_job_cancel"}
    assert names == expected and expected <= AUTO_APPROVED_TOOLS
    assert {n for n in READ_ONLY_TOOLS if n.startswith("notebooklm_")} == {
        "notebooklm_status", "notebooklm_list", "notebooklm_get", "notebooklm_job"}
    assert all(t["who"] == "개인 실행" for t in TOOLS if t["name"].startswith("notebooklm_"))


async def test_account_id_is_part_of_every_key(client, auth, fake):
    await client.post("/api/notebooklm/notebooks/nb1/sources/url", headers=auth, json={"url": "https://example.com/acct"})
    async with sessionmaker()() as db:
        assert (await db.scalar(select(NotebookLMSource))).account_id == "primary"
        assert (await db.scalar(select(NotebookLMJob))).account_id == "primary"
        assert (await db.get(NotebookLMAccount, "primary")) is not None
    with pytest.raises(errors.Invalid):
        await service.status("../etc")


@pytest.mark.parametrize("user_cancel", [True, False])
async def test_query_answer_survives_cancellation_during_enrichment(client, auth, fake, user_cancel):
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def hold_sources():
        entered.set()
        await gate.wait()

    fake.plan["list_sources"] = [hold_sources]
    jid = (await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth,
                             json={"question": "q", "export_for_ledger": True})).json()["job"]["id"]
    await asyncio.wait_for(entered.wait(), 3)
    if user_cancel:
        await client.post(f"/api/notebooklm/jobs/{jid}/cancel", headers=auth)
    else:
        service._tasks[uuid.UUID(jid)].cancel()  # 서버 drain/shutdown과 같은 task 취소
    job = await _wait_job(client, auth, jid)
    assert job["status"] == "succeeded"
    assert job["result"]["query"]["answer"] == "분석 [1]"
    assert job["result"]["query"]["citations"][0]["quoted_text"] == "원문 발췌"
    assert job["result"]["ledger_export"]["assertions"] == []


async def test_query_job_detail_enforces_google_account_scope(client, auth, fake):
    jid = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(NotebookLMJob(id=jid, account_id="another", kind="query", notebook_id="nb1",
                             status="succeeded", params={}, result={"query": {"answer": "private"}}))
        await db.commit()
    response = await client.get(f"/api/notebooklm/jobs/{jid}", headers=auth)
    assert response.status_code == 404
    assert "private" not in response.text
    assert not any(row["id"] == str(jid) for row in (await client.get("/api/notebooklm/jobs", headers=auth)).json())
