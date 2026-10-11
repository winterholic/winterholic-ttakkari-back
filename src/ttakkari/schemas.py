"""API 데이터 계약. 프론트 타입(src/api/types.ts)과 1:1 로 맞춘다."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EngineName = Literal["claude", "codex"]
# 세션 엔진. auto 는 엔진 프로필을 우선순위대로 쓰고 사용량 한도면 다음으로 넘어간다(ADR 0006).
SessionEngine = Literal["claude", "codex", "auto"]
SessionKindName = Literal["chat", "orchestrator"]
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
    # 작업 세션(worker)은 오케스트레이터 도구로만 만든다.
    kind: SessionKindName = "chat"


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    model: str | None = Field(default=None, max_length=100)
    effort: Effort | None = None
    archived: bool | None = None


class SessionOut(ORM):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    engine: str
    model: str | None
    effort: str | None
    engine_session_id: str | None
    kind: str
    parent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    active_run_id: uuid.UUID | None = None
    last_run_status: str | None = None


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
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


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


class ArtifactRegisterIn(BaseModel):
    workspace_id: uuid.UUID
    rel_path: str = Field(min_length=1, max_length=4096)
    session_id: uuid.UUID | None = None


class DownloadLinkOut(BaseModel):
    url: str
    expires_at: datetime


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None
