"""Google 계정 공통 모듈 검사(ADR 0051). 실제 DB(전용 테스트 DB)와 가짜 백엔드·가짜 기능을 쓴다. Google 요청·실제 로그인은 없다.

확인하는 것: 계정 격리, 세션 공유, 기능별 쿨다운 독립, 재인증 공유와 알림 1회, 옛 NotebookLM 데이터 이전, 마스킹, REST·MCP 범위.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
from datetime import UTC, datetime

import asyncpg
import pytest
from authz03_canary import Authz03Env, ensure_authz03
from sqlalchemy import select
from test_meeting_lineage_migration import migrate
from test_notebooklm_service import SID, TS, FakeBackend, _cookies
from test_retire_clovanote_migration import migration_database  # noqa: F401

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.google_account import errors, login, metrics, redact, registry, session
from ttakkari.google_account.service import service as accounts
from ttakkari.models import GoogleAccount, NotebookLMAccount, Notification
from ttakkari.notebooklm import errors as nlm_errors
from ttakkari.notebooklm.service import service as nlm
from ttakkari.orchestrator.runtime import issue_token

OTHER = "second"


class FakeFeature:
    """NotebookLM 이 아닌 두 번째 기능. 자기 쿨다운 상태를 자기가 들고 있고 계정 공통 모듈은 읽기만 한다."""

    def __init__(self) -> None:
        self.state = "session_saved"  # 접속 확인 전. 확인이 끝난 기능은 ready
        self.message: str | None = None
        self.invalidated: list[str] = []
        self.saved: list[str] = []
        self.removed: list[str] = []

    async def describe(self, account_id: str) -> dict:
        acct, msg = await accounts.effective(account_id)
        if acct in {"disabled", "login_required", "reauth_required"}:
            return {"state": acct, "message": msg}
        return {"state": self.state, "message": self.message}

    async def on_reauth(self, account_id: str) -> None:
        self.invalidated.append(account_id)

    async def on_saved(self, account_id: str) -> None:
        self.saved.append(account_id)

    async def on_removed(self, account_id: str) -> None:
        self.removed.append(account_id)

    def consumer(self) -> registry.Consumer:
        return registry.Consumer(
            id="fakedrive", label="가짜 드라이브", description="테스트용 두 번째 기능", path="/fakedrive",
            login_url="https://drive.google.com/", app_hosts=("drive.google.com",),
            cookie_domains=("youtube.com",), describe=self.describe, on_reauth=self.on_reauth,
            on_session_saved=self.on_saved, on_session_removed=self.on_removed,
        )


@pytest.fixture(autouse=True)
def clean_files():
    def wipe() -> None:
        shutil.rmtree(session.root_dir(), ignore_errors=True)
        shutil.rmtree(get_settings().notebooklm_dir / "accounts", ignore_errors=True)
        legacy_profile = get_settings().home / "notebooklm-chrome"
        if legacy_profile.is_symlink():
            legacy_profile.unlink()
        shutil.rmtree(legacy_profile, ignore_errors=True)
        login.manager = login.LoginManager()
        metrics.reset()
        redact.clear_secrets()

    wipe()
    yield
    wipe()


@pytest.fixture
def feature():
    f = FakeFeature()
    registry.register(f.consumer())
    yield f
    registry.unregister("fakedrive")


@pytest.fixture
def nlm_fake(monkeypatch):
    backend = FakeBackend()
    saved = nlm._factory
    nlm.set_backend_factory(lambda _a: backend)
    nlm._sems.clear()
    nlm._last_verify.clear()
    st = get_settings()
    monkeypatch.setattr(st, "notebooklm_read_retry_base_seconds", 0.0)
    monkeypatch.setattr(st, "notebooklm_read_retry_cap_seconds", 0.0)
    yield backend
    nlm._factory = saved
    nlm._backends.clear()


async def _row(account="primary") -> GoogleAccount | None:
    async with sessionmaker()() as db:
        return await db.get(GoogleAccount, account)


async def _features(account="primary") -> dict[str, dict]:
    return {f["id"]: f for f in (await accounts.status(account))["features"]}


# ------------------------------------------------------------------ 계정 격리

async def test_sessions_are_isolated_per_account():
    session.save_cookies("primary", _cookies())
    assert session.exists("primary") and not session.exists(OTHER)
    other_cookies = [{**c, "value": c["value"] + "-other"} for c in _cookies()]
    session.save_cookies(OTHER, other_cookies)
    assert session.storage_path("primary") != session.storage_path(OTHER)
    assert session.profile_dir("primary") != session.profile_dir(OTHER)
    assert json.loads(session.storage_path(OTHER).read_text())["cookies"][0]["value"].endswith("-other")
    session.delete(OTHER)
    assert session.exists("primary") and not session.exists(OTHER)  # 한 계정 삭제가 다른 계정에 번지지 않는다
    for bad in ("../etc", "A", "a/b", "", "x" * 40):
        with pytest.raises(errors.Invalid):
            session.check_account(bad)


async def test_account_state_rows_and_login_windows_are_per_account(monkeypatch):
    session.save_cookies("primary", _cookies())
    session.save_cookies(OTHER, _cookies())
    await accounts.mark_ok("primary")
    await accounts.mark_reauth(OTHER, "expired")
    assert (await accounts.effective("primary"))[0] == "ready"
    assert (await accounts.effective(OTHER))[0] == "reauth_required"  # 한 계정의 만료가 다른 계정을 막지 않는다

    login.manager._states["primary"] = login.LoginState(account_id="primary", state="waiting")
    assert accounts_status_login("primary")["active"] is True and accounts_status_login(OTHER)["active"] is False


def accounts_status_login(account: str) -> dict:
    return login.manager.state(account).to_dict()


async def test_login_windows_for_different_accounts_do_not_block_each_other(monkeypatch):
    launched: list[str] = []

    async def fake_run(self, st, chrome, target, on_done):
        launched.append(st.account_id)
        st.state = "waiting"

    monkeypatch.setattr(login.LoginManager, "_run", fake_run)
    monkeypatch.setattr(login, "find_chrome", lambda: login.Path("/bin/true"))
    monkeypatch.setattr(login, "profile_chrome_pids", lambda account_id="primary": [])
    await login.manager.start("primary")
    await login.manager.start(OTHER)
    await asyncio.sleep(0.01)
    assert sorted(launched) == ["primary", OTHER]
    with pytest.raises(errors.Busy):
        await login.manager.start("primary")  # 같은 계정의 두 번째 창만 거절


# ------------------------------------------------------------------ 세션 공유

async def test_one_login_session_is_shared_by_every_feature(feature):
    session.save_cookies("primary", _cookies())
    feats = await _features()
    assert set(feats) == {"notebooklm", "fakedrive"}
    assert feats["notebooklm"]["state"] == "session_saved" and feats["fakedrive"]["state"] == "session_saved"
    await accounts.mark_ok("primary")  # 어느 기능이든 접속에 성공하면 계정 세션이 검증된 것이다
    feats = await _features()
    assert feats["notebooklm"]["state"] == "session_saved"  # 기능별 접속 확인 이력은 기능이 따로 갖는다
    assert (await accounts.status())["state"] == "ready"
    # 같은 파일을 두 기능이 읽는다. 기능마다 다시 로그인하지 않는다.
    assert session.cookie_names("primary") == ["SID", "__Secure-1PSIDTS"]


async def test_login_done_notifies_every_feature_and_removal_does_too(feature, nlm_fake):
    session.save_cookies("primary", _cookies())
    await accounts._on_login_done(login.LoginState(account_id="primary", state="done"))
    assert feature.saved == ["primary"]
    row = await _row()
    assert row.last_login_at is not None and row.state in {"session_saved", "ready"}
    assert nlm_fake.count("verify") == 1  # NotebookLM 은 로그인 직후 접속을 한 번 확인한다
    out = await accounts.delete_session("primary")
    assert feature.removed == ["primary"] and out["state"] == "login_required" and not session.exists("primary")


async def test_cookie_scope_is_the_union_of_registered_features(feature):
    youtube = {"name": "LOGIN_INFO", "value": "yt-value-0123456789", "domain": ".youtube.com", "path": "/"}
    other = {"name": "x", "value": "z" * 20, "domain": ".example.com", "path": "/"}
    session.save_cookies("primary", [*_cookies(), youtube, other])
    names = session.cookie_names("primary")
    assert "LOGIN_INFO" in names and "x" not in names  # 등록된 기능이 요구하는 도메인만 담긴다
    assert session.covers("primary", registry.BASE_COOKIE_SUFFIXES + ("youtube.com",))
    registry.unregister("fakedrive")
    session.save_cookies("primary", _cookies())  # 기능이 빠진 뒤 다시 로그인하면 그 도메인은 담기지 않는다
    assert not session.covers("primary", ("youtube.com",))
    feats = await _features()
    assert feats["notebooklm"]["covered"] is True


# ------------------------------------------------------------------ 기능별 쿨다운 독립

async def test_one_feature_cooldown_does_not_block_another_or_the_account(feature, nlm_fake):
    session.save_cookies("primary", _cookies())
    feature.state = "ready"
    await nlm.verify()  # 접속 확인에 성공해 계정 세션이 ready 가 된다
    nlm_fake.plan["list_notebooks"] = [errors.RateLimited("limit", retry_after=60)]
    with pytest.raises(nlm_errors.RateLimited):
        await nlm.list_notebooks()
    feats = await _features()
    assert feats["notebooklm"]["state"] == "cooldown" and feats["notebooklm"]["retry_after"] <= 60
    assert feats["fakedrive"]["state"] == "ready"  # 다른 기능은 막히지 않는다
    assert (await accounts.status())["state"] == "ready"  # 계정 세션은 멀쩡하다(쿨다운은 계정 상태가 아니다)
    # 반대 방향: 두 번째 기능이 쉬어도 NotebookLM 은 쿨다운이 아니다
    async with sessionmaker()() as db:
        row = await db.get(NotebookLMAccount, "primary")
        row.state, row.cooldown_until = "ready", None
        await db.commit()
    feature.state, feature.message = "cooldown", "다른 기능의 429"
    assert (await nlm.status())["state"] == "ready"
    assert nlm_fake.count("list_notebooks") == 1
    await nlm.list_notebooks()
    assert nlm_fake.count("list_notebooks") == 2


async def test_blocked_state_is_feature_scoped_and_resume_is_per_feature(feature, nlm_fake):
    session.save_cookies("primary", _cookies())
    feature.state = "ready"
    nlm_fake.plan["list_notebooks"] = [nlm_errors.ProtectionSignal("captcha")]
    with pytest.raises(nlm_errors.ProtectionSignal):
        await nlm.list_notebooks()
    feats = await _features()
    assert feats["notebooklm"]["state"] == "blocked" and feats["fakedrive"]["state"] == "ready"
    assert (await accounts.effective("primary"))[0] != "blocked"
    with pytest.raises(errors.Unsupported):
        await accounts.resume("primary", "fakedrive")  # resume 을 정의하지 않은 기능
    out = await accounts.resume("primary", "notebooklm")
    assert out["state"] != "blocked"
    with pytest.raises(errors.Invalid):
        await accounts.resume("primary", "nosuch")


# ------------------------------------------------------------------ 재인증 공유와 알림 1회

async def test_reauth_detected_by_one_feature_is_shared_and_notifies_once(feature, nlm_fake):
    session.save_cookies("primary", _cookies())
    await accounts.mark_reauth("primary", "세션이 만료됐습니다.", feature="fakedrive")
    assert feature.invalidated == ["primary"]  # 모든 기능이 만료된 세션을 쥔 클라이언트를 닫는다
    feats = await _features()
    assert feats["notebooklm"]["state"] == "reauth_required" and feats["fakedrive"]["state"] == "reauth_required"
    # NotebookLM 은 Google 에 나가지 않고 막힌다
    with pytest.raises(nlm_errors.AuthRequired):
        await nlm.list_notebooks()
    assert nlm_fake.count("list_notebooks") == 0
    # 두 번째 기능과 NotebookLM 이 또 감지해도 알림은 늘지 않는다
    await accounts.mark_reauth("primary", "again", feature="fakedrive")
    await nlm._on_error("primary", nlm_errors.AuthRequired("expired"))
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(Notification).where(Notification.kind == "push.google_account")))
    assert len(rows) == 1 and "로그인" in rows[0].title and rows[0].url == "/settings#google-accounts"
    assert "가짜 드라이브" in rows[0].body

    # 다시 로그인하면 해제되고 다음 만료에는 새 알림이 간다
    await accounts._on_login_done(login.LoginState(account_id="primary", state="done"))
    assert (await _row()).reauth_notified_at is None and (await accounts.effective("primary"))[0] != "reauth_required"


async def test_slow_success_does_not_clear_a_reauth_recorded_meanwhile():
    session.save_cookies("primary", _cookies())
    await accounts.mark_reauth("primary", "expired")
    await accounts.mark_ok("primary")  # 늦게 도착한 성공
    assert (await _row()).state == "reauth_required"


async def test_failed_login_without_session_returns_to_login_required():
    await accounts._on_login_done(login.LoginState(account_id="primary", state="failed", message="보안 확인 화면"))
    row = await _row()
    assert row.state == "login_required" and "보안" in row.message


# ------------------------------------------------------------------ 옛 NotebookLM 데이터 이전

async def test_legacy_session_file_moves_and_keeps_a_link_for_the_old_generation():
    legacy = session.legacy_storage_path("primary")
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"cookies": [{"name": "SID", "value": SID, "domain": ".google.com"},
                                              {"name": "__Secure-1PSIDTS", "value": TS, "domain": ".google.com"}],
                                  "origins": []}))
    legacy.chmod(0o600)
    assert session.exists("primary")  # 재로그인 없이 이어진다
    new = session.storage_path("primary")
    assert new.is_file() and not new.is_symlink() and legacy.is_symlink() and legacy.resolve() == new.resolve()
    assert stat.S_IMODE(new.stat().st_mode) == 0o600 and session.mode_ok("primary")
    assert session.cookie_names("primary") == ["SID", "__Secure-1PSIDTS"]
    assert json.loads(legacy.read_text())["cookies"][0]["value"] == SID  # 이전 세대도 같은 파일을 읽는다
    assert not session.adopt_legacy_session("primary")  # 멱등
    session.delete("primary")
    assert not new.exists() and not legacy.exists() and not legacy.is_symlink()


async def test_legacy_chrome_profile_moves_unless_chrome_is_using_it():
    legacy = session.legacy_profile_path()
    legacy.mkdir(parents=True)
    (legacy / "Default").mkdir()
    (legacy / "Default" / "Cookies").write_text("chrome-owned")
    assert not session.adopt_legacy_profile("primary", in_use=True) and not legacy.is_symlink()
    assert not session.adopt_legacy_profile(OTHER, in_use=False)  # 옛 프로필은 기본 계정 것이다
    assert session.adopt_legacy_profile("primary", in_use=False)
    new = session.profile_dir("primary")
    assert (new / "Default" / "Cookies").read_text() == "chrome-owned" and legacy.is_symlink()
    assert not session.adopt_legacy_profile("primary", in_use=False)


async def test_login_start_adopts_legacy_profile(monkeypatch):
    legacy = session.legacy_profile_path()
    legacy.mkdir(parents=True)
    (legacy / "marker").write_text("x")

    async def noop(self, st, chrome, target, on_done):
        st.state = "waiting"

    monkeypatch.setattr(login.LoginManager, "_run", noop)
    monkeypatch.setattr(login, "find_chrome", lambda: login.Path("/bin/true"))
    monkeypatch.setattr(login, "profile_chrome_pids", lambda account_id="primary": [])
    await login.manager.start("primary")
    assert (session.profile_dir("primary") / "marker").exists()


MIGRATION_ROWS = [
    # account_id, state, last_ok_at, message, reauth_notified
    ("primary", "reauth_required", True, "만료", True),
    ("a-ready", "ready", True, None, False),
    ("a-cool", "cooldown", True, "쉼", False),
    ("a-cool-new", "cooldown", False, "쉼", False),
    ("a-blocked", "blocked", True, "보호", False),
    ("a-saved", "session_saved", False, "저장됨", False),
    ("a-login", "login_required", False, "처음", False),
]


async def test_migration_copies_notebooklm_account_state_without_touching_the_old_table(migration_database):  # noqa: F811
    database = migration_database
    await asyncio.to_thread(migrate, database, "0048_game_active_build")
    conn = await asyncpg.connect(database=database, host="localhost")
    now = datetime.now(UTC)
    try:
        for account, state, ok, message, notified in MIGRATION_ROWS:
            await conn.execute(
                "INSERT INTO notebooklm_accounts(account_id,state,message,last_ok_at,reauth_notified_at,created_at,updated_at)"
                " VALUES($1,$2,$3,$4,$5,$6,$6)",
                account, state, message, now if ok else None, now if notified else None, now)
        before = await conn.fetch("SELECT * FROM notebooklm_accounts ORDER BY account_id")
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, database, "head")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        rows = {r["account_id"]: r for r in await conn.fetch("SELECT * FROM google_accounts")}
        assert {k: rows[k]["state"] for k in rows} == {
            "primary": "reauth_required", "a-ready": "ready", "a-cool": "ready", "a-cool-new": "session_saved",
            "a-blocked": "ready", "a-saved": "session_saved", "a-login": "login_required"}
        assert rows["primary"]["reauth_notified_at"] is not None and rows["primary"]["message"] == "만료"
        assert rows["a-cool"]["message"] is None  # 쿨다운 문구는 기능 상태라 계정으로 옮기지 않는다
        assert await conn.fetchval("SELECT count(*) FROM google_accounts") == len(MIGRATION_ROWS)
        after = await conn.fetch("SELECT * FROM notebooklm_accounts ORDER BY account_id")
        assert [dict(r) for r in after] == [dict(r) for r in before]  # 이전 세대가 읽는 표는 그대로
        await conn.execute("DELETE FROM google_accounts WHERE account_id = 'a-ready'")
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, database, "0048_game_active_build", "downgrade")  # 새 표만 사라진다
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        assert await conn.fetchval("SELECT to_regclass('google_accounts')") is None
        assert await conn.fetchval("SELECT count(*) FROM notebooklm_accounts") == len(MIGRATION_ROWS)
    finally:
        await conn.close()


async def test_migration_is_idempotent_on_rerun_of_the_copy(migration_database):  # noqa: F811
    database = migration_database
    await asyncio.to_thread(migrate, database, "head")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        cols = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='google_accounts'")}
        assert {"account_id", "state", "reauth_notified_at", "last_login_at", "label"} <= cols
        assert await conn.fetchval("SELECT count(*) FROM google_accounts") == 0
    finally:
        await conn.close()


# ------------------------------------------------------------------ 마스킹

async def test_status_errors_and_logs_never_contain_cookie_values(client, auth, caplog):
    caplog.set_level("DEBUG")
    session.save_cookies("primary", _cookies())
    session.load_secrets("primary")
    await accounts.mark_reauth("primary", f"expired Cookie: SID={SID}; __Secure-1PSIDTS={TS}")
    blobs = [json.dumps(await accounts.status()), (await client.get("/api/google-accounts", headers=auth)).text,
             caplog.text, json.dumps(metrics.snapshot())]
    leaky = errors.Upstream(f"upstream said SID={SID} csrf_token=abcdefghijkl")
    resp = (await client.get("/api/google-accounts/nope", headers=auth))
    blobs.append(resp.text + redact.mask(leaky.message))
    # 저장된 메시지에는 쿠키 값이 들어갈 수 있으므로 마스킹은 출력 경로(응답)에서도 걸려 있어야 한다.
    for blob in blobs[2:]:
        assert SID not in blob and TS not in blob and "abcdefghijkl" not in blob
    assert set(session.cookie_names("primary")) == {"SID", "__Secure-1PSIDTS"}
    assert "profile" not in json.dumps(await accounts.status()).lower().replace("profile_", "")


async def test_browser_launches_are_counted_in_common_metrics(monkeypatch):
    import subprocess

    class P:
        stderr = iter(["DevTools listening on ws://127.0.0.1:5555/devtools/browser/abc\n"])

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: P())
    await login._launch(login.Path("/chrome"), session.profile_dir("primary"), "https://notebooklm.google.com/")
    await login._launch_headless(login.Path("/chrome"), session.profile_dir("primary"), "https://notebooklm.google.com/")
    counters = metrics.snapshot()["counters"]
    assert counters["browser_launches"] == 2 and counters["browser_launches.visible"] == 1


# ------------------------------------------------------------------ 로그인 대상(기능별 앱 화면)

def test_login_target_follows_the_feature_and_completion_requires_that_app(feature):
    nlm_target = registry.login_target("notebooklm")
    drive_target = registry.login_target("fakedrive")
    assert nlm_target.url == "https://notebooklm.google.com/" and drive_target.url == "https://drive.google.com/"
    assert registry.login_target(None).feature == "notebooklm"  # 기본은 첫 번째 등록 기능
    with pytest.raises(errors.Invalid):
        registry.login_target("nosuch")
    good = [{"name": n, "value": "v" * 8, "domain": ".google.com"} for n in session.REQUIRED_COOKIES]
    assert login.login_complete(["https://drive.google.com/"], good, drive_target)
    assert not login.login_complete(["https://notebooklm.google.com/"], good, drive_target)  # 다른 기능의 화면은 완료가 아니다
    assert not login.login_complete(["https://drive.google.com/", "https://accounts.google.com/x"], good, drive_target)


def test_consumer_registration_is_validated():
    base = {"label": "x", "description": "x", "path": "/x", "app_hosts": ("a.google.com",)}
    with pytest.raises(ValueError):
        registry.register(registry.Consumer(id="Bad Id", login_url="https://a.google.com/", **base))
    with pytest.raises(ValueError):
        registry.register(registry.Consumer(id="plain", login_url="http://a.google.com/", **base))
    with pytest.raises(ValueError):
        registry.register(registry.Consumer(id="elsewhere", login_url="https://b.google.com/", **base))
    with pytest.raises(ValueError):
        registry.register(registry.Consumer(id="dots", login_url="https://a.google.com/", cookie_domains=(".x.com",), **base))


async def test_disabled_feature_cannot_open_its_login_window(monkeypatch):
    monkeypatch.setattr(get_settings(), "notebooklm_enabled", False)
    with pytest.raises(errors.Disabled):
        await login.manager.start("primary", feature="notebooklm")


# ------------------------------------------------------------------ REST·MCP

async def test_rest_lists_accounts_and_features_without_secrets(client, auth, feature):
    session.save_cookies("primary", _cookies())
    r = await client.get("/api/google-accounts", headers=auth)
    body = r.json()
    assert r.status_code == 200 and body["accounts"][0]["account_id"] == "primary"
    acct = body["accounts"][0]
    assert {f["id"] for f in acct["features"]} == {"notebooklm", "fakedrive"}
    assert SID not in r.text and TS not in r.text and acct["session"]["stored"] is True
    one = await client.get("/api/google-accounts/primary", headers=auth)
    assert one.json()["state"] == "session_saved"
    bad = await client.get("/api/google-accounts/Bad..Id", headers=auth)
    assert bad.status_code in (404, 422)
    assert (await client.get("/api/google-accounts")).status_code in (401, 403)


async def test_rest_login_lifecycle_delegates_to_the_login_manager(client, auth, monkeypatch, feature):
    calls: list[tuple] = []

    async def fake_start(account_id, on_done=None, feature=None):
        calls.append(("start", account_id, feature))
        return login.LoginState(account_id=account_id, state="waiting", feature=feature)

    async def fake_complete(account_id):
        calls.append(("complete", account_id))
        return login.LoginState(account_id=account_id, state="done")

    async def fake_cancel(account_id):
        calls.append(("cancel", account_id))
        return login.LoginState(account_id=account_id, state="cancelled")

    monkeypatch.setattr(login.manager, "start", fake_start)
    monkeypatch.setattr(login.manager, "complete", fake_complete)
    monkeypatch.setattr(login.manager, "cancel", fake_cancel)
    r = await client.post("/api/google-accounts/primary/login", headers=auth, json={"feature": "fakedrive"})
    assert r.status_code == 202 and r.json()["feature"] == "fakedrive"
    r = await client.post("/api/google-accounts/primary/login", headers=auth)  # 본문 없이도 시작
    assert r.status_code == 202
    assert (await client.post("/api/google-accounts/primary/login/complete", headers=auth)).json()["state"] == "done"
    assert (await client.post("/api/google-accounts/primary/login/cancel", headers=auth)).json()["state"] == "cancelled"
    assert calls == [("start", "primary", "fakedrive"), ("start", "primary", None), ("complete", "primary"),
                     ("cancel", "primary")]

    async def busy(*a, **k):
        raise errors.Busy("이미 열려 있습니다.")

    monkeypatch.setattr(login.manager, "start", busy)
    r = await client.post("/api/google-accounts/primary/login", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "busy"
    r = await client.post("/api/google-accounts/primary/login", headers=auth, json={"feature": "nosuch"})
    assert r.status_code in (409, 422)


async def test_rest_delete_session_verify_and_resume(client, auth, nlm_fake):
    session.save_cookies("primary", _cookies())
    r = await client.post("/api/google-accounts/primary/features/notebooklm/verify", headers=auth)
    assert r.status_code == 200 and r.json()["state"] == "ready" and nlm_fake.count("verify") == 1
    assert (await accounts.status())["state"] == "ready"
    nope = await client.post("/api/google-accounts/primary/features/nosuch/resume", headers=auth)
    assert nope.status_code == 422
    rr = await client.post("/api/google-accounts/primary/features/notebooklm/resume", headers=auth)
    assert rr.status_code == 409 and rr.json()["code"] == "busy"  # 멈춘 상태가 아니면 거절
    d = await client.delete("/api/google-accounts/primary/session", headers=auth)
    assert d.status_code == 200 and d.json()["state"] == "login_required" and not session.exists("primary")


async def test_legacy_notebooklm_auth_paths_still_work_and_share_the_account(client, auth, nlm_fake):
    session.save_cookies("primary", _cookies())
    ok = await client.post("/api/notebooklm/auth/verify", headers=auth)
    assert ok.status_code == 200 and ok.json()["state"] == "ready" and ok.json()["account"]["state"] == "ready"
    assert (await client.get("/api/google-accounts/primary", headers=auth)).json()["state"] == "ready"
    d = await client.delete("/api/notebooklm/auth/session", headers=auth)  # 기존 경로 삭제가 공통 세션을 지운다
    assert d.json()["state"] == "login_required"
    assert (await client.get("/api/google-accounts/primary", headers=auth)).json()["session"]["stored"] is False


@pytest.fixture
async def env(make_dir) -> Authz03Env:
    async with sessionmaker()() as db:
        return await ensure_authz03(db, make_dir)


async def test_agent_route_is_read_only_for_both_session_groups(client, env):
    work = {"Authorization": f"Bearer {issue_token(env.work_session_id)}"}
    personal = {"Authorization": f"Bearer {issue_token(env.personal_session_id)}"}
    assert (await client.get("/api/agent/google-accounts", headers=work)).status_code == 200
    assert (await client.get("/api/agent/google-accounts/primary", headers=work)).status_code == 200
    ok = await client.get("/api/agent/google-accounts", headers=personal)
    assert ok.status_code == 200 and ok.json()["accounts"][0]["account_id"] == "primary"
    # 로그인 창·세션 삭제·재개·확인은 에이전트에 노출하지 않는다
    for method, path in [("POST", "/api/agent/google-accounts/primary/login"),
                         ("POST", "/api/agent/google-accounts/primary/login/complete"),
                         ("POST", "/api/agent/google-accounts/primary/login/cancel"),
                         ("DELETE", "/api/agent/google-accounts/primary/session"),
                         ("POST", "/api/agent/google-accounts/primary/features/notebooklm/resume"),
                         ("POST", "/api/agent/google-accounts/primary/features/notebooklm/verify")]:
        assert (await client.request(method, path, headers=personal)).status_code in (404, 405), path


def test_status_tool_is_read_only_and_listed():
    from ttakkari.api.assistant import TOOLS
    from ttakkari.orchestrator.tool_policy import READ_ONLY_TOOLS

    assert "google_account_status" in READ_ONLY_TOOLS
    tool = next(t for t in TOOLS if t["name"] == "google_account_status")
    assert tool["who"] == "개인 실행"
    assert not {"google_account_login", "google_account_delete_session"} & {t["name"] for t in TOOLS}
