"""Shared helpers for collector fetch/pipeline tests: a local fixture web server and a loopback-only NetGuard.

No test here touches the public network. Hostnames like docs.test resolve through a fake resolver to 127.0.0.1, and the
guard is told that 127.0.0.0/8 is permitted for these tests only. The default NetGuard (used by production code and by
the SSRF tests) still rejects loopback.
"""
from __future__ import annotations

import ipaddress
import json
import os
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from ttakkari.collector.netguard import NetGuard

LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"),)
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def fake_resolver(mapping: dict[str, list[str]], calls: list[str] | None = None):
    async def resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append(host)
        if host not in mapping:
            raise OSError(f"no such host {host}")
        return list(mapping[host])

    return resolve


def loopback_guard(*hosts: str, calls: list[str] | None = None) -> NetGuard:
    return NetGuard(resolver=fake_resolver({h: ["127.0.0.1"] for h in hosts}, calls), allow_networks=LOOPBACK)


class FixtureServer:
    """aiohttp server with a mutable route table. `hits` records (method, path, headers) for every request."""

    def __init__(self, ssl_context=None) -> None:
        self.ssl_context = ssl_context
        self.routes: dict[str, Handler] = {}
        self.hits: list[tuple[str, str, dict[str, str]]] = []
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def __aenter__(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=self.ssl_context)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._runner is not None
        await self._runner.cleanup()

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        self.hits.append((request.method, request.path_qs, dict(request.headers)))
        handler = self.routes.get(request.path)
        if handler is None:
            return web.Response(status=404, text="not found")
        return await handler(request)

    def url(self, path: str, host: str = "docs.test") -> str:
        return f"{'https' if self.ssl_context else 'http'}://{host}:{self.port}{path}"

    def page(self, path: str, body: str | bytes, content_type: str = "text/html", **headers: str) -> None:
        data = body.encode() if isinstance(body, str) else body

        async def handler(request: web.Request) -> web.StreamResponse:
            return web.Response(body=data, content_type=content_type, charset="utf-8" if content_type.startswith("text/") else None,
                                headers=headers)

        self.routes[path] = handler

    def count(self, path: str) -> int:
        return sum(1 for _, p, _ in self.hits if p.split("?")[0] == path)


def article_html(title: str, paragraphs: list[str], *, published: str | None = None, extra_head: str = "") -> str:
    meta = f'<meta property="article:published_time" content="{published}"/>' if published else ""
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        f"<html><head><title>{title}</title>{meta}{extra_head}</head><body>"
        f"<nav><a href='/'>home</a> <a href='/about'>about</a></nav><article><h1>{title}</h1>{body}</article>"
        "<footer>copyright footer links</footer></body></html>"
    )


LOREM_KO = (
    "오늘 서울의 하늘은 맑았고 바람은 약하게 불었다. 시민들은 한강 공원에서 산책을 즐겼으며 자전거를 타는 사람도 많았다. "
    "기상청은 주말까지 비슷한 날씨가 이어질 것이라고 밝혔다. 다만 일교차가 커서 외출할 때 겉옷을 챙기는 것이 좋다."
)
LOREM_EN = (
    "The committee published its quarterly report on infrastructure spending this morning, noting that road maintenance "
    "costs rose faster than expected. Officials said the increase reflects higher material prices and a backlog of repairs "
    "that had been postponed during the previous two years."
)


_EN_SUBJECTS = ["the council", "a regional bank", "the transport agency", "local researchers", "the city library", "an energy firm",
                "the education ministry", "a hospital network", "the water authority", "two universities"]
_EN_VERBS = ["announced", "reviewed", "postponed", "approved", "criticized", "estimated", "reported", "extended", "questioned", "outlined"]
_EN_OBJECTS = ["a new maintenance schedule", "the quarterly spending figures", "a pilot program for rural clinics",
               "revised safety guidelines", "an updated budget forecast", "the results of a public survey",
               "a plan to modernize older buildings", "stricter reporting rules", "an independent audit of contracts"]
_KO_SUBJECTS = ["서울시", "교육부", "한국은행", "연구진", "국립도서관", "에너지 기업", "대학 병원", "수자원공사", "지역 주민 대표", "교통공사"]
_KO_OBJECTS = ["새로운 정비 계획", "분기별 지출 내역", "농촌 지역 진료소 시범 사업", "개정된 안전 지침", "수정된 예산 전망",
               "시민 설문조사 결과", "노후 건물 현대화 방안", "강화된 보고 기준", "계약에 대한 독립 감사"]
_KO_VERBS = ["발표했다", "검토했다", "연기했다", "승인했다", "비판했다", "추정했다", "보고했다", "연장했다", "의문을 제기했다", "설명했다"]


def prose(lang: str, seed: int, sentences: int = 8) -> str:
    """Deterministic, non-repetitive filler text, so L0 repetition rules do not fire on test fixtures."""
    rng = random.Random(f"{lang}-{seed}")
    out = []
    for i in range(sentences):
        n, w = rng.randint(11, 989), rng.randint(2, 51)
        if lang == "ko":
            out.append(f"{rng.choice(_KO_SUBJECTS)}는 {w}번째 주에 {rng.choice(_KO_OBJECTS)}을 {n}건 기준으로 {rng.choice(_KO_VERBS)}.")
        else:
            out.append(f"On day {n}, {rng.choice(_EN_SUBJECTS)} {rng.choice(_EN_VERBS)} {rng.choice(_EN_OBJECTS)} covering {w} districts in sector {i + 1}.")
    return " ".join(out)


def recorder() -> Callable[[str, dict], None]:
    """Append test outcomes to $COLLECTOR_REPORT_FILE (JSON lines) when set, so a run can emit a report artifact."""
    path = os.environ.get("COLLECTOR_REPORT_FILE")

    def record(group: str, row: dict) -> None:
        if not path:
            return
        with Path(path).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"group": group, "at": datetime.now(UTC).isoformat(), **row}, ensure_ascii=False) + "\n")

    return record
