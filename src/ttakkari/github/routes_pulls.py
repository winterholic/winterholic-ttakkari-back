"""PR 목록·상세·파일별 patch."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from ttakkari.github import targets, views
from ttakkari.github.errors import GithubError
from ttakkari.github.routes_common import (
    PR_FILES_LIMIT,
    Account,
    Gh,
    PageQ,
    PerPageQ,
    envelope,
    ident,
    page_meta,
)
from ttakkari.github.schemas import Envelope, PullDetail, PullFile, PullSummary

router = APIRouter()
PR_FILES_MAX_BYTES = 4 * 1024 * 1024
State = Annotated[Literal["open", "closed", "all"], Query()]
Sort = Annotated[Literal["created", "updated"], Query()]
Direction = Annotated[Literal["asc", "desc"] | None, Query()]


def _list(raw) -> list:
    if not isinstance(raw, list):
        raise GithubError("gh_schema_changed")
    return raw


@router.get("/repos/{owner}/{repo}/pulls", response_model=Envelope[list[PullSummary]])
async def pulls(owner: str, repo: str, svc: Gh, account: Account, state: State = "open", sort: Sort = "created",
                direction: Direction = None, page: PageQ = 1, per_page: PerPageQ = 30) -> dict:
    o, r = targets.owner(owner), targets.repo(repo)
    login = await ident(svc, account)
    up = await svc.client.get(login, "pulls", f"/repos/{o}/{r}/pulls", ttl=30, params={
        "state": state, "sort": sort, "direction": direction, "page": page, "per_page": per_page})
    return envelope(up, [views.view_pull_summary(x) for x in _list(up.json())], login, page=page_meta(up, page, per_page))


@router.get("/repos/{owner}/{repo}/pulls/{number}", response_model=Envelope[PullDetail])
async def pull(owner: str, repo: str, number: str, svc: Gh, account: Account) -> dict:
    o, r, n = targets.owner(owner), targets.repo(repo), targets.number(number)
    login = await ident(svc, account)
    # mergeable 은 GitHub 가 비동기로 계산하므로 짧게만 캐시해 새로고침이 값을 보게 한다.
    up = await svc.client.get(login, "pull", f"/repos/{o}/{r}/pulls/{n}", ttl=15)
    return envelope(up, views.view_pull_detail(up.json()), login)


@router.get("/repos/{owner}/{repo}/pulls/{number}/files", response_model=Envelope[list[PullFile]])
async def pull_files(owner: str, repo: str, number: str, svc: Gh, account: Account, page: PageQ = 1,
                     per_page: PerPageQ = 30) -> dict:
    o, r, n = targets.owner(owner), targets.repo(repo), targets.number(number)
    login = await ident(svc, account)
    up = await svc.client.get(login, "pull_files", f"/repos/{o}/{r}/pulls/{n}/files", ttl=30,
                              max_bytes=PR_FILES_MAX_BYTES, params={"page": page, "per_page": per_page})
    return envelope(up, [views.view_pull_file(x) for x in _list(up.json())], login,
                    page=page_meta(up, page, per_page, limit_hit=page * per_page >= PR_FILES_LIMIT))
