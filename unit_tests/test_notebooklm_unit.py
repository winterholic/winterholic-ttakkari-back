"""NotebookLM 연동 단위 검사(ADR 0045). DB·브라우저·Google 없이 돌린다. 상류 클라이언트는 쓰지 않고 예외·DTO 매핑만 본다."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from types import SimpleNamespace
from typing import ClassVar

os.environ.setdefault("TTAKKARI_NO_ENV_FILE", "1")

import pytest

from ttakkari.config import get_settings
from ttakkari.notebooklm import dedupe, errors, login, redact, session
from ttakkari.notebooklm.adapters import notebooklm_py as adapter
from ttakkari.notebooklm.export import build_ledger_export
from ttakkari.notebooklm.ports import (
    Citation,
    NotebookLMBackend,
    QueryResult,
    SourceInfo,
    SourceRef,
    Span,
)
from ttakkari.notebooklm.service import backoff_delay

COOKIE_SID = "g.a000fakeSIDvalue-0123456789abcdefghijklmnop"
COOKIE_TS = "sidts-FAKEVALUE-0123456789abcdefghij"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TTAKKARI_HOME", str(tmp_path))
    get_settings.cache_clear()
    redact.clear_secrets()
    yield tmp_path
    redact.clear_secrets()
    monkeypatch.undo()
    get_settings.cache_clear()


def _cookies():
    base = {"domain": ".google.com", "path": "/", "expires": 4102444800, "httpOnly": True, "secure": True, "sameSite": "None"}
    return [
        {**base, "name": "SID", "value": COOKIE_SID},
        {**base, "name": "__Secure-1PSIDTS", "value": COOKIE_TS},
        {**base, "name": "NID", "value": "nid-value-0123456789abcdef"},
        {"name": "other", "value": "x" * 20, "domain": ".example.com", "path": "/"},
    ]


# ------------------------------------------------------------------ 마스킹

def test_mask_registered_values_and_patterns():
    redact.register_secrets([COOKIE_SID])
    text = f"boom SID={COOKIE_SID} Cookie: SID=abc; __Secure-1PSIDTS=zzz\nAuthorization: Bearer abc.def\n" \
           f'{{"csrf_token":"tok-1234567890"}} https://x/?at=SECRETTOKEN&a=1 ' + "A1b2C3d4" * 6
    out = redact.mask(text)
    for leaked in (COOKIE_SID, "SID=abc", "zzz", "Bearer abc", "tok-1234567890", "SECRETTOKEN", "A1b2C3d4" * 6):
        assert leaked not in out
    assert "a=1" in out  # 무관한 쿼리는 남긴다


def test_safe_error_is_one_line_and_bounded():
    redact.register_secrets([COOKIE_TS])
    err = RuntimeError(f"fail {COOKIE_TS}\n" + "x" * 1000)
    out = redact.safe_error(err)
    assert COOKIE_TS not in out and "\n" not in out and len(out) <= redact.MAX_ERROR_CHARS
    assert out.startswith("RuntimeError")


def test_session_save_masks_values_and_hides_them_from_names(home):
    names = session.save_cookies("primary", _cookies())
    assert "SID" in names and "other" not in names  # 구글 도메인만
    assert COOKIE_SID not in json.dumps(names)
    assert COOKIE_SID not in redact.mask(f"error with {COOKIE_SID}")  # 저장 시 값이 마스킹 대상으로 등록된다


# ------------------------------------------------------------------ 세션 저장

def test_session_permissions_and_atomic_replace(home):
    session.save_cookies("primary", _cookies())
    p = session.storage_path("primary")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(p.parent.parent.stat().st_mode) == 0o700
    assert session.mode_ok("primary")
    first = p.stat().st_ino
    session.save_cookies("primary", _cookies())
    assert p.stat().st_ino != first  # 임시 파일을 만든 뒤 교체했다
    assert not list(p.parent.glob(".storage_state.json.*.tmp"))
    state = json.loads(p.read_text())
    assert set(state) == {"cookies", "origins"} and all(c["domain"].endswith("google.com") for c in state["cookies"])
    p.chmod(0o644)
    assert not session.mode_ok("primary")


def test_session_save_fails_if_private_directory_permissions_cannot_be_set(home, monkeypatch):
    account_dir = session.account_dir("primary")
    account_dir.mkdir(parents=True)
    original_chmod = session.Path.chmod

    def fail_for_account_dir(path, mode, *args, **kwargs):
        if path == account_dir:
            raise PermissionError("permission denied")
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(session.Path, "chmod", fail_for_account_dir)
    with pytest.raises(PermissionError):
        session.save_cookies("primary", _cookies())
    assert not session.exists("primary")


def test_session_requires_login_cookies_and_valid_account(home):
    with pytest.raises(errors.AuthRequired):
        session.save_cookies("primary", [c for c in _cookies() if c["name"] != "__Secure-1PSIDTS"])
    assert not session.exists("primary")
    for bad in ("../x", "A", "", "a/b", "x" * 40):
        with pytest.raises(errors.Invalid):
            session.check_account(bad)


def test_cookie_names_never_expose_values(home):
    session.save_cookies("primary", _cookies())
    names = session.cookie_names("primary")
    assert "SID" in names and COOKIE_SID not in str(names)
    session.delete("primary")
    assert not session.exists("primary") and session.cookie_names("primary") == []


# ------------------------------------------------------------------ 중복 키

def test_url_normalization_merges_variants():
    a = dedupe.normalize_url("HTTPS://Example.com:443/a/b/?utm_source=x&b=2&a=1#frag")
    b = dedupe.normalize_url("https://example.com/a/b?a=1&b=2")
    assert a == b == ("https://example.com/a/b?a=1&b=2", "url")
    yt = {dedupe.normalize_url(u) for u in (
        "https://youtu.be/dQw4w9WgXcQ?si=abc", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/shorts/dQw4w9WgXcQ")}
    assert yt == {("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube")}
    assert dedupe.url_key("https://example.com/") != dedupe.url_key("https://example.com/x")


@pytest.mark.parametrize("bad", ["ftp://x.com/a", "javascript:alert(1)", "http://localhost/x", "http://127.0.0.1/x",
                                 "http://10.0.0.5/", "https://user:pw@x.com/", "not a url", "", "https://x.com/a b"])
def test_url_rejects_unsafe(bad):
    with pytest.raises(errors.Invalid):
        dedupe.normalize_url(bad)


def test_text_and_file_keys_are_content_hashes():
    assert dedupe.text_key("a\r\nb ") == dedupe.text_key("a\nb")
    assert dedupe.text_key("a") != dedupe.text_key("b")
    assert dedupe.file_key("abc").startswith("file:")
    assert len(dedupe.url_key("https://x.com/")) <= 80


# ------------------------------------------------------------------ 재시도 계산

def test_backoff_is_exponential_and_capped():
    assert backoff_delay(0, 1.0, 8.0, jitter=False) == 1.0
    assert backoff_delay(1, 1.0, 8.0, jitter=False) == 2.0
    assert backoff_delay(2, 1.0, 8.0, jitter=False) == 4.0
    assert backoff_delay(10, 1.0, 8.0, jitter=False) == 8.0
    assert 8.0 <= backoff_delay(10, 1.0, 8.0) <= 10.0


# ------------------------------------------------------------------ 상류 예외 매핑

def test_exception_mapping_hides_upstream_text():
    import notebooklm as up

    leaked = f"Cookie: SID={COOKIE_SID} body"
    m = adapter.map_exception(up.RateLimitError(leaked, retry_after=9))
    assert isinstance(m, errors.RateLimited) and m.retry_after == 9 and COOKIE_SID not in m.message
    assert isinstance(adapter.map_exception(up.AuthError(leaked)), errors.AuthRequired)
    assert isinstance(adapter.map_exception(up.NotebookNotFoundError("nb")), errors.NotFound)
    assert isinstance(adapter.map_exception(up.ServerError(leaked, status_code=503)), errors.Upstream)
    assert isinstance(adapter.map_exception(up.ClientError(leaked, status_code=400)), errors.UpstreamRejected)
    assert isinstance(adapter.map_exception(up.ValidationError("bad")), errors.Invalid)
    # 쓰기에서 시간 초과·연결 오류는 결과 불명으로 표시한다(읽기는 아님)
    assert adapter.map_exception(up.NetworkError("x"), write=True).outcome_unknown is True
    assert adapter.map_exception(up.NetworkError("x")).outcome_unknown is False
    assert adapter.map_exception(TimeoutError(), write=True).outcome_unknown is True
    assert isinstance(adapter.map_exception(up.NetworkError("x")), errors.Transient)
    assert adapter.map_exception(up.NetworkError("x")).retryable and not adapter.map_exception(up.ClientError("x")).retryable


def test_protection_text_fails_closed():
    import notebooklm as up

    for text in ("https://www.google.com/sorry/index", "Please solve the reCAPTCHA", "unusual traffic from your network"):
        m = adapter.map_exception(up.ClientError(text, status_code=403))
        assert isinstance(m, errors.ProtectionSignal) and not m.retryable


def test_query_result_separates_answer_citations_sources():
    import notebooklm as up

    ref = up.ChatReference(source_id="s1", citation_number=1, cited_text="원문 인용", start_char=10, end_char=20,
                           answer_anchor_start=3, answer_anchor_end=9, chunk_id="c1", score=0.65)
    bare = up.ChatReference(source_id="s2", citation_number=2)
    ask = SimpleNamespace(answer="답변 [1][2]", conversation_id="conv", turn_number=1, references=[ref, bare])
    r = adapter.to_query_result("nb", "질문?", ask)
    assert r.answer == "답변 [1][2]" and r.query == "질문?"
    c1, c2 = r.citations
    assert c1.quoted_text == "원문 인용" and c1.source_span == Span(start=10, end=20) and c1.answer_span == Span(start=3, end=9)
    assert c2.quoted_text is None and c2.source_span is None and c2.answer_span is None  # 없으면 꾸미지 않는다
    assert [(s.id, s.citation_numbers) for s in r.sources] == [("s1", [1]), ("s2", [2])]


def test_capabilities_state_unsupported_features():
    caps = {c.name: c for c in adapter.NotebookLMPyBackend("primary").capabilities()}
    assert caps["notebooks"].available and caps["sources.url"].available and caps["query.citations"].available
    for name in ("audio_overview", "video_overview", "research", "drive_sources", "sharing"):
        assert caps[name].available is False and caps[name].reason


def test_adapter_satisfies_backend_protocol():
    assert isinstance(adapter.NotebookLMPyBackend("primary"), NotebookLMBackend)


def test_adapter_uses_only_public_upstream_api():
    import inspect

    src = inspect.getsource(adapter)
    assert "notebooklm._" not in src and "from notebooklm._" not in src
    for banned in ("curl_cffi", "impersonate", "master_token", "allow_headless=True", "playwright"):
        assert banned not in src.replace("allow_headless", "allow_headless")  # 우회 성격 기능은 쓰지 않는다


# ------------------------------------------------------------------ 지식 원장 내보내기

def test_ledger_export_keeps_source_analysis_citation_apart():
    result = QueryResult(
        notebook_id="nb", query="무엇?", answer="분석 텍스트 [1]", conversation_id="cv", turn_number=2,
        citations=[Citation(number=1, source_id="s1", quoted_text="원문 발췌", source_span=Span(start=0, end=5),
                            answer_span=Span(start=6, end=9), chunk_id="c"),
                   Citation(number=2, source_id="s2")],
        sources=[SourceRef(id="s1", title="문서", url="https://x.com/a", kind="web_page", citation_numbers=[1]),
                 SourceRef(id="s2", citation_numbers=[2])],
        created_at="2026-10-19T00:00:00+00:00")
    out = build_ledger_export(result, [SourceInfo(id="s1", title="문서", url="https://x.com/a", kind="web_page")])
    assert out["analysis"]["text"] == "분석 텍스트 [1]" and out["analysis"]["promoted"] is False
    assert out["analysis"]["review_status"] == "unreviewed"
    assert out["assertions"] == [] and out["promotion"]["auto_promoted"] is False
    units = {u["source_id"]: u for u in out["source_units"]}
    assert units["s1"]["excerpts"][0]["text"] == "원문 발췌" and units["s1"]["excerpts"][0]["span"] == {"start": 0, "end": 5}
    assert units["s1"]["url"] == "https://x.com/a"
    assert units["s2"]["excerpts"] == []  # 인용 텍스트가 없으면 원문을 만들어 내지 않는다
    assert out["citations"][0]["answer_span"] == {"start": 6, "end": 9}
    assert "분석 텍스트" not in json.dumps(out["source_units"], ensure_ascii=False)  # 분석이 원문 영역에 섞이지 않는다
    assert out["origin"]["conversation_id"] == "cv" and out["origin"]["system"] == "notebooklm"


# ------------------------------------------------------------------ 로그인 판정

def test_protection_signal_and_login_complete():
    good = [{"name": "SID", "value": "v" * 8, "domain": ".google.com"},
            {"name": "__Secure-1PSIDTS", "value": "v" * 8, "domain": ".google.com"}]
    assert login.protection_signal(["https://accounts.google.com/signin/v2/challenge/recaptcha"])
    assert login.protection_signal(["https://www.google.com/sorry/index?continue=x"])
    assert not login.protection_signal(["https://notebooklm.google.com/", "https://example.com/captcha"])
    assert login.login_complete(["https://notebooklm.google.com/"], good)
    assert not login.login_complete(["https://accounts.google.com/v3/signin"], good)
    assert not login.login_complete(["https://notebooklm.google.com/", "https://accounts.google.com/x"], good)
    assert not login.login_complete(["https://notebooklm.google.com/"], good[:1])
    assert login.is_loopback_ws("ws://127.0.0.1:9222/devtools/browser/x")
    assert not login.is_loopback_ws("ws://0.0.0.0:9222/devtools/browser/x")
    assert not login.is_loopback_ws("wss://127.0.0.1/x")


class _FakeProc:
    def __init__(self) -> None:
        self.stderr = None
        self.code: int | None = None
        self.terminated = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.code = 0 if self.code is None else self.code
        return self.code

    def terminate(self):
        self.terminated, self.code = True, 0

    def kill(self):
        self.code = -9


class _FakeBrowser:
    """로그인 창 시나리오: 틱마다 (페이지 URL 목록, 쿠키 목록)을 순서대로 돌려준다."""

    script: ClassVar[list] = []
    closed = False
    cookie_reads = 0

    def __init__(self, ws_url: str) -> None:
        self.i = 0

    async def open(self):
        pass

    async def close(self):
        type(self).closed = True

    async def send(self, method, params=None, timeout=10):
        return {}

    async def page_urls(self):
        step = type(self).script[min(self.i, len(type(self).script) - 1)]
        return step[0]

    async def cookies(self):
        type(self).cookie_reads += 1
        step = type(self).script[min(self.i, len(type(self).script) - 1)]
        self.i += 1
        return step[1]


@pytest.fixture
def fake_login(home, monkeypatch):
    proc = _FakeProc()

    async def fake_launch(chrome, profile):
        return proc, "ws://127.0.0.1:9/devtools/browser/x"

    monkeypatch.setattr(login, "_launch", fake_launch)
    monkeypatch.setattr(login, "find_chrome", lambda: login.Path("/bin/true"))
    monkeypatch.setattr(login, "profile_chrome_pids", list)
    monkeypatch.setattr(login, "_Browser", _FakeBrowser)
    monkeypatch.setattr(login, "POLL_SECONDS", 0.01)
    _FakeBrowser.closed, _FakeBrowser.cookie_reads = False, 0
    login.manager = login.LoginManager()
    return proc


async def _wait_login(mgr, account="primary", seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        if not mgr.state(account).active:
            return mgr.state(account)
        await asyncio.sleep(0.02)
    raise AssertionError("login still active")


async def test_login_flow_saves_session_and_closes_window(fake_login):
    app = "https://notebooklm.google.com/"
    _FakeBrowser.script = [(["https://accounts.google.com/v3/signin"], []), (["https://accounts.google.com/v3/signin"], []),
                           ([app], _cookies()), ([app], _cookies()), ([app], _cookies())]
    done = []

    async def on_done(st):
        done.append(st.state)

    st = await login.manager.start("primary", on_done=on_done)
    assert st.active
    final = await _wait_login(login.manager)
    assert final.state == "done" and done == ["done"]
    assert session.exists("primary") and "SID" in final.cookie_names
    assert _FakeBrowser.closed  # 창을 닫았다
    assert COOKIE_SID not in json.dumps(final.to_dict())


async def test_login_protection_signal_stops_extraction_and_times_out(fake_login, monkeypatch):
    monkeypatch.setattr(get_settings(), "notebooklm_login_timeout_seconds", 1)  # 보호 화면이 계속 뜨다 1초 뒤 시간 초과
    _FakeBrowser.script = [(["https://accounts.google.com/signin/v2/challenge/recaptcha"], _cookies())]
    st = await login.manager.start("primary")
    final = await _wait_login(login.manager)
    assert final.state == "timeout"
    assert _FakeBrowser.cookie_reads == 0  # 보호 신호 화면에서는 쿠키를 읽지도 저장하지도 않는다
    assert not session.exists("primary") and _FakeBrowser.closed
    assert st.account_id == "primary"


async def test_login_cancel_and_duplicate_lock(fake_login):
    _FakeBrowser.script = [(["https://accounts.google.com/v3/signin"], [])]
    await login.manager.start("primary")
    with pytest.raises(errors.Busy):
        await login.manager.start("primary")  # 같은 계정의 두 번째 창은 거절
    await asyncio.sleep(0.05)
    cancelled = await login.manager.cancel("primary")
    assert cancelled.state == "cancelled" and _FakeBrowser.closed and not session.exists("primary")


async def test_login_user_closing_window_ends_flow(fake_login):
    _FakeBrowser.script = [(["https://accounts.google.com/v3/signin"], [])]
    await login.manager.start("primary")
    await asyncio.sleep(0.05)
    fake_login.code = 0  # 사용자가 창을 닫음
    final = await _wait_login(login.manager)
    assert final.state == "cancelled"


async def test_login_refuses_when_leftover_window_exists(fake_login, monkeypatch):
    monkeypatch.setattr(login, "profile_chrome_pids", lambda: [123])
    with pytest.raises(errors.Busy):
        await login.manager.start("primary")


def test_launch_args_bind_loopback_and_use_dedicated_profile(home, monkeypatch):
    seen = {}

    class P:
        stderr = iter(["DevTools listening on ws://127.0.0.1:5555/devtools/browser/abc\n"])

        def poll(self):
            return None

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _proc, ws = asyncio.run(login._launch(login.Path("/chrome"), get_settings().notebooklm_profile_dir))
    argv = seen["argv"]
    assert "--remote-debugging-address=127.0.0.1" in argv and "--remote-debugging-port=0" in argv
    assert f"--user-data-dir={get_settings().notebooklm_profile_dir}" in argv
    assert not any("headless" in a or "enable-automation" in a for a in argv)  # 일반 보이는 창, 자동화 플래그 없음
    assert ws.startswith("ws://127.0.0.1:")
    assert stat.S_IMODE(get_settings().notebooklm_profile_dir.stat().st_mode) == 0o700


def test_login_does_not_launch_when_profile_permissions_cannot_be_set(home, monkeypatch):
    profile = get_settings().notebooklm_profile_dir
    original_chmod = login.Path.chmod
    launched = False

    def fail_for_profile(path, mode, *args, **kwargs):
        if path == profile:
            raise PermissionError("permission denied")
        return original_chmod(path, mode, *args, **kwargs)

    def fake_popen(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("Chrome must not launch with an unprotected profile")

    monkeypatch.setattr(login.Path, "chmod", fail_for_profile)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(PermissionError):
        asyncio.run(login._launch(login.Path("/chrome"), profile))
    assert not launched
