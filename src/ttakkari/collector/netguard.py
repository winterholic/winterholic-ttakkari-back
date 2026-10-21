"""Collector Platform: URL normalization and SSRF guard for outbound fetches (CP-S2-04).

Every address a hostname resolves to must be public. One non-public A/AAAA record rejects the host, so a
mixed answer cannot be used to steer the connection. The transport connects to an address that was validated here,
never to a second DNS answer (see fetcher.PinnedTransport), which closes the check-then-connect rebinding window.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import (
    SplitResult,
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

DEFAULT_PORTS = {"http": 80, "https": 443}
TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "yclid", "msclkid"})
MAX_URL_LENGTH = 2048
_HOST_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


class UrlRejected(ValueError):
    """The URL can never be fetched under the guard. `code` is the stable reason stored on the item."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    host: str  # lowercase, IDNA (punycode) encoded, IPv6 without brackets
    port: int
    target: str  # path + query, normalized
    url: str  # canonical string, fragment dropped, default port omitted

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def normalize_url(
    raw: str,
    *,
    allowed_schemes: Iterable[str] = ("https",),
    allowed_ports: Iterable[int] | None = None,
    strip_tracking: bool = True,
) -> ParsedUrl:
    """Parse and canonicalize a fetch target or reject it. No network access."""
    if not isinstance(raw, str) or not raw.strip():
        raise UrlRejected("url_invalid", "empty")
    raw = raw.strip()
    if len(raw) > MAX_URL_LENGTH or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise UrlRejected("url_invalid", "length or control character")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as e:
        raise UrlRejected("url_invalid", str(e)) from e
    scheme = parts.scheme.lower()
    if scheme not in set(allowed_schemes):
        raise UrlRejected("scheme_not_allowed", scheme or "missing")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlRejected("userinfo_not_allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlRejected("url_invalid", "no host")
    host = _canonical_host(host)
    effective_port = port or DEFAULT_PORTS[scheme]
    ports = {DEFAULT_PORTS[scheme]} if allowed_ports is None else set(allowed_ports)
    if effective_port not in ports:
        raise UrlRejected("port_not_allowed", str(effective_port))
    path = _normalize_path(parts.path or "/")
    query = _normalize_query(parts.query, strip_tracking)
    target = path + (f"?{query}" if query else "")
    netloc_host = f"[{host}]" if ":" in host else host
    netloc = netloc_host if effective_port == DEFAULT_PORTS[scheme] else f"{netloc_host}:{effective_port}"
    return ParsedUrl(scheme, host, effective_port, target, urlunsplit(SplitResult(scheme, netloc, path, query, "")))


def _canonical_host(host: str) -> str:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+|0x[0-9a-f.x]+", host):
        # 2130706433, 0x7f.1 and similar spellings of an IPv4 address are rejected, not interpreted.
        raise UrlRejected("host_ambiguous_ip", host)
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise UrlRejected("url_invalid", "host is not valid IDNA") from e
    if len(ascii_host) > 253 or not all(_HOST_LABEL.match(label) for label in ascii_host.split(".")):
        raise UrlRejected("url_invalid", "host label")
    return ascii_host


def _normalize_path(path: str) -> str:
    """Resolve dot segments and re-encode each segment once (RFC 3986 5.2.4 style)."""
    segments: list[str] = []
    for segment in path.split("/")[1:]:
        if segment == ".":
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(quote(unquote(segment), safe="!$&'()*+,;=:@-._~"))
    last = path.rsplit("/", 1)[-1]
    if last in (".", ".."):
        segments.append("")
    return "/" + "/".join(segments)


def _normalize_query(query: str, strip_tracking: bool) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    if strip_tracking:
        pairs = [(k, v) for k, v in pairs if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)]
    return urlencode(pairs, quote_via=quote)


def is_public_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return is_public_ip(addr.ipv4_mapped)
        # 6to4 / Teredo / NAT64 can embed a private IPv4 address; refuse them rather than decode them.
        if addr in ipaddress.ip_network("2002::/16") or addr in ipaddress.ip_network("2001::/32"):
            return False
        if addr in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"))
    )


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


@dataclass(frozen=True)
class NetGuard:
    """Resolves and validates hosts. `allow_networks` is only for local fixture servers in tests; production
    code never sets it, and the pipeline constructs the default guard."""

    resolver: Resolver = system_resolver
    allow_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    def _permitted(self, addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if any(addr in net for net in self.allow_networks if net.version == addr.version):
            return True
        return is_public_ip(addr)

    async def resolve(self, host: str, port: int) -> list[str]:
        """Return the validated addresses of `host`, or raise UrlRejected."""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not self._permitted(literal):
                raise UrlRejected("address_not_public", str(literal))
            return [str(literal)]
        try:
            answers = await self.resolver(host, port)
        except (OSError, UnicodeError) as e:
            raise UrlRejected("dns_failed", str(e)) from e
        if not answers:
            raise UrlRejected("dns_failed", "no address")
        parsed = []
        for a in answers:
            try:
                parsed.append(ipaddress.ip_address(a))
            except ValueError as e:
                raise UrlRejected("dns_failed", f"unparseable address {a!r}") from e
        bad = [str(a) for a in parsed if not self._permitted(a)]
        if bad:
            raise UrlRejected("address_not_public", f"{host} -> {bad[0]}")
        return [str(a) for a in parsed]
