"""Script-shell pages: the isolated renderer's network policy, and a real Chromium run when a runtime exists."""
import asyncio
import ipaddress
import json
import os
from pathlib import Path

import pytest
from collector_support import FixtureServer, fake_resolver


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    yield


@pytest.fixture(autouse=True)
def _clean():
    yield


from ttakkari.collector import render
from ttakkari.collector.fetcher import FetchLimits, SafeFetcher
from ttakkari.collector.netguard import NetGuard, UrlRejected, normalize_url

LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"),)
SHELL = ('<html><head><title>Docs app</title><script src="/app.js"></script></head>'
         '<body><div id="root"></div><noscript>You need to enable JavaScript to run this app.</noscript></body></html>')
APP_JS = """
fetch('/api/page').then(r => r.json()).then(data => {
  document.getElementById('root').innerHTML = '<article><h1>' + data.title + '</h1><p>' + data.body + '</p></article>';
});
try { new WebSocket('ws://docs.test/socket'); } catch (e) { window.__ws_blocked = true; }
fetch('http://internal.test/secret').then(r => r.text()).then(t => { window.__leak = t; }).catch(() => {});
"""
BODY = " ".join(f"Sentence {i} explains how the harness orchestrates retrieval, tools and evaluation loops in production." for i in range(12))


class Recorded:
    def __init__(self):
        self.urls: list[str] = []


def loopback_fetch(server_port: int, recorded: Recorded):
    """The collector's real NetGuard + pinned transport, with only the fixture hosts allowed to resolve to loopback."""
    resolver = fake_resolver({"docs.test": ["127.0.0.1"], "internal.test": ["10.0.0.5"]})
    guard = NetGuard(resolver=resolver, allow_networks=LOOPBACK)
    fetcher = SafeFetcher(guard, user_agent="t", limits=FetchLimits(total_timeout=5))

    async def fetch(url: str):
        recorded.urls.append(url)
        parsed = normalize_url(url, allowed_schemes=("http", "https"), allowed_ports=(server_port,), strip_tracking=False)
        if parsed.host == "internal.test":
            raise UrlRejected("address_not_public", parsed.host)  # a private answer must be refused before any connect
        response = await fetcher.fetch(parsed, allowed_schemes=("http",), allowed_ports=(server_port,), accept="*/*")
        return response.status, {"content-type": response.headers.get("content-type", "")}, response.body

    return fetch, fetcher


class FakeBrowser:
    """Stands in for Chromium: asks the broker for the requests a hostile page would make."""

    def __init__(self, requests, html="<html><body><p>done</p></body></html>"):
        self.requests, self.html, self.sent, self.queue = requests, html, [], asyncio.Queue()
        self.replies: dict[int, dict] = {}
        self.closed = False

    async def send(self, message):
        self.sent.append(message)
        if message["t"] == "start":
            for index, request in enumerate(self.requests, start=1):
                self.queue.put_nowait({"t": "req", "id": index, "method": "GET", "navigation": False, **request})
        elif message["t"] == "res":
            self.replies[message["id"]] = message
            if len(self.replies) == len(self.requests):
                self.queue.put_nowait({"t": "done", "html": self.html, "title": "t", "version": "fake"})

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True


async def fake_fetch(url):
    return 200, {"content-type": "application/javascript"}, b"console.log(1)"


async def test_broker_allows_only_get_script_style_xhr_fetch_and_counts_what_it_blocks():
    browser = FakeBrowser([
        {"url": "https://cdn.test/app.js", "type": "script"}, {"url": "https://cdn.test/logo.png", "type": "image"},
        {"url": "https://cdn.test/api", "type": "fetch", "method": "POST"}, {"url": "https://other.test/", "type": "document", "navigation": True},
        {"url": "https://cdn.test/style.css", "type": "stylesheet"},
    ])
    result = await render.render_html("https://docs.test/", b"<html></html>", fetch=fake_fetch, channel=browser)
    assert [browser.replies[i]["action"] for i in sorted(browser.replies)] == ["fulfill", "abort", "abort", "abort", "fulfill"]
    assert result.requests == 2
    assert result.blocked == {"type:image": 1, "method": 1, "navigation": 1}
    assert browser.closed


async def test_broker_caps_request_count_and_total_bytes():
    many = FakeBrowser([{"url": f"https://cdn.test/{i}.js", "type": "script"} for i in range(55)])
    result = await render.render_html("https://docs.test/", b"x", fetch=fake_fetch, channel=many)
    assert result.requests == 50 and result.blocked == {"request_cap": 5}

    async def big(url):
        return 200, {"content-type": "text/javascript"}, b"x" * (render.MAX_TOTAL_BYTES // 2 + 1)
    heavy = FakeBrowser([{"url": f"https://cdn.test/{i}.js", "type": "script"} for i in range(2)])
    result = await render.render_html("https://docs.test/", b"x", fetch=big, channel=heavy)
    assert result.blocked == {"byte_cap": 1}


async def test_broker_caps_at_exact_50_requests_and_blocks_51st():
    exact_50 = FakeBrowser([{"url": f"https://cdn.test/{i}.js", "type": "script"} for i in range(50)])
    res_50 = await render.render_html("https://docs.test/", b"x", fetch=fake_fetch, channel=exact_50)
    assert res_50.requests == 50
    assert "request_cap" not in res_50.blocked

    over_51 = FakeBrowser([{"url": f"https://cdn.test/{i}.js", "type": "script"} for i in range(51)])
    res_51 = await render.render_html("https://docs.test/", b"x", fetch=fake_fetch, channel=over_51)
    assert res_51.requests == 50
    assert res_51.blocked == {"request_cap": 1}


async def test_renderer_child_requests_enforce_policy_host_port_path_and_robots(monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock

    from ttakkari.collector.fetcher import FetchResult, SafeFetcher
    from ttakkari.collector.policy import SourcePolicySpec
    from ttakkari.collector.polite import PolicyBlocked

    spec = SourcePolicySpec(
        allowed_hosts=("docs.test",),
        allowed_path_prefixes=("/assets",),
        schemes=("https",),
        ports=(443,),
        permissions={"store": "allowed", "knowledge": "allowed"},
        basis="contract",
        checked_at=datetime.now(UTC),
    )

    async def mock_fetch(self, url, **kwargs):
        return FetchResult(
            requested_url=str(url),
            final_url=str(url),
            status=200,
            body=b"console.log('asset')",
            media_type="application/javascript",
            charset="utf-8",
            etag=None,
            last_modified=None,
            not_modified=False,
            redirects=(),
            headers={"content-type": "application/javascript"},
        )

    monkeypatch.setattr(SafeFetcher, "fetch", mock_fetch)

    fetch, closer = render.default_fetch(policy=spec)

    browser = FakeBrowser([
        {"url": "https://docs.test/assets/app.js", "type": "script"},
        {"url": "https://other-public.test/assets/ext.js", "type": "script"},
        {"url": "https://docs.test/forbidden/secret.js", "type": "script"},
        {"url": "https://docs.test:8443/assets/app.js", "type": "script"},
        {"url": "http://docs.test/assets/app.js", "type": "script"},
    ])

    result = await render.render_html("https://docs.test/", b"<html></html>", fetch=fetch, channel=browser)
    assert [browser.replies[i]["action"] for i in sorted(browser.replies)] == [
        "fulfill", "abort", "abort", "abort", "abort"
    ]
    assert browser.replies[1]["action"] == "fulfill"
    assert browser.replies[2]["reason"] == "policy_blocked:host_not_allowed"
    assert browser.replies[3]["reason"] == "policy_blocked:path_outside_policy"
    assert browser.replies[4]["reason"] in ("policy_blocked:port_not_allowed", "network_policy")
    assert browser.replies[5]["reason"] in ("policy_blocked:scheme_not_allowed", "network_policy")
    assert result.requests == 5
    assert result.blocked["policy_blocked:host_not_allowed"] == 1
    assert result.blocked["policy_blocked:path_outside_policy"] == 1
    await closer()

    # PoliteFetcher with robots policy test
    class FakePolite:
        def __init__(self):
            self.fetcher = MagicMock()
            self.fetcher.aclose = AsyncMock()

        async def fetch(self, parsed, policy, **kwargs):
            if "robots_blocked" in parsed.target:
                raise PolicyBlocked("robots_disallowed", parsed.target)
            res = MagicMock()
            res.result.status = 200
            res.result.headers = {"content-type": "application/javascript"}
            res.result.body = b"console.log('ok')"
            return res

    polite_fetch, _ = render.default_fetch(policy=spec, polite=FakePolite())
    browser_robots = FakeBrowser([
        {"url": "https://docs.test/assets/robots_blocked.js", "type": "script"},
    ])
    result_robots = await render.render_html("https://docs.test/", b"<html></html>", fetch=polite_fetch, channel=browser_robots)
    assert browser_robots.replies[1]["action"] == "abort"
    assert browser_robots.replies[1]["reason"] == "policy_blocked:robots_disallowed"
    assert result_robots.blocked["policy_blocked:robots_disallowed"] == 1


async def test_renderer_that_dies_or_hangs_is_a_failure_not_empty_content():
    class Dead(FakeBrowser):
        async def recv(self):
            return None
    with pytest.raises(render.RenderFailed) as dead:
        await render.render_html("https://docs.test/", b"x", fetch=fake_fetch, channel=Dead([]))
    assert dead.value.code == "renderer_exited"

    class Hung(FakeBrowser):
        async def recv(self):
            await asyncio.sleep(30)
    with pytest.raises(render.RenderFailed) as hung:
        await render.render_html("https://docs.test/", b"x", fetch=fake_fetch, channel=Hung([]), wall_seconds=0.05)
    assert hung.value.code == "render_wall_timeout"


def test_script_shell_detection_ignores_real_articles():
    assert render.looks_like_spa(SHELL.encode())
    article = "<html><body><script>var a=1</script><article>" + "<p>" + BODY + "</p></article></body></html>"
    assert not render.looks_like_spa(article.encode())
    assert not render.looks_like_spa(b"<html><body><p>short static page</p></body></html>")  # no script: nothing to render


async def test_static_text_never_triggers_rendering():
    page = "<html><head><title>Static</title></head><body><article><h1>Static</h1><p>" + BODY + "</p></article></body></html>"

    async def must_not_render(*args, **kwargs):
        raise AssertionError("rendered a page that already has text")
    signals = {}
    doc, links_body, _ = await render.extract_with_fallback(page.encode(), "https://docs.test/a", "utf-8", signals, render=must_not_render)
    assert "Sentence 3" in doc.text and links_body == page.encode() and signals == {}


async def test_unrenderable_shell_is_reported_with_its_reason_not_as_an_empty_page():
    async def unavailable(*args, **kwargs):
        raise render.RendererUnavailable("chromium_missing")
    signals = {}
    with pytest.raises(ValueError, match="empty_extraction"):
        await render.extract_with_fallback(SHELL.encode(), "https://docs.test/", "utf-8", signals, render=unavailable)
    assert signals["render"] == {"used": False, "error": "chromium_missing", "static_chars": 0, "reason": "spa_render_unavailable"}


async def test_borrowed_polite_fetcher_is_not_closed_by_renderer_and_supports_subsequent_pages():
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock

    from ttakkari.collector.policy import SourcePolicySpec

    mock_polite = MagicMock()
    mock_polite.fetcher = MagicMock()
    mock_polite.fetcher.aclose = AsyncMock()
    mock_polite.fetch = AsyncMock()

    res_mock = MagicMock()
    res_mock.result.status = 200
    res_mock.result.headers = {"content-type": "application/javascript"}
    res_mock.result.body = b"console.log('page1');"
    mock_polite.fetch.return_value = res_mock

    policy = SourcePolicySpec(
        allowed_hosts=("cdn.test",),
        schemes=("https",),
        ports=(443,),
        permissions={"store": "allowed"},
        basis="contract",
        checked_at=datetime.now(UTC),
    )

    browser = FakeBrowser([{"url": "https://cdn.test/app.js", "type": "script"}])
    result = await render.render_html("https://docs.test/", b"<html></html>", policy=policy, polite=mock_polite, channel=browser)
    assert result.requests == 1

    # borrowed polite 의 fetcher.aclose 는 렌더러 완료 시점에 호출되지 않아야 함 (소유권은 호출자에 있음)
    assert mock_polite.fetcher.aclose.await_count == 0

    # 순차 페이지 등 후속 fetch 도 같은 polite 로 여전히 정상 동작함
    fetch, closer = render.default_fetch(policy=policy, polite=mock_polite)
    status, _headers, body = await fetch("https://cdn.test/app.js")
    assert status == 200 and body == b"console.log('page1');"
    await closer()
    assert mock_polite.fetcher.aclose.await_count == 0


def _runtime():
    candidates = [os.environ.get("TTAKKARI_TEST_RENDER_RUNTIME"), str(Path(__file__).parents[2] / "back" / ".browser-runtime"),
                  str(Path(__file__).parents[1] / ".browser-runtime")]
    for root in filter(None, candidates):
        if (Path(root) / "node_modules" / "playwright-core").is_dir():
            browsers = Path.home() / "Library/Caches/ms-playwright"
            executable = render._find_chromium(browsers)
            node = render.shutil.which("node")
            if executable and node:
                return {"node": node, "runtime": root, "executable": str(executable), "browsers": str(browsers)}
    return None


@pytest.mark.skipif(_runtime() is None, reason="playwright-core and Chromium are not installed")
async def test_real_chromium_renders_a_script_shell_through_the_guarded_broker():
    async with FixtureServer() as server:
        server.page("/", SHELL)
        server.page("/app.js", APP_JS, "application/javascript")
        server.page("/api/page", json.dumps({"title": "Harness docs", "body": BODY}), "application/json")
        recorded = Recorded()
        fetch, fetcher = loopback_fetch(server.port, recorded)
        url = f"http://docs.test:{server.port}/"
        try:
            signals = {}

            async def run(url_, body):
                return await render.render_html(url_, body, fetch=fetch, info=_runtime())
            doc, links_body, charset = await render.extract_with_fallback(SHELL.encode(), url, "utf-8", signals, render=run)
        finally:
            await fetcher.aclose()
    assert "Sentence 5" in doc.text and doc.title
    assert signals["render"]["used"] and signals["render"]["rendered_chars"] > 200 and signals["render"]["static_chars"] == 0
    assert "Harness docs" in links_body.decode() and charset == "utf-8"
    assert f"http://docs.test:{server.port}/app.js" in recorded.urls and f"http://docs.test:{server.port}/api/page" in recorded.urls
    assert "http://internal.test/secret" in recorded.urls  # the page tried it ...
    assert signals["render"]["blocked"].get("network_policy") == 1  # ... and the guard refused it
    assert "docs.test/socket" not in " ".join(recorded.urls)  # WebSocket never reached the broker
    # nothing the page fetched was stored: only the rendered text leaves the renderer
    assert "console.log" not in doc.text and "fetch(" not in doc.text
