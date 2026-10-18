"""라우트 공통: 서비스 의존성, 쿼리 형식, 응답 봉투."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Query

from ttakkari.github.client import Upstream, current_deadline, under_deadline
from ttakkari.github.schemas import Page
from ttakkari.github.service import GithubService, get_service


async def _scope(svc: GithubService, budget: float) -> AsyncIterator[GithubService]:
    """요청 하나의 마감을 연다. 이 요청 안의 계정 확인·모든 중첩 호출·재시도·백오프가 이 한 마감을 나눠 쓴다(ADR 0042 §6, V04).
    async 의존성이라 엔드포인트와 같은 컨텍스트에서 실행되고, 끝나면 되돌려 같은 태스크의 다음 호출에 새지 않는다."""
    token = current_deadline.set(asyncio.get_running_loop().time() + budget)
    try:
        yield svc
    finally:
        try:
            current_deadline.reset(token)
        except ValueError:  # 다른 컨텍스트에서 닫히는 비정상 경로: 최소한 마감을 남기지 않는다
            current_deadline.set(None)


async def get_gh(svc: Annotated[GithubService, Depends(get_service)]) -> AsyncIterator[GithubService]:
    async for s in _scope(svc, svc.client.budget):
        yield s


async def get_gh_logs(svc: Annotated[GithubService, Depends(get_service)]) -> AsyncIterator[GithubService]:
    """Actions 로그만 예산이 더 크다(기본 20초). 그래도 라우트 전체가 한 마감이다."""
    async for s in _scope(svc, svc.logs.budget):
        yield s


Gh = Annotated[GithubService, Depends(get_gh)]
GhLogs = Annotated[GithubService, Depends(get_gh_logs)]
# 길이만 여기서 막고 형식·존재 검증은 AccountRegistry.require 가 한다(오류 코드를 invalid_target/account_unavailable 로 통일).
Account = Annotated[str, Query(min_length=1, max_length=64, description="gh에 로그인된 GitHub 계정 login")]
OptAccount = Annotated[str | None, Query(max_length=64)]
PageQ = Annotated[int, Query(ge=1, le=200)]
PerPageQ = Annotated[int, Query(ge=1, le=50)]
PR_FILES_LIMIT = 3000  # GitHub 이 주는 PR 파일 수 상한


async def ident(svc: GithubService, account: str) -> str:
    """검증된 계정의 정규화 login."""
    return (await under_deadline(svc.registry.require(account))).login


def page_meta(up: Upstream, page: int, per_page: int, *, total: int | None = None, limit_hit: bool = False) -> Page:
    return Page(page=page, per_page=per_page, has_more=up.next_page is not None, total_count=total, limit_hit=limit_hit)


def envelope(up: Upstream, data: Any, login: str, *, page: Page | None = None) -> dict[str, Any]:
    return {"data": data, "page": page, "rate": up.rate, "fetched_at": up.fetched_at.isoformat(),
            "cached": up.cached, "stale": up.stale, "account": login}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
