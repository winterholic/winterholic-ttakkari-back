"""Isolated renderer for pages whose static HTML has no text because a script builds the content.

It is a fallback for one case only: the policy-checked static fetch produced no usable text and the markup looks like
a script shell. The page then runs in a Chromium child process that has no network access of its own. Every request
it makes is relayed to this module, which allows only GET script/stylesheet/xhr/fetch traffic through the same
DNS-pinned NetGuard fetcher as the rest of the collector, within request and byte caps. The rendered DOM is evaluated
as text only; nothing it fetches is stored, and robots/rights were already decided on the page URL by the static fetch.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lxml import html as lxml_html

from ttakkari.collector import parse
from ttakkari.collector.fetcher import FetchError, FetchLimits, SafeFetcher
from ttakkari.collector.netguard import NetGuard, UrlRejected, normalize_url
from ttakkari.config import get_settings

MIN_STATIC_CHARS = 200
ALLOWED_TYPES = frozenset({"script", "stylesheet", "xhr", "fetch"})
MAX_REQUESTS = 60
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_RESOURCE_BYTES = 2 * 1024 * 1024
WALL_SECONDS = 30
SETTLE_MS = 8_000
QUIET_MS = 600
MAX_HTML_CHARS = 5_000_000
CONCURRENCY = 6
WORKER = Path(__file__).with_name("render_worker.mjs")


class RendererUnavailable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RenderFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class RenderResult:
    html: str
    title: str | None
    engine: str
    requests: int
    bytes: int
    blocked: dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0


class Channel(Protocol):
    async def send(self, message: dict[str, Any]) -> None: ...
    async def recv(self) -> dict[str, Any] | None: ...
    async def close(self) -> None: ...


def looks_like_spa(body: bytes) -> bool:
    """Little visible text plus at least one script: the signature of a client-rendered shell."""
    try:
        tree = lxml_html.fromstring(body)
    except (ValueError, lxml_html.etree.ParserError):
        return False
    if not tree.xpath("//script"):
        return False
    for element in tree.xpath("//script|//style|//noscript"):
        element.drop_tree()
    return len(" ".join(tree.text_content().split())) < MIN_STATIC_CHARS


def _find_chromium(browsers: Path) -> Path | None:
    for pattern in ("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
                    "chromium_headless_shell-*/chrome-linux/headless_shell",
                    "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium", "chromium-*/chrome-linux*/chrome"):
        found = sorted(browsers.glob(pattern), reverse=True)
        if found:
            return found[0]
    return None


def runtime() -> dict[str, str]:
    """node, playwright-core and a Chromium build, or RendererUnavailable naming what is missing."""
    settings = get_settings()
    if not settings.collector_render_enabled:
        raise RendererUnavailable("render_disabled")
    node = shutil.which("node")
    if node is None:
        raise RendererUnavailable("node_missing")
    root = Path(settings.collector_render_runtime_dir) if settings.collector_render_runtime_dir else Path(__file__).parents[3] / ".browser-runtime"
    if not (root / "node_modules" / "playwright-core").is_dir():
        raise RendererUnavailable("playwright_core_missing")
    browsers = Path(settings.collector_render_browsers_path) if settings.collector_render_browsers_path else (
        Path.home() / ("Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright"))
    executable = _find_chromium(browsers)
    if executable is None:
        raise RendererUnavailable("chromium_missing")
    return {"node": node, "runtime": str(root), "executable": str(executable), "browsers": str(browsers)}


class NodeChannel:
    def __init__(self, proc: asyncio.subprocess.Process, home: tempfile.TemporaryDirectory) -> None:
        self._proc, self._home = proc, home

    async def send(self, message: dict[str, Any]) -> None:
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def recv(self) -> dict[str, Any] | None:
        line = await self._proc.stdout.readline()
        return json.loads(line) if line else None

    async def close(self) -> None:
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
        await self._proc.wait()
        self._home.cleanup()


async def spawn_node(info: dict[str, str]) -> Channel:
    home = tempfile.TemporaryDirectory(prefix="ttakkari-render-")
    # The page's scripts are untrusted: the child gets no collector environment, secrets or database access.
    env = {"PATH": str(Path(info["node"]).parent), "HOME": home.name, "TMPDIR": home.name, "PLAYWRIGHT_BROWSERS_PATH": info["browsers"]}
    proc = await asyncio.create_subprocess_exec(
        info["node"], str(WORKER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env, cwd=home.name, limit=16 * 1024 * 1024)
    return NodeChannel(proc, home)


Fetch = Callable[[str], Awaitable[tuple[int, dict[str, str], bytes]]]


def default_fetch(guard: NetGuard | None = None) -> tuple[Fetch, Callable[[], Awaitable[None]]]:
    fetcher = SafeFetcher(guard or NetGuard(), user_agent="TtakkariCollector/1.0 (render)",
                          limits=FetchLimits(total_timeout=10.0, max_bytes=MAX_RESOURCE_BYTES))

    async def fetch(url: str) -> tuple[int, dict[str, str], bytes]:
        response = await fetcher.fetch(normalize_url(url, allowed_schemes=("http", "https"), strip_tracking=False),
                                       allowed_schemes=("http", "https"), accept="*/*")
        return response.status, {"content-type": response.headers.get("content-type", "application/octet-stream")}, response.body

    return fetch, fetcher.aclose


async def render_html(url: str, body: bytes, *, content_type: str = "text/html; charset=utf-8", fetch: Fetch | None = None,
                      channel: Channel | None = None, info: dict[str, str] | None = None, wall_seconds: float = WALL_SECONDS) -> RenderResult:
    """Render `body` (the already fetched, policy-checked document at `url`) and return the resulting DOM."""
    started = time.monotonic()
    closer: Callable[[], Awaitable[None]] | None = None
    if fetch is None:
        fetch, closer = default_fetch()
    if channel is None:
        info = info or runtime()
        channel = await spawn_node(info)
    counters = {"requests": 0, "bytes": 0}
    blocked: dict[str, int] = {}
    tasks: set[asyncio.Task] = set()
    gate = asyncio.Semaphore(CONCURRENCY)

    def block(reason: str) -> dict[str, Any]:
        blocked[reason] = blocked.get(reason, 0) + 1
        return {"action": "abort", "reason": reason}

    async def answer(message: dict[str, Any]) -> dict[str, Any]:
        if message.get("navigation"):
            return block("navigation")
        if message.get("method") != "GET":
            return block("method")
        if message.get("type") not in ALLOWED_TYPES:
            return block("type:" + str(message.get("type")))
        if counters["requests"] >= MAX_REQUESTS:
            return block("request_cap")
        counters["requests"] += 1
        try:
            async with gate:
                status, headers, data = await fetch(message["url"])
        except UrlRejected:
            return block("network_policy")
        except (FetchError, TimeoutError, OSError):
            return block("fetch_failed")
        counters["bytes"] += len(data)
        if counters["bytes"] > MAX_TOTAL_BYTES:
            return block("byte_cap")
        return {"action": "fulfill", "status": status, "headers": headers, "body_b64": base64.b64encode(data).decode()}

    async def respond(message: dict[str, Any]) -> None:
        reply = await answer(message)
        await channel.send({"t": "res", "id": message["id"], **reply})

    try:
        await channel.send({
            "t": "start", "url": url, "content_type": content_type, "body_b64": base64.b64encode(body).decode(),
            "runtime": (info or {}).get("runtime", ""), "executable": (info or {}).get("executable"), "wall_ms": int(wall_seconds * 1000),
            "settle_ms": SETTLE_MS, "quiet_ms": QUIET_MS, "max_html_chars": MAX_HTML_CHARS})
        async with asyncio.timeout(wall_seconds + 5):
            while True:
                message = await channel.recv()
                if message is None:
                    raise RenderFailed("renderer_exited")
                kind = message.get("t")
                if kind == "req":
                    task = asyncio.create_task(respond(message))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                elif kind == "done":
                    return RenderResult(message["html"], message.get("title"), str(message.get("version", "chromium")),
                                        counters["requests"], counters["bytes"], blocked, time.monotonic() - started)
                elif kind == "error":
                    raise RenderFailed(str(message.get("code", "render_failed")))
    except TimeoutError as exc:
        raise RenderFailed("render_wall_timeout") from exc
    finally:
        for task in tasks:
            task.cancel()
        await channel.close()
        if closer is not None:
            await closer()


async def extract_with_fallback(body: bytes, url: str, charset: str | None, signals: dict[str, Any], *,
                                render: Callable[..., Awaitable[RenderResult]] = render_html) -> tuple[parse.ExtractedDocument, bytes, str | None]:
    """Static extraction first; render only when it yields no usable text from a script shell.

    Returns the document plus the bytes/charset whose links should be followed. Records what happened in
    `signals["render"]` either way, and raises a ValueError with an explicit reason when a shell cannot be rendered."""
    static_error: str | None = None
    static = None
    try:
        static = await asyncio.to_thread(parse.extract_document, body, url=url, kind="html", charset=charset)
        if len(static.text.strip()) >= MIN_STATIC_CHARS:
            return static, body, charset
    except ValueError as exc:
        static_error = str(exc)
    if not looks_like_spa(body):
        if static is None:
            raise ValueError(static_error or "empty_extraction")
        return static, body, charset
    static_chars = len(static.text.strip()) if static is not None else 0
    try:
        rendered = await render(url, body)
    except (RendererUnavailable, RenderFailed) as exc:
        signals["render"] = {"used": False, "error": exc.code, "static_chars": static_chars,
                             "reason": "spa_render_unavailable" if isinstance(exc, RendererUnavailable) else "spa_render_failed"}
        if static is None:
            raise ValueError(static_error or "empty_extraction") from exc
        return static, body, charset
    data = rendered.html.encode()
    document = await asyncio.to_thread(parse.extract_document, data, url=url, kind="html", charset="utf-8")
    signals["render"] = {"used": True, "engine": rendered.engine, "static_chars": static_chars, "rendered_chars": len(document.text.strip()),
                         "requests": rendered.requests, "bytes": rendered.bytes, "blocked": rendered.blocked,
                         "rendered_sha256": hashlib.sha256(data).hexdigest(), "seconds": round(rendered.elapsed, 2)}
    return document, data, "utf-8"
