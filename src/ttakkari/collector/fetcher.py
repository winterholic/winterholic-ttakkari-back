"""Collector Platform: safe HTTP GET transport, redirect handling and bounded body read (CP-S2-04).

Transport and parsing are separate: this module only moves bytes. It never follows redirects on its own, never
uses environment proxies or credentials, never keeps cookies, and bounds time and size (decoded size included).

SSRF defence is layered:
1. normalize_url rejects bad schemes, userinfo, ports, ambiguous IP spellings before any DNS lookup
2. NetGuard validates every address a host resolves to
3. PinnedTransport connects to an address validated in the same step and keeps the original Host/SNI, so a
   second DNS answer can never be used for the connection (rebinding)
4. every redirect target goes through 1-3 again, plus the caller's per-hop `host_check` (source host allowlist)
"""
from __future__ import annotations

import asyncio
import email.utils
import ssl
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpcore
import httpx

from ttakkari.collector.netguard import NetGuard, ParsedUrl, UrlRejected, normalize_url

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_RETRY_AFTER_SECONDS = 24 * 3600
_SUPPORTED_ENCODINGS = frozenset({"", "identity", "gzip", "x-gzip", "deflate"})


@dataclass(frozen=True)
class FetchLimits:
    total_timeout: float = 30.0
    connect_timeout: float = 10.0
    max_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 5


class FetchError(Exception):
    """A fetch that produced no usable body. `retryable` is only true for timeouts, 429 and 5xx (architecture 4)."""

    def __init__(self, code: str, detail: str = "", *, retryable: bool = False, retry_after: float | None = None,
                 status: int | None = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    body: bytes
    media_type: str
    charset: str | None
    etag: str | None
    last_modified: str | None
    not_modified: bool = False
    redirects: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolve through the guard and connect to the validated address, never to a fresh DNS answer."""

    def __init__(self, guard: NetGuard) -> None:
        self._guard = guard
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = await self._guard.resolve(host, port)
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except httpcore.ConnectError as e:
                last = e
        raise last or httpcore.ConnectError("no address to connect to")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool uses GuardedBackend. HTTP/1.1 only, no proxy, no retries."""

    def __init__(self, guard: NetGuard, *, verify: bool | ssl.SSLContext = True) -> None:
        super().__init__(verify=verify, trust_env=False, http2=False, retries=0)
        # httpx exposes no hook for the network backend, so the pool it built is replaced by an equivalent one.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=15.0,
            http1=True,
            http2=False,
            network_backend=GuardedBackend(guard),
        )


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _split_content_type(value: str | None) -> tuple[str, str | None]:
    if not value:
        return "", None
    head, *params = [p.strip() for p in value.split(";")]
    charset = None
    for p in params:
        if p.lower().startswith("charset="):
            charset = p.split("=", 1)[1].strip().strip("\"'").lower() or None
    return head.lower(), charset


class _BoundedDecoder:
    """gzip/deflate decoding with a hard cap on the decoded size, so a bomb never materializes in memory."""

    def __init__(self, encoding: str, limit: int) -> None:
        self._encoding = encoding
        self._limit = limit
        self._out = 0
        self._fed = 0
        self._deflate_raw = False
        self._d = None if encoding in ("", "identity") else zlib.decompressobj(
            zlib.MAX_WBITS | 16 if "gzip" in encoding else zlib.MAX_WBITS
        )

    def feed(self, chunk: bytes) -> bytes:
        self._fed += len(chunk)
        if self._d is None:
            data = chunk
        else:
            data = self._decode(chunk)
        self._out += len(data)
        if self._out > self._limit:
            raise FetchError("response_too_large", f"decoded body exceeds {self._limit} bytes")
        return data

    def _decode(self, chunk: bytes) -> bytes:
        parts: list[bytes] = []
        budget = self._limit - self._out + 1
        try:
            data = self._d.decompress(chunk, budget)
            parts.append(data)
            while self._d.unconsumed_tail:
                budget -= len(data)
                if budget <= 0:
                    break
                data = self._d.decompress(self._d.unconsumed_tail, budget)
                parts.append(data)
        except zlib.error as e:
            if self._encoding == "deflate" and not self._deflate_raw and self._out == 0:
                self._deflate_raw = True
                self._d = zlib.decompressobj(-zlib.MAX_WBITS)  # some servers send raw deflate
                return self._decode(chunk)
            raise FetchError("content_encoding_invalid", str(e)) from e
        return b"".join(parts)

    def finish(self) -> None:
        if self._d is not None and self._fed and not self._d.eof:
            raise FetchError("content_encoding_invalid", "truncated compressed stream")


HostCheck = Callable[[ParsedUrl], str | None]
BeforeRequest = Callable[[ParsedUrl], Awaitable[None]]


class SafeFetcher:
    def __init__(
        self,
        guard: NetGuard | None = None,
        *,
        user_agent: str,
        limits: FetchLimits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool | ssl.SSLContext = True,
    ) -> None:
        self.guard = guard or NetGuard()
        self.limits = limits or FetchLimits()
        self.user_agent = user_agent
        self._client = httpx.AsyncClient(
            transport=transport or PinnedTransport(self.guard, verify=verify),
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(self.limits.total_timeout, connect=self.limits.connect_timeout),
            headers={"User-Agent": user_agent},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        url: ParsedUrl,
        *,
        allowed_schemes: tuple[str, ...] = ("https",),
        allowed_ports: tuple[int, ...] | None = None,
        host_check: HostCheck | None = None,
        before_request: BeforeRequest | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.1",
        max_bytes: int | None = None,
    ) -> FetchResult:
        """GET `url`, following up to `max_redirects` redirects hop by hop. Raises FetchError or UrlRejected."""
        try:
            async with asyncio.timeout(self.limits.total_timeout):
                return await self._fetch(
                    url, allowed_schemes, allowed_ports, host_check, before_request, etag, last_modified, accept,
                    max_bytes or self.limits.max_bytes,
                )
        except TimeoutError as e:
            raise FetchError("timeout", f"exceeded {self.limits.total_timeout}s", retryable=True) from e

    async def _fetch(self, start, schemes, ports, host_check, before_request, etag, last_modified, accept, cap):
        current = start
        hops: list[str] = []
        while True:
            if host_check is not None and (reason := host_check(current)) is not None:
                raise UrlRejected(reason, current.url)
            if before_request is not None:
                await before_request(current)
            headers = {"Accept": accept, "Accept-Encoding": "gzip, deflate"}
            if not hops:
                if etag:
                    headers["If-None-Match"] = etag
                if last_modified:
                    headers["If-Modified-Since"] = last_modified
            try:
                async with self._client.stream("GET", current.url, headers=headers) as response:
                    self._client.cookies.clear()
                    status = response.status_code
                    if status in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("redirect_without_location", status=status)
                        if len(hops) >= self.limits.max_redirects:
                            raise FetchError("too_many_redirects", f"more than {self.limits.max_redirects}")
                        hops.append(current.url)
                        current = normalize_url(
                            urljoin(current.url, location), allowed_schemes=schemes, allowed_ports=ports,
                            strip_tracking=False,
                        )
                        continue
                    return await self._read(response, start.url, current.url, hops, cap, status)
            except (UrlRejected, FetchError):
                raise
            except httpx.TimeoutException as e:
                raise FetchError("timeout", type(e).__name__, retryable=True) from e
            except (httpx.TransportError, httpcore.ConnectError, OSError) as e:
                raise FetchError("connect_failed", f"{type(e).__name__}: {e}", retryable=True) from e

    async def _read(self, response, requested_url, final_url, hops, cap, status) -> FetchResult:
        h = response.headers
        media_type, charset = _split_content_type(h.get("content-type"))
        retry_after = parse_retry_after(h.get("retry-after"))
        if status == 304:
            return FetchResult(requested_url, final_url, 304, b"", media_type, charset, h.get("etag"),
                               h.get("last-modified"), True, tuple(hops))
        if status == 429:
            raise FetchError("rate_limited", retryable=True, retry_after=retry_after, status=status)
        if status >= 500:
            raise FetchError("server_error", str(status), retryable=True, retry_after=retry_after, status=status)
        if status in (401, 403):
            raise FetchError("access_denied", str(status), status=status)
        if status in (404, 410):
            raise FetchError("not_found", str(status), status=status)
        if status < 200 or status >= 300:
            raise FetchError("unexpected_status", str(status), status=status)
        encoding = (h.get("content-encoding") or "").strip().lower()
        if encoding not in _SUPPORTED_ENCODINGS:
            raise FetchError("content_encoding_unsupported", encoding)
        declared = h.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap and encoding in ("", "identity"):
            raise FetchError("response_too_large", f"content-length {declared} exceeds {cap}")
        decoder = _BoundedDecoder(encoding, cap)
        body = bytearray()
        raw_total = 0
        async for chunk in response.aiter_raw():
            raw_total += len(chunk)
            if raw_total > cap:
                raise FetchError("response_too_large", f"raw body exceeds {cap} bytes")
            body += decoder.feed(chunk)
        decoder.finish()
        if not body:
            raise FetchError("empty_response", f"{status} with an empty body", status=status)
        return FetchResult(
            requested_url, final_url, status, bytes(body), media_type, charset, h.get("etag"), h.get("last-modified"),
            False, tuple(hops), {"content-type": h.get("content-type", ""), "date": h.get("date", "")},
        )
