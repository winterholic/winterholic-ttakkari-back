"""`tree|blob/<ref>/<path>` 의 ref 를 실제 저장소 기준으로 푼다(ADR 0042 §7, GH03). 입력은 fetch 하지 않고 검증된 조각만 고정 경로에 끼운다.

순서: HEAD(기본 브랜치) → 브랜치·태그 → 약식 SHA(7~39자, 커밋 조회로 확인). 40자 SHA 는 호출 없이 인정한다(순서 앞에서 처리).
브랜치·태그는 `git/matching-refs/<heads|tags>/<첫 마디>`(쪽당 100개)를 Link next 로 최대 MAX_LIST_PAGES 쪽까지 읽고, 일치가 나오거나 목록이 끝나면 멈춘다.
그래도 목록이 남았으면(dependabot/… 수백 개) 일부만 본 것이므로 "없음"을 결론내지 않고 `git/ref/<heads|tags>/<후보>` 정확 조회로 확인한다.
같은 이름공간에서는 ref 하나가 다른 ref 의 접두 경로일 수 없으므로(git 은 `a` 와 `a/b` 를 함께 둘 수 없다) 짧은 후보부터 조회해 처음 맞는 것이 유일한 답이다.
정확 조회가 확인할 수 있는 마디 수에는 상한(MAX_PROBE_SEGMENTS)이 있고, 넘겨서 확인하지 못했을 때는 ref_not_found 가 아니라 한계 오류로 알린다.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ttakkari.github import targets, views
from ttakkari.github.errors import GithubError
from ttakkari.github.schemas import ResolveOut
from ttakkari.github.service import GithubService

MAX_LIST_PAGES = 3
MAX_PROBE_SEGMENTS = 6
PER_PAGE = 100
SHA_ACCEPT = "application/vnd.github.sha"
_KINDS = ("heads", "tags")


def _limit_error() -> GithubError:
    return GithubError(
        "gh_unprocessable", "같은 접두사의 브랜치·태그가 너무 많고 이름이 깊어 ref 를 확인하지 못했습니다. 저장소 화면에서 브랜치를 직접 고르세요.",
        reason="ref_resolution_limit")


async def _listing(svc: GithubService, login: str, o: str, r: str, kind: str, ref_path: str) -> tuple[tuple[str, str] | None, bool]:
    """(일치, 목록을 끝까지 봤는지). Link next 를 MAX_LIST_PAGES 쪽까지 따라가고, 일치가 나오면 거기서 멈춘다.
    접두사에 맞는 ref 가 없을 때 404 를 주는 경우는 빈 완전 목록이다."""
    first = ref_path.split("/", 1)[0]
    path = f"/repos/{o}/{r}/git/matching-refs/{kind}/{quote(first, safe='')}"
    prefix = f"refs/{kind}/"
    names: list[str] = []
    page = 1
    while True:
        params = {"per_page": PER_PAGE} | ({"page": page} if page > 1 else {})
        try:
            up = await svc.client.get(login, "matching_refs", path, params=params, ttl=60)
        except GithubError as e:
            if e.code == "gh_not_found":
                return targets.match_ref(ref_path, names), True
            raise
        data = up.json()
        if not isinstance(data, list):
            raise GithubError("gh_schema_changed")
        names += [d["ref"][len(prefix):] for d in data if isinstance(d, dict) and isinstance(d.get("ref"), str)
                  and d["ref"].startswith(prefix)]
        hit = targets.match_ref(ref_path, names)
        if hit:
            return hit, True  # 같은 이름공간에서 접두 경로인 ref 는 하나뿐이라 더 읽을 필요가 없다
        nxt = up.next_page
        if nxt is None:
            return None, len(data) < PER_PAGE  # Link 를 읽지 못해도 한 쪽이 꽉 찼으면 더 있을 수 있다고 본다
        if page >= MAX_LIST_PAGES or nxt <= page:
            return None, False
        page = nxt


def _is_ref(data: Any, full: str) -> bool:
    if isinstance(data, dict):
        return data.get("ref") == full
    return isinstance(data, list) and any(isinstance(d, dict) and d.get("ref") == full for d in data)  # 접두사 배열은 정확히 같은 이름만


async def _probe(svc: GithubService, login: str, o: str, r: str, kind: str, segs: list[str]) -> tuple[tuple[str, str] | None, bool]:
    """(일치, 상한 때문에 다 확인하지 못했는지)."""
    for n in range(1, min(len(segs), MAX_PROBE_SEGMENTS) + 1):
        name = "/".join(segs[:n])
        try:
            targets.ref(name)
        except GithubError:
            return None, False  # 이 마디를 품은 더 긴 후보도 모두 ref 이름이 될 수 없다
        path = f"/repos/{o}/{r}/git/ref/{kind}/" + "/".join(quote(s, safe="") for s in segs[:n])
        try:
            up = await svc.client.get(login, "git_ref", path, ttl=60)
        except GithubError as e:
            if e.code == "gh_not_found":
                continue
            raise
        if _is_ref(up.json(), f"refs/{kind}/{name}"):
            return (name, "/".join(segs[n:])), False
    return None, len(segs) > MAX_PROBE_SEGMENTS


async def _short_sha(svc: GithubService, login: str, o: str, r: str, short: str) -> str | None:
    """약식 SHA 를 실제 커밋으로 확인해 전체 SHA 로 넓힌다. 없으면 None(임의의 16진수는 받지 않는다)."""
    try:
        up = await svc.client.get(login, "commit", f"/repos/{o}/{r}/commits/{short}", accept=SHA_ACCEPT, ttl=600)
    except GithubError as e:
        if e.code in ("gh_not_found", "gh_unprocessable"):
            return None
        raise
    full = up.body.decode("utf-8", "replace").strip().lower()
    if not targets.SHA_RE.fullmatch(full) or not full.startswith(short.lower()):
        raise GithubError("gh_schema_changed")
    return full


async def resolve_ref(svc: GithubService, login: str, out: ResolveOut) -> ResolveOut:
    assert out.owner is not None and out.repo is not None and out.ref_path is not None
    o, r = out.owner, out.repo
    segs = out.ref_path.split("/")
    first, rest = segs[0], "/".join(segs[1:])
    if targets.SHA_RE.fullmatch(first):
        return targets.with_ref(out, [])
    if first == "HEAD":  # GitHub 주소의 기본 브랜치 별칭
        summary = views.view_repo((await svc.client.get(login, "repo", f"/repos/{o}/{r}", ttl=60)).json())
        if not summary.default_branch:
            raise GithubError("gh_empty_repository")
        return targets.with_ref_at(out, summary.default_branch, rest)
    targets.ref(first)
    matches: list[tuple[str, str]] = []
    limited = False
    for kind in _KINDS:
        hit, complete = await _listing(svc, login, o, r, kind, out.ref_path)
        if hit is None and not complete:
            hit, cut = await _probe(svc, login, o, r, kind, segs)
            limited = limited or cut
        if hit:
            matches.append(hit)
    if matches:
        return targets.with_ref_at(out, *max(matches, key=lambda m: len(m[0])))
    if targets.SHORT_SHA_RE.fullmatch(first):
        full = await _short_sha(svc, login, o, r, first)
        if full:
            return targets.with_ref_at(out, full, rest)
    if limited:
        raise _limit_error()
    raise GithubError("ref_not_found")
