"""Actions job 로그(ADR 0042 §5-4, H4).

상류는 302 로 서명된 URL(1분 유효)을 준다. 그 URL은 다음 조건에서만 따라간다:
https, 기본 포트, userinfo 없음, 호스트가 허용 목록(`*.githubusercontent.com`, `productionresultssa<N>.blob.core.windows.net`).
내려받는 요청에는 Authorization 을 절대 싣지 않고, 다시 리다이렉트하면 따라가지 않는다. 서명 URL은 응답·로그에 남기지 않는다.
허용 목록은 문서에 호스트 목록이 없어 알려진 값으로 시작한 가정이다(실제 Location 관측으로 확정, 미검증).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlsplit

import httpx

from ttakkari.github.cache import TTLCache
from ttakkari.github.client import (
    USER_AGENT,
    GithubClient,
    current_deadline,
    under_deadline,
)
from ttakkari.github.errors import GithubError
from ttakkari.github.schemas import LogWindow
from ttakkari.oplog.scrub import MASK, scrub

log = logging.getLogger("ttakkari.github")

LOG_BUDGET = 20.0
MAX_LOG_BYTES = 8 * 1024 * 1024
WINDOW_CHARS = 180_000  # oplog.scrub 의 처리 상한(200k) 안쪽
MAX_LINE = 20_000
LOG_TTL = 120.0
_HOST_SUFFIX = ".githubusercontent.com"
_AZURE_RESULTS = re.compile(r"^productionresultssa[0-9]+\.blob\.core\.windows\.net$")
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")  # PGP 는 "PRIVATE KEY BLOCK"
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")
# oplog.scrub 가 놓치는 형식(검증 V11d): Azure SAS 서명(`sig=`, GitHub 로그 다운로드 URL 이 이 형태)과 Stripe 비밀 키.
_SAS_SIG = re.compile(r"(?<![A-Za-z0-9_])(sig=)[^&\s\"'<>]+")
_STRIPE = re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{10,}")


class _RedactQuery(logging.Filter):
    """httpx 는 요청 URL 을 INFO 로 남긴다. 서명 URL(?sig=…)의 쿼리가 로그에 남지 않게 URL 인자의 쿼리를 지운다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(a.copy_with(query=None) if isinstance(a, httpx.URL) and a.query else a for a in record.args)
        return True


def _install_log_redaction() -> None:
    lg = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactQuery) for f in lg.filters):
        lg.addFilter(_RedactQuery())


_install_log_redaction()


def log_host_allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443) or not host:
        return False
    return host.endswith(_HOST_SUFFIX) or bool(_AZURE_RESULTS.fullmatch(host))


def split_lines(data: bytes) -> list[str]:
    text = data.decode("utf-8", "replace").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def mask_private_keys(lines: list[str]) -> list[str]:
    """개인키 블록(BEGIN~END)을 줄마다 가린다. 창으로 나누기 전에 로그 전체에 적용해야 창 경계에 걸친 블록도 새지 않는다."""
    out: list[str] = []
    inside = False
    for line in lines:
        if inside or _PEM_BEGIN.search(line):
            out.append(MASK)
            inside = not _PEM_END.search(line)
        else:
            out.append(line)
    return out


def mask_lines(lines: list[str]) -> list[str]:
    """줄 번호를 유지한 채 비밀을 가린다. 개인키 블록은 로그 전체 단위(mask_private_keys)로 이미 가렸고, 나머지는 창 단위로 한 번에 scrub 한다."""
    out = [_STRIPE.sub(MASK, _SAS_SIG.sub(rf"\1{MASK}", line)) for line in mask_private_keys(lines)]
    masked = scrub("\n".join(out)).split("\n")
    if len(masked) == len(out):
        return masked
    # scrub 정규식이 줄을 가로질러 줄 수가 달라진 드문 경우: 줄 단위로 다시 가린다(느리지만 번호는 정확하다).
    return [scrub(line) for line in out]


def view_log_window(lines: list[str], job_id: int, from_line: int, count: int) -> LogWindow:
    total = len(lines)
    start = from_line - 1
    chunk: list[str] = []
    chars = 0
    truncated = False
    for line in lines[start:start + count] if start < total else []:
        if len(line) > MAX_LINE:
            line, truncated = line[:MAX_LINE] + "…", True
        if chunk and chars + len(line) + 1 > WINDOW_CHARS:
            truncated = True
            break
        chunk.append(line)
        chars += len(line) + 1
    nxt = start + len(chunk) + 1
    return LogWindow(job_id=job_id, from_line=from_line, lines=mask_lines(chunk) if chunk else [], total_lines=total,
                     next_from_line=nxt if chunk and nxt <= total else None, truncated=truncated, masked=True)


class LogFetcher:
    def __init__(self, client: GithubClient, *, transport: httpx.AsyncBaseTransport | None = None,
                 clock=time.monotonic, budget: float = LOG_BUDGET) -> None:
        self._client = client
        self._budget = budget
        self._transport = transport
        self._cache = TTLCache(max_entries=8, max_bytes=24 * 1024 * 1024, clock=clock)
        self._dl: httpx.AsyncClient | None = None
        client.registry.on_removed(lambda login: self._cache.purge_login(login))

    def _http(self) -> httpx.AsyncClient:
        if self._dl is None:
            # 인증 헤더·기본 base_url 이 없는 별도 클라이언트: 서명 URL 호스트에 토큰이 갈 길이 없다.
            self._dl = httpx.AsyncClient(
                transport=self._transport, follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT},
                timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0))
        return self._dl

    @property
    def budget(self) -> float:
        return self._budget

    async def aclose(self) -> None:
        if self._dl is not None:
            await self._dl.aclose()
            self._dl = None

    async def lines(self, login: str, owner: str, repo: str, job_id: int) -> tuple[str, list[str]]:
        loop = asyncio.get_running_loop()
        scoped = current_deadline.get()  # 라우트 마감이 있으면 그것을, 없으면(직접 호출) 이 호출의 예산을 쓴다
        deadline = scoped if scoped is not None else loop.time() + self._budget
        info = await under_deadline(self._client.registry.require(login))
        key = (info.login, owner, repo, job_id)
        entry, fresh = self._cache.get(key)
        if entry is not None and fresh:
            return info.login, entry.payload
        up = await self._client.get(info.login, "job_logs", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                                    statuses=(302,), budget=self._budget, max_bytes=MAX_LOG_BYTES, ttl=0)
        if up.status == 302:
            location = up.location or ""
            if not log_host_allowed(location):
                log.warning("github logs redirect host not allowed: %s", (urlsplit(location).hostname or "?")[:80])
                raise GithubError("gh_logs_unavailable", reason="redirect_host_not_allowed")
            remaining = deadline - loop.time()
            if remaining <= 0.05:
                raise GithubError("gh_timeout")  # 첫 호출이 예산을 거의 다 썼다
            body = await self._download(location, remaining)
        else:
            body = up.body
        lines = mask_private_keys(split_lines(body))
        self._cache.put(key, lines, size=len(body) + 256, ttl=LOG_TTL, login=info.login)
        return info.login, lines

    async def _download(self, url: str, budget: float) -> bytes:
        try:
            async with asyncio.timeout(budget):
                async with self._http().stream("GET", url, headers={"Accept": "text/plain"}) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        raise GithubError("gh_logs_unavailable", reason="redirect_chain")
                    if r.status_code == 410:
                        raise GithubError("gh_gone", "로그가 만료되어 더 이상 받을 수 없습니다.")
                    if r.status_code != 200:
                        raise GithubError("gh_logs_unavailable", reason=f"download_status_{r.status_code}")
                    length = r.headers.get("content-length", "")
                    if length.isdigit() and int(length) > MAX_LOG_BYTES:
                        raise GithubError("gh_too_large")
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > MAX_LOG_BYTES:
                            raise GithubError("gh_too_large")
                    return bytes(buf)
        except TimeoutError as e:
            raise GithubError("gh_timeout") from e
        except httpx.HTTPError as e:
            raise GithubError("gh_upstream_error", "로그 다운로드에 실패했습니다.") from e
