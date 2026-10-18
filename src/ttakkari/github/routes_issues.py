"""이슈 목록·상세·댓글(조회만)."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from ttakkari.github import targets, views
from ttakkari.github.errors import GithubError
from ttakkari.github.routes_common import (
    Account,
    Gh,
    PageQ,
    PerPageQ,
    envelope,
    ident,
    page_meta,
)
from ttakkari.github.schemas import Comment, Envelope, IssueDetail, IssueSummary

router = APIRouter()
State = Annotated[Literal["open", "closed", "all"], Query()]
Sort = Annotated[Literal["created", "updated", "comments"], Query()]
Direction = Annotated[Literal["asc", "desc"] | None, Query()]
LabelsQ = Annotated[str | None, Query(max_length=600)]


def _list(raw) -> list:
    if not isinstance(raw, list):
        raise GithubError("gh_schema_changed")
    return raw


@router.get("/repos/{owner}/{repo}/issues", response_model=Envelope[list[IssueSummary]])
async def issues(owner: str, repo: str, svc: Gh, account: Account, state: State = "open", labels: LabelsQ = None,
                 sort: Sort = "created", direction: Direction = None, page: PageQ = 1, per_page: PerPageQ = 30) -> dict:
    o, r = targets.owner(owner), targets.repo(repo)
    lab = targets.labels(labels)
    login = await ident(svc, account)
    up = await svc.client.get(login, "issues", f"/repos/{o}/{r}/issues", ttl=30, params={
        "state": state, "labels": lab, "sort": sort, "direction": direction, "page": page, "per_page": per_page})
    # PR 은 걸러지므로 페이지당 개수가 줄 수 있다. 더 보기 여부는 개수가 아니라 상류 Link 로 정한다.
    return envelope(up, views.view_issue_list(_list(up.json())), login, page=page_meta(up, page, per_page))


@router.get("/repos/{owner}/{repo}/issues/{number}", response_model=Envelope[IssueDetail])
async def issue(owner: str, repo: str, number: str, svc: Gh, account: Account) -> dict:
    o, r, n = targets.owner(owner), targets.repo(repo), targets.number(number)
    login = await ident(svc, account)
    up = await svc.client.get(login, "issue", f"/repos/{o}/{r}/issues/{n}", ttl=30)
    return envelope(up, views.view_issue_detail(up.json()), login)


@router.get("/repos/{owner}/{repo}/issues/{number}/comments", response_model=Envelope[list[Comment]])
async def comments(owner: str, repo: str, number: str, svc: Gh, account: Account, page: PageQ = 1,
                   per_page: PerPageQ = 30) -> dict:
    o, r, n = targets.owner(owner), targets.repo(repo), targets.number(number)
    login = await ident(svc, account)
    up = await svc.client.get(login, "issue_comments", f"/repos/{o}/{r}/issues/{n}/comments", ttl=30,
                              params={"page": page, "per_page": per_page})
    return envelope(up, [views.view_comment(c) for c in _list(up.json())], login, page=page_meta(up, page, per_page))
