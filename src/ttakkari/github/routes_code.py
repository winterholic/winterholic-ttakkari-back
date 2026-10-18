"""저장소 요약·브랜치·디렉터리/파일·README."""
from __future__ import annotations

from typing import Annotated

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
from ttakkari.github.schemas import Branch, ContentsOut, Envelope, RepoSummary

router = APIRouter()
OBJECT = "application/vnd.github.object+json"
RAW = "application/vnd.github.raw+json"
RAW_LIMIT_BYTES = 100 * 1024 * 1024  # 이 크기를 넘으면 contents API 가 지원하지 않는다
RefQ = Annotated[str | None, Query(max_length=255)]
PathQ = Annotated[str, Query(max_length=1100)]


def _ttl(ref: str | None) -> int:
    return 600 if ref and targets.SHA_RE.fullmatch(ref) else 30  # 전체 SHA 로 고정된 내용은 바뀌지 않는다


@router.get("/repos/{owner}/{repo}", response_model=Envelope[RepoSummary])
async def repo_summary(owner: str, repo: str, svc: Gh, account: Account) -> dict:
    o, r = targets.owner(owner), targets.repo(repo)
    login = await ident(svc, account)
    up = await svc.client.get(login, "repo", f"/repos/{o}/{r}", ttl=60)
    return envelope(up, views.view_repo(up.json()), login)


@router.get("/repos/{owner}/{repo}/branches", response_model=Envelope[list[Branch]])
async def branches(owner: str, repo: str, svc: Gh, account: Account, page: PageQ = 1, per_page: PerPageQ = 30) -> dict:
    o, r = targets.owner(owner), targets.repo(repo)
    login = await ident(svc, account)
    summary = views.view_repo((await svc.client.get(login, "repo", f"/repos/{o}/{r}", ttl=60)).json())
    up = await svc.client.get(login, "branches", f"/repos/{o}/{r}/branches", ttl=30,
                              params={"page": page, "per_page": per_page})
    return envelope(up, views.view_branches(up.json(), default_branch=summary.default_branch), login,
                    page=page_meta(up, page, per_page))


@router.get("/repos/{owner}/{repo}/contents", response_model=Envelope[ContentsOut])
async def contents(owner: str, repo: str, svc: Gh, account: Account, ref: RefQ = None, path: PathQ = "") -> dict:
    o, r = targets.owner(owner), targets.repo(repo)
    p = targets.path(path)
    rf = targets.ref(ref) if ref else None
    login = await ident(svc, account)
    api_path = f"/repos/{o}/{r}/contents" + (f"/{targets.quote_path(p)}" if p else "")
    up = await svc.client.get(login, "contents", api_path, params={"ref": rf}, accept=OBJECT, ttl=_ttl(rf))
    data = views.view_contents(up.json(), path=p, ref=rf)
    f = data.file
    if f is not None and f.too_large and f.size is not None and f.size <= RAW_LIMIT_BYTES:
        # 표시 상한을 넘는 파일은 앞부분만 raw 로 받아 truncated 로 표시한다. 실패하면 본문 없이 too_large 만 남긴다.
        try:
            raw = await svc.client.get(login, "contents", api_path, params={"ref": rf}, accept=RAW,
                                       max_bytes=views.FILE_TEXT_LIMIT, truncate=True, ttl=_ttl(rf))
            data = data.model_copy(update={"file": views.file_from_bytes(f, raw.body, truncated=raw.truncated)})
        except GithubError as e:
            if e.code not in ("gh_forbidden", "gh_unprocessable", "gh_too_large"):
                raise
    return envelope(up, data, login)


@router.get("/repos/{owner}/{repo}/readme", response_model=Envelope[ContentsOut])
async def readme(owner: str, repo: str, svc: Gh, account: Account, ref: RefQ = None) -> dict:
    o, r = targets.owner(owner), targets.repo(repo)
    rf = targets.ref(ref) if ref else None
    login = await ident(svc, account)
    try:
        up = await svc.client.get(login, "readme", f"/repos/{o}/{r}/readme", params={"ref": rf}, ttl=_ttl(rf))
    except GithubError as e:
        if e.code == "gh_not_found":
            # 저장소가 보이는데 README 만 없는 경우와, 저장소 자체가 안 보이는 경우를 구분한다.
            await svc.client.get(login, "repo", f"/repos/{o}/{r}", ttl=60)
            raise GithubError("readme_not_found") from e
        raise
    raw = up.json()
    path = raw.get("path") if isinstance(raw, dict) and isinstance(raw.get("path"), str) else "README"
    return envelope(up, views.view_contents(raw, path=path, ref=rf), login)
