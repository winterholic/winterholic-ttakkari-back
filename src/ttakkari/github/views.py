"""상류 GitHub JSON → 응답 모델. 필드를 하나씩 골라 담는 화이트리스트라 상류가 필드를 늘려도 새지 않고,
필수 필드가 사라지거나 형이 바뀌면 gh_schema_changed 로 알린다(내 오류와 구분, dev-api-integration #5)."""
from __future__ import annotations

import base64
import binascii
from typing import Any

from ttakkari.github.errors import GithubError
from ttakkari.github.schemas import (
    Branch,
    BranchRef,
    Comment,
    ContentEntry,
    ContentsOut,
    FileOut,
    IssueDetail,
    IssueSummary,
    Job,
    Label,
    OrgRef,
    Profile,
    PullDetail,
    PullFile,
    PullSummary,
    RepoPermissions,
    RepoSummary,
    RunDetail,
    RunSummary,
    Step,
    SubmoduleOut,
    UserRef,
)

FILE_TEXT_LIMIT = 1024 * 1024  # 파일 본문 표시 상한
DIR_ENTRY_LIMIT = 1000  # contents API 디렉터리 상한
BODY_LIMIT = 64 * 1024  # 이슈·PR·댓글 본문 상한(글자)
PATCH_LIMIT = 256 * 1024  # 파일별 patch 상한(글자)
BINARY_SNIFF = 8192


def _schema(what: str) -> GithubError:
    return GithubError("gh_schema_changed", f"GitHub 응답 형식이 예상과 달라 표시할 수 없습니다({what}).")


def _dict(raw: Any, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _schema(what)
    return raw


def _s(d: dict[str, Any], key: str) -> str | None:
    v = d.get(key)
    return v if isinstance(v, str) and v != "" else None


def _i(d: dict[str, Any], key: str) -> int | None:
    v = d.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _b(d: dict[str, Any], key: str) -> bool:
    return d.get(key) is True


def _req_s(d: dict[str, Any], key: str, what: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or v == "":
        raise _schema(f"{what}.{key}")
    return v


def _req_i(d: dict[str, Any], key: str, what: str) -> int:
    v = d.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise _schema(f"{what}.{key}")
    return v


def _cap(text: str | None, limit: int, ellipsis: bool = True) -> tuple[str | None, bool]:
    if text is None or len(text) <= limit:
        return text, False
    return text[:limit] + ("…" if ellipsis else ""), True


def _gh_url(v: Any) -> str | None:
    return v if isinstance(v, str) and v.startswith("https://github.com/") else None


def _avatar(v: Any) -> str | None:
    """아바타는 GitHub CDN 만 통과시킨다(브라우저가 임의 호스트를 열지 않게)."""
    if isinstance(v, str) and v.startswith("https://") and "/" in v[8:]:
        host = v[8:].split("/", 1)[0].lower()
        if host == "githubusercontent.com" or host.endswith(".githubusercontent.com"):
            return v
    return None


def _list(raw: Any, key: str | None = None) -> list[Any]:
    v = raw.get(key) if key is not None and isinstance(raw, dict) else raw
    return v if isinstance(v, list) else []


def user_ref(raw: Any) -> UserRef | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("login"), str):
        return None
    return UserRef(login=raw["login"], type=_s(raw, "type"), avatar_url=_avatar(raw.get("avatar_url")),
                   html_url=_gh_url(raw.get("html_url")))


def _users(raw: Any) -> list[UserRef]:
    return [u for u in (user_ref(x) for x in _list(raw)) if u is not None]


def _labels(raw: Any) -> list[Label]:
    out: list[Label] = []
    for x in _list(raw):
        if isinstance(x, dict) and isinstance(x.get("name"), str):
            out.append(Label(name=x["name"], color=_s(x, "color")))
    return out


# ---- 신원 ----

def view_profile(raw: Any, *, viewer_login: str | None, org: Any | None = None) -> Profile:
    d = _dict(raw, "user")
    login = _req_s(d, "login", "user")
    o = org if isinstance(org, dict) else {}
    return Profile(
        login=login, id=_i(d, "id"), type=_s(d, "type") or "User",
        name=_s(o, "name") or _s(d, "name"), bio=_s(d, "bio"), description=_s(o, "description"),
        company=_s(d, "company"), location=_s(d, "location"), blog=_s(d, "blog"),
        avatar_url=_avatar(d.get("avatar_url")), html_url=_gh_url(d.get("html_url")),
        public_repos=_i(o, "public_repos") if _i(o, "public_repos") is not None else _i(d, "public_repos"),
        total_private_repos=_i(d, "total_private_repos"),
        followers=_i(o, "followers") if _i(o, "followers") is not None else _i(d, "followers"),
        following=_i(d, "following"), created_at=_s(d, "created_at"),
        is_viewer=viewer_login is not None and login.lower() == viewer_login.lower(),
    )


def view_org_ref(raw: Any) -> OrgRef:
    d = _dict(raw, "org")
    return OrgRef(login=_req_s(d, "login", "org"), id=_i(d, "id"), description=_s(d, "description"),
                  avatar_url=_avatar(d.get("avatar_url")))


# ---- 저장소·코드 ----

def view_repo(raw: Any) -> RepoSummary:
    d = _dict(raw, "repo")
    lic = d.get("license")
    perms = d.get("permissions")
    return RepoSummary(
        id=_i(d, "id"), name=_req_s(d, "name", "repo"), full_name=_req_s(d, "full_name", "repo"),
        owner=user_ref(d.get("owner")), private=_b(d, "private"), visibility=_s(d, "visibility"),
        description=_s(d, "description"), fork=_b(d, "fork"), archived=_b(d, "archived"), disabled=_b(d, "disabled"),
        is_template=_b(d, "is_template"), language=_s(d, "language"), stargazers_count=_i(d, "stargazers_count"),
        forks_count=_i(d, "forks_count"), open_issues_count=_i(d, "open_issues_count"),
        default_branch=_s(d, "default_branch"), pushed_at=_s(d, "pushed_at"), updated_at=_s(d, "updated_at"),
        html_url=_gh_url(d.get("html_url")), homepage=_s(d, "homepage"),
        topics=[t for t in _list(d.get("topics")) if isinstance(t, str)],
        license=_s(lic, "name") if isinstance(lic, dict) else None, size_kb=_i(d, "size"),
        has_issues=d.get("has_issues") if isinstance(d.get("has_issues"), bool) else None,
        permissions=RepoPermissions(admin=_b(perms, "admin"), push=_b(perms, "push"), pull=_b(perms, "pull"))
        if isinstance(perms, dict) else None,
    )


def view_branches(raw: Any, *, default_branch: str | None) -> list[Branch]:
    out: list[Branch] = []
    for x in _list(raw):
        d = _dict(x, "branch")
        name = _req_s(d, "name", "branch")
        commit = d.get("commit")
        out.append(Branch(name=name, sha=_s(commit, "sha") if isinstance(commit, dict) else None,
                          protected=_b(d, "protected"), is_default=name == default_branch))
    return out


def _entry(raw: Any) -> ContentEntry:
    d = _dict(raw, "content")
    typ = _s(d, "type")
    return ContentEntry(
        name=_req_s(d, "name", "content"), path=_req_s(d, "path", "content"),
        type=typ if typ in ("file", "dir", "symlink", "submodule") else "other",  # type: ignore[arg-type]
        size=_i(d, "size"), sha=_s(d, "sha"), html_url=_gh_url(d.get("html_url")),
    )


def _dir(entries: list[Any], *, path: str, ref: str | None) -> ContentsOut:
    rows = sorted((_entry(e) for e in entries), key=lambda e: (e.type != "dir", e.name.lower(), e.name))
    return ContentsOut(kind="dir", path=path, ref=ref, entries=rows, entries_limit_hit=len(entries) >= DIR_ENTRY_LIMIT)


def _line_count(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def file_from_bytes(base: FileOut, data: bytes, *, truncated: bool) -> FileOut:
    """바이트에서 본문을 만든다. NUL 이 있거나 UTF-8 이 아니면 깨진 글자로 성공시키지 않고 binary 로 표시한다."""
    if b"\x00" in data[:BINARY_SNIFF]:
        return base.model_copy(update={"binary": True, "text": None, "line_count": None, "truncated": False})
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        # 상한에서 자른 경계가 한 글자 중간이면 그 글자만 버린다.
        if truncated and e.start >= len(data) - 3:
            text = data[: e.start].decode("utf-8", errors="strict")
        else:
            return base.model_copy(update={"binary": True, "text": None, "line_count": None, "truncated": False})
    return base.model_copy(update={"text": text, "line_count": _line_count(text), "truncated": truncated, "binary": False})


def view_contents(raw: Any, *, path: str, ref: str | None) -> ContentsOut:
    if isinstance(raw, list):
        return _dir(raw, path=path, ref=ref)
    d = _dict(raw, "contents")
    typ = d.get("type")
    if typ == "dir":
        return _dir(_list(d, "entries"), path=path, ref=ref)
    if typ == "symlink":
        return ContentsOut(kind="symlink", path=path, ref=ref, symlink_target=_s(d, "target"))
    if typ == "submodule":
        return ContentsOut(kind="submodule", path=path, ref=ref,
                           submodule=SubmoduleOut(git_url=_s(d, "submodule_git_url"), sha=_s(d, "sha")))
    if typ != "file":
        raise _schema("contents.type")
    size = _i(d, "size")
    base = FileOut(name=_req_s(d, "name", "contents"), path=_s(d, "path") or path, sha=_s(d, "sha"), size=size,
                   html_url=_gh_url(d.get("html_url")))
    content = d.get("content")
    too_large = (size is not None and size > FILE_TEXT_LIMIT) or d.get("encoding") != "base64" or not content
    if too_large and size == 0 and d.get("encoding") == "base64":
        too_large = False  # 빈 파일
    if too_large:
        return ContentsOut(kind="file", path=path, ref=ref, file=base.model_copy(update={"too_large": True}))
    try:
        data = base64.b64decode(content or "", validate=False)
    except (binascii.Error, ValueError) as e:
        raise _schema("contents.content") from e
    return ContentsOut(kind="file", path=path, ref=ref, file=file_from_bytes(base, data, truncated=False))


# ---- PR ----

def _branch_ref(raw: Any) -> BranchRef | None:
    if not isinstance(raw, dict):
        return None
    repo = raw.get("repo")
    return BranchRef(ref=_s(raw, "ref"), sha=_s(raw, "sha"),
                     repo_full_name=_s(repo, "full_name") if isinstance(repo, dict) else None)


def view_pull_summary(raw: Any) -> PullSummary:
    d = _dict(raw, "pull")
    merged_at = _s(d, "merged_at")
    return PullSummary(
        number=_req_i(d, "number", "pull"), title=_req_s(d, "title", "pull"), state=_req_s(d, "state", "pull"),
        draft=_b(d, "draft"), merged=d["merged"] if isinstance(d.get("merged"), bool) else merged_at is not None,
        user=user_ref(d.get("user")), created_at=_s(d, "created_at"), updated_at=_s(d, "updated_at"),
        closed_at=_s(d, "closed_at"), merged_at=merged_at, head=_branch_ref(d.get("head")),
        base=_branch_ref(d.get("base")), labels=_labels(d.get("labels")), html_url=_gh_url(d.get("html_url")),
    )


def view_pull_detail(raw: Any) -> PullDetail:
    d = _dict(raw, "pull")
    base = view_pull_summary(d)
    body, truncated = _cap(_s(d, "body"), BODY_LIMIT)
    mergeable = d["mergeable"] if isinstance(d.get("mergeable"), bool) else None
    return PullDetail(
        **base.model_dump(), body=body, body_truncated=truncated, mergeable=mergeable,
        mergeable_computing=mergeable is None and base.state == "open", mergeable_state=_s(d, "mergeable_state"),
        additions=_i(d, "additions"), deletions=_i(d, "deletions"), changed_files=_i(d, "changed_files"),
        commits=_i(d, "commits"), comments=_i(d, "comments"), review_comments=_i(d, "review_comments"),
        requested_reviewers=_users(d.get("requested_reviewers")), assignees=_users(d.get("assignees")),
    )


def view_pull_file(raw: Any) -> PullFile:
    d = _dict(raw, "pull_file")
    status = _req_s(d, "status", "pull_file")
    changes = _i(d, "changes") or 0
    patch = d.get("patch") if isinstance(d.get("patch"), str) else None
    if patch is None:
        # 내용 변경이 없는 이름·모드 변경이면 empty, 그 밖에 patch 가 없으면 바이너리이거나 GitHub이 너무 커서 생략한 것.
        state = "empty" if changes == 0 and status in ("renamed", "copied", "changed", "modified") else "unavailable"
    elif len(patch) > PATCH_LIMIT:
        patch, state = patch[:PATCH_LIMIT], "truncated"
    else:
        state = "ok"
    return PullFile(
        filename=_req_s(d, "filename", "pull_file"), previous_filename=_s(d, "previous_filename"), status=status,
        additions=_i(d, "additions") or 0, deletions=_i(d, "deletions") or 0, changes=changes, patch=patch,
        patch_state=state, blob_url=_gh_url(d.get("blob_url")),  # type: ignore[arg-type]
    )


# ---- 이슈 ----

def view_issue_summary(raw: Any) -> IssueSummary:
    d = _dict(raw, "issue")
    return IssueSummary(
        number=_req_i(d, "number", "issue"), title=_req_s(d, "title", "issue"), state=_req_s(d, "state", "issue"),
        state_reason=_s(d, "state_reason"), locked=_b(d, "locked"), user=user_ref(d.get("user")),
        labels=_labels(d.get("labels")), comments=_i(d, "comments") or 0, created_at=_s(d, "created_at"),
        updated_at=_s(d, "updated_at"), closed_at=_s(d, "closed_at"), html_url=_gh_url(d.get("html_url")),
    )


def view_issue_list(raw: Any) -> list[IssueSummary]:
    """이슈 API는 PR도 준다(`pull_request` 키). 이슈 목록에서는 뺀다."""
    return [view_issue_summary(x) for x in _list(raw) if not (isinstance(x, dict) and "pull_request" in x)]


def view_issue_detail(raw: Any) -> IssueDetail:
    d = _dict(raw, "issue")
    base = view_issue_summary(d).model_dump()
    if "pull_request" in d:
        return IssueDetail(**base, kind="pull")
    body, truncated = _cap(_s(d, "body"), BODY_LIMIT)
    ms = d.get("milestone")
    return IssueDetail(**base, kind="issue", body=body, body_truncated=truncated, assignees=_users(d.get("assignees")),
                       milestone=_s(ms, "title") if isinstance(ms, dict) else None,
                       author_association=_s(d, "author_association"))


def view_comment(raw: Any) -> Comment:
    d = _dict(raw, "comment")
    body, truncated = _cap(_s(d, "body"), BODY_LIMIT)
    return Comment(id=_req_i(d, "id", "comment"), user=user_ref(d.get("user")), body=body, body_truncated=truncated,
                   author_association=_s(d, "author_association"), created_at=_s(d, "created_at"),
                   updated_at=_s(d, "updated_at"), html_url=_gh_url(d.get("html_url")))


# ---- Actions ----

def view_run_summary(raw: Any) -> RunSummary:
    d = _dict(raw, "run")
    return RunSummary(
        id=_req_i(d, "id", "run"), name=_s(d, "name"), display_title=_s(d, "display_title"),
        run_number=_i(d, "run_number"), run_attempt=_i(d, "run_attempt"), event=_s(d, "event"),
        status=_s(d, "status"), conclusion=_s(d, "conclusion"), head_branch=_s(d, "head_branch"),
        head_sha=_s(d, "head_sha"), workflow_id=_i(d, "workflow_id"), path=_s(d, "path"), actor=user_ref(d.get("actor")),
        created_at=_s(d, "created_at"), run_started_at=_s(d, "run_started_at"), updated_at=_s(d, "updated_at"),
        html_url=_gh_url(d.get("html_url")),
    )


def view_run_detail(raw: Any) -> RunDetail:
    d = _dict(raw, "run")
    commit = d.get("head_commit")
    message = _s(commit, "message") if isinstance(commit, dict) else None
    first = message.splitlines()[0][:200] if message else None
    return RunDetail(**view_run_summary(d).model_dump(), triggering_actor=user_ref(d.get("triggering_actor")),
                     head_commit_message=first)


def view_job(raw: Any) -> Job:
    d = _dict(raw, "job")
    steps: list[Step] = []
    for x in _list(d.get("steps")):
        s = _dict(x, "job.step")
        steps.append(Step(number=_req_i(s, "number", "step"), name=_req_s(s, "name", "step"), status=_s(s, "status"),
                          conclusion=_s(s, "conclusion"), started_at=_s(s, "started_at"),
                          completed_at=_s(s, "completed_at")))
    failed = next((s.number for s in steps if s.conclusion in ("failure", "timed_out")), None)
    return Job(
        id=_req_i(d, "id", "job"), run_id=_i(d, "run_id"), run_attempt=_i(d, "run_attempt"),
        name=_req_s(d, "name", "job"), status=_s(d, "status"), conclusion=_s(d, "conclusion"),
        started_at=_s(d, "started_at"), completed_at=_s(d, "completed_at"), runner_name=_s(d, "runner_name"),
        labels=[x for x in _list(d.get("labels")) if isinstance(x, str)], steps=steps, first_failed_step=failed,
        html_url=_gh_url(d.get("html_url")),
    )
