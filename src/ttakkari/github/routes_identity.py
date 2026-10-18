"""계정·입력 해석·프로필·조직·저장소 목록."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from ttakkari.github import refs, targets, views
from ttakkari.github.client import under_deadline
from ttakkari.github.errors import GithubError
from ttakkari.github.routes_common import (
    Account,
    Gh,
    OptAccount,
    PageQ,
    PerPageQ,
    envelope,
    ident,
    now_iso,
    page_meta,
)
from ttakkari.github.schemas import (
    AccountOut,
    Envelope,
    OrgRef,
    Profile,
    RepoSummary,
    ResolveOut,
)

router = APIRouter()
RepoSort = Annotated[Literal["created", "updated", "pushed", "full_name"], Query()]


@router.get("/accounts")
async def accounts(svc: Gh, probe: bool = False) -> dict:
    """gh 계정 메타데이터. probe=true 는 토큰을 받을 수 있는지만 확인한다(값은 어디에도 없다)."""
    try:
        rows = await under_deadline(svc.registry.list_accounts())
        available = True
    except GithubError as e:
        if e.code != "no_account":
            raise
        rows, available = [], False
    out = []
    for a in rows:
        token_ok = await under_deadline(svc.registry.probe(a.login)) if probe and a.state == "success" else None
        out.append(AccountOut(login=a.login, host=a.host, active=a.active, scopes=a.scopes, state=a.state,
                              token_available=token_ok, rate=svc.client.rate_of(a.login)))
    return {"accounts": out, "gh_available": available, "fetched_at": now_iso()}


@router.get("/resolve", response_model=ResolveOut)
async def resolve(svc: Gh, q: Annotated[str, Query(max_length=4096)], account: OptAccount = None) -> ResolveOut:
    out = targets.parse(q)
    if not out.needs_ref_resolution or account is None:
        return out
    login = await ident(svc, account)
    try:
        return await refs.resolve_ref(svc, login, out)
    except GithubError as e:
        if e.code == "ref_not_found":
            # 저장소 자체가 안 보이면 ref 오류가 아니라 권한/없음 오류가 맞다.
            await svc.client.get(login, "repo", f"/repos/{out.owner}/{out.repo}", ttl=60)
        raise


@router.get("/me", response_model=Envelope[Profile])
async def me(svc: Gh, account: Account) -> dict:
    login = await ident(svc, account)
    up = await svc.client.get(login, "user", "/user", ttl=60)
    raw = up.json()
    viewer = raw.get("login") if isinstance(raw, dict) else None
    return envelope(up, views.view_profile(raw, viewer_login=viewer if isinstance(viewer, str) else login), login)


@router.get("/me/orgs", response_model=Envelope[list[OrgRef]])
async def my_orgs(svc: Gh, account: Account, page: PageQ = 1, per_page: PerPageQ = 30) -> dict:
    login = await ident(svc, account)
    up = await svc.client.get(login, "user_orgs", "/user/orgs", params={"page": page, "per_page": per_page}, ttl=60)
    data = up.json()
    if not isinstance(data, list):  # 빈 목록으로 위장하지 않는다(조직이 없는 것과 상류 형식 변경은 다르다)
        raise GithubError("gh_schema_changed")
    return envelope(up, [views.view_org_ref(o) for o in data], login, page=page_meta(up, page, per_page))


def _repos(up_json) -> list[RepoSummary]:
    if not isinstance(up_json, list):
        raise GithubError("gh_schema_changed")
    return [views.view_repo(r) for r in up_json]


@router.get("/me/repos", response_model=Envelope[list[RepoSummary]])
async def my_repos(svc: Gh, account: Account, sort: RepoSort = "updated", page: PageQ = 1, per_page: PerPageQ = 30) -> dict:
    login = await ident(svc, account)
    up = await svc.client.get(login, "user_repos", "/user/repos", ttl=30, params={
        "sort": sort, "affiliation": "owner,collaborator,organization_member", "page": page, "per_page": per_page})
    return envelope(up, _repos(up.json()), login, page=page_meta(up, page, per_page))


async def _owner(svc, login: str, owner: str):
    up = await svc.client.get(login, "users", f"/users/{owner}", ttl=60)
    return up, up.json()


@router.get("/owners/{owner}", response_model=Envelope[Profile])
async def owner_overview(owner: str, svc: Gh, account: Account) -> dict:
    name = targets.owner(owner)
    login = await ident(svc, account)
    up, raw = await _owner(svc, login, name)
    org = None
    if isinstance(raw, dict) and raw.get("type") == "Organization":
        try:
            org = (await svc.client.get(login, "orgs", f"/orgs/{name}", ttl=60)).json()
        except GithubError as e:
            if e.code not in ("gh_not_found", "gh_forbidden"):
                raise
    return envelope(up, views.view_profile(raw, viewer_login=login, org=org), login)


@router.get("/owners/{owner}/repos", response_model=Envelope[list[RepoSummary]])
async def owner_repos(owner: str, svc: Gh, account: Account, sort: RepoSort = "updated", page: PageQ = 1,
                      per_page: PerPageQ = 30) -> dict:
    name = targets.owner(owner)
    login = await ident(svc, account)
    _, raw = await _owner(svc, login, name)
    org = isinstance(raw, dict) and raw.get("type") == "Organization"
    up = await svc.client.get(login, "org_repos" if org else "users_repos", f"/{'orgs' if org else 'users'}/{name}/repos",
                              ttl=30, params={"sort": sort, "page": page, "per_page": per_page})
    return envelope(up, _repos(up.json()), login, page=page_meta(up, page, per_page))
