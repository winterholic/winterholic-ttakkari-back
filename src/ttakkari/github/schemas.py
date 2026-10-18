"""GitHub 탐색 API 응답 모델(ADR 0042 §8). 상류 JSON을 통째로 내보내지 않고 이 모델의 필드만 내보낸다."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Rate(BaseModel):
    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None  # UTC epoch seconds
    resource: str | None = None
    low: bool = False  # remaining <= 100


class Page(BaseModel):
    page: int
    per_page: int
    has_more: bool
    total_count: int | None = None
    # 상류가 내줄 수 있는 상한에 닿았는지(PR 파일 3,000개). 닿았으면 목록이 전부가 아니다.
    limit_hit: bool = False


class Envelope[T](BaseModel):
    data: T
    page: Page | None = None
    rate: Rate | None = None
    fetched_at: str
    cached: bool = False
    stale: bool = False  # 상류 실패로 만료된 캐시를 대신 낸 경우
    account: str


class AccountOut(BaseModel):
    login: str
    host: str
    active: bool  # gh의 전역 active 여부(참고용, 선택 기준이 아님)
    scopes: list[str]
    state: str  # success | error
    token_available: bool | None = None  # probe=true 일 때만. 토큰 값은 어디에도 없다.
    rate: Rate | None = None  # 이 프로세스가 마지막으로 본 값


class UserRef(BaseModel):
    login: str
    type: str | None = None
    avatar_url: str | None = None
    html_url: str | None = None


class Profile(BaseModel):
    login: str
    id: int | None = None
    type: Literal["User", "Organization", "Bot"] | str
    name: str | None = None
    bio: str | None = None
    description: str | None = None
    company: str | None = None
    location: str | None = None
    blog: str | None = None
    avatar_url: str | None = None
    html_url: str | None = None
    public_repos: int | None = None
    total_private_repos: int | None = None
    followers: int | None = None
    following: int | None = None
    created_at: str | None = None
    is_viewer: bool = False


class OrgRef(BaseModel):
    login: str
    id: int | None = None
    description: str | None = None
    avatar_url: str | None = None


class RepoPermissions(BaseModel):
    admin: bool = False
    push: bool = False
    pull: bool = False


class RepoSummary(BaseModel):
    id: int | None = None
    name: str
    full_name: str
    owner: UserRef | None = None
    private: bool = False
    visibility: str | None = None
    description: str | None = None
    fork: bool = False
    archived: bool = False
    disabled: bool = False
    is_template: bool = False
    language: str | None = None
    stargazers_count: int | None = None
    forks_count: int | None = None
    open_issues_count: int | None = None  # 이슈와 PR을 합친 값(GitHub 정의)
    default_branch: str | None = None
    pushed_at: str | None = None
    updated_at: str | None = None
    html_url: str | None = None
    homepage: str | None = None
    topics: list[str] = []
    license: str | None = None
    size_kb: int | None = None
    has_issues: bool | None = None
    permissions: RepoPermissions | None = None


class Branch(BaseModel):
    name: str
    sha: str | None = None
    protected: bool = False
    is_default: bool = False


class ContentEntry(BaseModel):
    name: str
    path: str
    type: Literal["file", "dir", "symlink", "submodule", "other"]
    size: int | None = None
    sha: str | None = None
    html_url: str | None = None


class FileOut(BaseModel):
    name: str
    path: str
    sha: str | None = None
    size: int | None = None
    html_url: str | None = None
    binary: bool = False
    too_large: bool = False  # 표시 상한(1MiB)을 넘음. truncated 와 함께면 앞부분만 text 에 있다.
    truncated: bool = False
    text: str | None = None
    line_count: int | None = None


class SubmoduleOut(BaseModel):
    git_url: str | None = None
    sha: str | None = None


class ContentsOut(BaseModel):
    kind: Literal["dir", "file", "symlink", "submodule"]
    path: str
    ref: str | None = None
    entries: list[ContentEntry] | None = None
    # 디렉터리 상한(1,000개)에 닿음. 목록이 전부가 아닐 수 있다.
    entries_limit_hit: bool = False
    file: FileOut | None = None
    symlink_target: str | None = None
    submodule: SubmoduleOut | None = None


class Label(BaseModel):
    name: str
    color: str | None = None


class BranchRef(BaseModel):
    ref: str | None = None
    sha: str | None = None
    repo_full_name: str | None = None  # 포크이면 base 와 다르다. 삭제된 포크는 None.


class PullSummary(BaseModel):
    number: int
    title: str
    state: str
    draft: bool = False
    merged: bool = False
    user: UserRef | None = None
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    merged_at: str | None = None
    head: BranchRef | None = None
    base: BranchRef | None = None
    labels: list[Label] = []
    html_url: str | None = None


class PullDetail(PullSummary):
    body: str | None = None
    body_truncated: bool = False
    mergeable: bool | None = None
    mergeable_computing: bool = False  # mergeable 이 null: GitHub이 아직 계산 중
    mergeable_state: str | None = None
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    commits: int | None = None
    comments: int | None = None
    review_comments: int | None = None
    requested_reviewers: list[UserRef] = []
    assignees: list[UserRef] = []


PatchState = Literal["ok", "empty", "unavailable", "truncated"]


class PullFile(BaseModel):
    filename: str
    previous_filename: str | None = None
    status: str
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: str | None = None
    # ok: patch 있음 / empty: 내용 변경 없음(이름 변경·모드 변경) / unavailable: 바이너리이거나 GitHub이 너무 커서 생략 / truncated: 256KiB에서 자름
    patch_state: PatchState = "ok"
    blob_url: str | None = None


class IssueSummary(BaseModel):
    number: int
    title: str
    state: str
    state_reason: str | None = None
    locked: bool = False
    user: UserRef | None = None
    labels: list[Label] = []
    comments: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    html_url: str | None = None


class IssueDetail(IssueSummary):
    # kind="pull" 이면 이 번호는 PR이다. 본문 없이 요약만 주고 PR 화면으로 보내게 한다.
    kind: Literal["issue", "pull"] = "issue"
    body: str | None = None
    body_truncated: bool = False
    assignees: list[UserRef] = []
    milestone: str | None = None
    author_association: str | None = None


class Comment(BaseModel):
    id: int
    user: UserRef | None = None
    body: str | None = None
    body_truncated: bool = False
    author_association: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    html_url: str | None = None


class RunSummary(BaseModel):
    id: int
    name: str | None = None
    display_title: str | None = None
    run_number: int | None = None
    run_attempt: int | None = None
    event: str | None = None
    status: str | None = None
    conclusion: str | None = None
    head_branch: str | None = None
    head_sha: str | None = None
    workflow_id: int | None = None
    path: str | None = None
    actor: UserRef | None = None
    created_at: str | None = None
    run_started_at: str | None = None
    updated_at: str | None = None
    html_url: str | None = None


class RunDetail(RunSummary):
    triggering_actor: UserRef | None = None
    head_commit_message: str | None = None  # 첫 줄만


class Step(BaseModel):
    number: int
    name: str
    status: str | None = None
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class Job(BaseModel):
    id: int
    run_id: int | None = None
    run_attempt: int | None = None
    name: str
    status: str | None = None
    conclusion: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    runner_name: str | None = None
    labels: list[str] = []
    steps: list[Step] = []
    first_failed_step: int | None = None
    html_url: str | None = None


class LogWindow(BaseModel):
    job_id: int
    from_line: int  # 1부터
    lines: list[str]
    total_lines: int
    next_from_line: int | None = None
    truncated: bool = False  # 글자 수 상한으로 창이 줄어듦(또는 긴 줄을 잘랐음)
    masked: bool = True


class ResolveOut(BaseModel):
    kind: Literal["owner", "repo", "code", "pulls", "pull", "issues", "issue", "actions", "run", "job"]
    owner: str
    repo: str | None = None
    number: int | None = None
    run_id: int | None = None
    job_id: int | None = None
    ref: str | None = None
    path: str | None = None
    is_file: bool | None = None  # URL이 blob 이면 True, tree 이면 False
    tab: str | None = None
    # tree/blob 의 ref 에 `/` 가 있는지는 실제 브랜치로만 알 수 있다. account 없이 호출하면 True 로 남는다.
    needs_ref_resolution: bool = False
    ref_path: str | None = None  # needs_ref_resolution 일 때 원문(ref/path 합친 것)
