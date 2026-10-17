"""API 데이터 계약. 프론트 타입(src/api/types.ts)과 1:1 로 맞춘다."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EngineName = Literal["claude", "codex", "antigravity", "muse"]
# 세션 엔진. auto 는 엔진 프로필을 우선순위대로 쓰고 사용량 한도면 다음으로 넘어간다(ADR 0006).
SessionEngine = Literal["claude", "codex", "antigravity", "muse", "qwen-local", "auto"]
SessionKindName = Literal["chat", "orchestrator", "desk"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth
class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class MeOut(BaseModel):
    authenticated: bool = True
    password_set: bool = True


# ---- workspaces
class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    root_path: str = Field(min_length=1, max_length=4096)
    default_engine: EngineName = "claude"
    codex_sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    export_allowed: bool = True
    # 루트 밖에서 에이전트가 쓰기를 허용받는 추가 경로. 각각 allowed_roots 검증을 거친다.
    extra_writable_roots: list[str] = Field(default_factory=list, max_length=20)


class WorkspacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    default_engine: EngineName | None = None
    codex_sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    export_allowed: bool | None = None
    extra_writable_roots: list[str] | None = Field(default=None, max_length=20)
    archived: bool | None = None


class WorkspaceOut(ORM):
    id: uuid.UUID
    name: str
    root_path: str
    is_git: bool
    default_engine: str
    codex_sandbox: str
    export_allowed: bool
    extra_writable_roots: list[str]
    created_at: datetime
    archived_at: datetime | None


class FileEntry(BaseModel):
    name: str
    rel_path: str
    type: Literal["file", "dir", "symlink", "other"]
    size: int | None = None
    modified_at: datetime | None = None
    sensitive: bool = False


class DirListing(BaseModel):
    rel_path: str
    entries: list[FileEntry]
    truncated: bool = False


# ---- sessions
class SessionIn(BaseModel):
    workspace_id: uuid.UUID
    title: str | None = Field(default=None, max_length=300)
    engine: SessionEngine | None = None
    model: str | None = Field(default=None, max_length=100)
    effort: Effort | None = None
    strict_engine: bool = False
    # 작업 세션(worker)은 오케스트레이터 도구로만 만든다.
    kind: SessionKindName = "chat"
    # personal | work. 회사 업무 탭에서 만든 대화는 회사 계정만 쓴다.
    pool: Literal["personal", "work"] = "personal"


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    model: str | None = Field(default=None, max_length=100)
    effort: Effort | None = None
    strict_engine: bool | None = None
    archived: bool | None = None


class SessionOut(ORM):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    engine: str
    model: str | None
    effort: str | None
    worker_task_type: str | None = None
    strict_engine: bool = False
    engine_session_id: str | None
    kind: str
    parent_id: uuid.UUID | None
    pool: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    active_run_id: uuid.UUID | None = None
    last_run_status: str | None = None
    last_run_profile: str | None = None
    last_run_started_at: datetime | None = None
    # 마지막으로 끝난 Run 의 결과(없으면 오류) 앞 200자. 오케스트레이터 화면의 작업 세션 탭에 쓴다.
    last_result: str | None = None
    waiting_limit: bool = False
    waiting_reason: str | None = None
    is_stuck: bool = False


# ---- runs
class RunIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    # 사용자가 지금 열람 중인 Artifact. 후속 지시의 맥락으로 프롬프트에 붙는다.
    context_artifact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    model: str | None = Field(default=None, max_length=100)
    effort: Effort | None = None


class RunOut(ORM):
    id: uuid.UUID
    session_id: uuid.UUID
    workspace_id: uuid.UUID
    prompt: str
    context_artifact_ids: list[uuid.UUID]
    engine: str
    model: str | None
    effort: str | None
    status: str
    exit_code: int | None
    error: str | None
    result_text: str | None
    cost_usd: float | None
    usage: dict[str, Any]
    checkpoint_ref: str | None
    origin: str
    profile: str | None
    last_seq: int
    resume_after: datetime | None = None
    resume_handoff: str | None = None
    waiting_limit: bool = False
    waiting_reason: str | None = None
    last_progress: str | None = None
    is_stuck: bool = False
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # 접수 세션의 사용자 메시지 Run 에만 채운다(목록·단건 조회). 판정이 없거나 다른 응답이면 null.
    desk_feedback: DeskFeedbackBrief | None = None


class FailedRunOut(RunOut):
    """실패 목록용 Run과 소속 세션·워크스페이스 정보."""
    failed_at: datetime
    session_title: str
    session_kind: str
    session_parent_id: uuid.UUID | None
    pool: str
    workspace_name: str


class FailedRunsPage(BaseModel):
    items: list[FailedRunOut]
    next_before: datetime | None = None
    next_before_id: uuid.UUID | None = None


# ---- 접수 판정(ADR 0013)
DeskAction = Literal["answer", "delegate"]
DeskVerdict = Literal["ok", "should_delegate", "should_answer"]


class DeskFeedbackIn(BaseModel):
    verdict: DeskVerdict
    note: str | None = Field(None, max_length=2000)
    # should_delegate 이고 접수가 바로 답했던 Run 이면 그 메시지를 새 기획 세션에 넘긴다.
    redo: bool = False


class DeskFeedbackBrief(BaseModel):
    verdict: DeskVerdict
    decided: DeskAction
    expected: DeskAction
    note: str | None
    updated_at: datetime


class DeskFeedbackOut(DeskFeedbackBrief):
    run_id: uuid.UUID
    session_id: uuid.UUID
    message: str
    created_at: datetime
    # redo 로 넘긴 기획 세션. 넘기지 않았으면 null.
    lead_id: uuid.UUID | None = None


class DeskEvalItem(BaseModel):
    message: str
    expected: DeskAction


class RunEventOut(ORM):
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


# ---- artifacts
class ArtifactOut(ORM):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    session_id: uuid.UUID | None
    run_id: uuid.UUID | None
    filename: str
    mime_type: str
    kind: str
    source: str
    area: str
    rel_path: str | None
    storage_mode: str
    size_bytes: int
    sha256: str
    preview_status: str
    preview_mime: str | None
    preview_error: str | None
    export_policy: str
    downloadable: bool = False
    retention_until: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, a: Any) -> ArtifactOut:
        out = cls.model_validate(a)
        out.downloadable = a.export_policy == "allow"
        return out


class ArtifactAreaIn(BaseModel):
    area: Literal["files", "agent"]


class ArtifactRegisterIn(BaseModel):
    workspace_id: uuid.UUID
    rel_path: str = Field(min_length=1, max_length=4096)
    session_id: uuid.UUID | None = None


class ArtifactResolveIn(BaseModel):
    """메시지 본문 링크의 대상 문자열. 절대경로·~·file://·상대경로·`:줄번호` 접미사를 그대로 받는다."""

    path: str = Field(min_length=1, max_length=4096)
    # 상대경로를 풀 기준 작업 폴더와, 그 세션이 이미 가진 결과물을 먼저 찾기 위한 힌트.
    workspace_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None


class DownloadLinkOut(BaseModel):
    url: str
    expires_at: datetime


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


# ---- 작업 트리(ADR 0014)
class DeskIn(BaseModel):
    pool: Literal["personal", "work"] = "personal"


class TaskNode(BaseModel):
    """트리 한 칸. 세션 하나에 마지막 Run 상태를 붙인 것이다."""
    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: str
    title: str
    pool: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    status: str | None = None
    engine: str | None = None
    profile: str | None = None
    model: str | None = None
    last_result: str | None = None


class TaskForest(BaseModel):
    # roots 는 보여 줄 순서다. 뿌리의 parent_id 는 접수일 수 있으니 트리를 parent_id 만으로 세우지 않는다.
    roots: list[uuid.UUID]
    nodes: list[TaskNode]


class TaskTree(BaseModel):
    root_id: uuid.UUID
    # 뿌리부터 요청한 세션까지(양끝 포함)
    path: list[TaskNode]
    nodes: list[TaskNode]


class TaskRoot(TaskNode):
    descendants: int = 0
    active: int = 0
    failed: int = 0


class TaskLibrary(BaseModel):
    roots: list[TaskRoot]
    next_before: datetime | None = None


class TaskHit(BaseModel):
    node: TaskNode
    root_id: uuid.UUID
    path: list[TaskNode]
    field: Literal["title", "prompt", "result"]
    snippet: str
