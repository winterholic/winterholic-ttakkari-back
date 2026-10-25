"""KB v3 자동 지식화 파이프라인 타입 및 인터페이스 계약.

6단계 무인 지식화 파이프라인:
1. parsing: 원본 포맷 파싱 및 구조 메타데이터 추출
2. chunking: 구조/의미 보존 청킹 및 패시지 생성
3. extracting: L0/L1/L2 3단 온톨로지 개념·주장·엔터티 추출
4. linking: 엔터티 별칭 정규화 및 문서-엔터티 연결
5. indexing: 어휘/임베딩 색인 갱신
6. canonicalizing: 다중 문서 정본 통합 및 상충 주장 감지
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

# 6단계 파이프라인 단계 식별자
STAGE_PARSING = "parsing"
STAGE_CHUNKING = "chunking"
STAGE_EXTRACTING = "extracting"
STAGE_LINKING = "linking"
STAGE_INDEXING = "indexing"
STAGE_CANONICALIZING = "canonicalizing"

PIPELINE_STAGES: tuple[str, ...] = (
    STAGE_PARSING,
    STAGE_CHUNKING,
    STAGE_EXTRACTING,
    STAGE_LINKING,
    STAGE_INDEXING,
    STAGE_CANONICALIZING,
)

# 잡 전체 상태
JOB_STATUS_REGISTERED = "registered"
JOB_STATUS_READY = "ready"
JOB_STATUS_DEGRADED = "degraded"
JOB_STATUS_HELD = "held"
JOB_STATUS_FAILED = "failed"

JOB_STATUSES: tuple[str, ...] = (
    JOB_STATUS_REGISTERED,
    STAGE_PARSING,
    STAGE_CHUNKING,
    STAGE_EXTRACTING,
    STAGE_LINKING,
    STAGE_INDEXING,
    STAGE_CANONICALIZING,
    JOB_STATUS_READY,
    JOB_STATUS_DEGRADED,
    JOB_STATUS_HELD,
    JOB_STATUS_FAILED,
)

# 개별 단계 상태
STAGE_STATUS_PENDING = "pending"
STAGE_STATUS_RUNNING = "running"
STAGE_STATUS_SUCCEEDED = "succeeded"
STAGE_STATUS_DEGRADED = "degraded"
STAGE_STATUS_HELD = "held"
STAGE_STATUS_FAILED = "failed"
STAGE_STATUS_SKIPPED = "skipped"

STAGE_STATUSES: tuple[str, ...] = (
    STAGE_STATUS_PENDING,
    STAGE_STATUS_RUNNING,
    STAGE_STATUS_SUCCEEDED,
    STAGE_STATUS_DEGRADED,
    STAGE_STATUS_HELD,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_SKIPPED,
)


class StaleWriterError(RuntimeError):
    """임차권(lease) 만료 또는 fencing 토큰(attempt_token) 불일치로 쓰기가 거부됨."""


class PipelineError(Exception):
    """파이프라인 실행 중 발생한 일반 오류."""


class CapabilityMissingError(PipelineError):
    """필수 단계 처리기(processor) 부재로 인한 작업 보류/기능 저하."""


@dataclass
class StageContext:
    """개별 단계 프로세서가 전달받는 실행 맥락.

    visibility 와 origin_scope 는 기본값 'global'에 의존하지 않고,
    원천 문서 접근(KbDocumentAccess)에서 유도된 fail-closed 값('work_only')을 기본으로 취한다.
    """

    job_id: uuid.UUID
    document_id: uuid.UUID
    revision_id: uuid.UUID
    stage: str
    attempt_token: uuid.UUID
    origin_scope: str = "work"
    visibility: str = "work_only"
    session_factory: Callable[[], AsyncSession] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageResult:
    """개별 단계 프로세서의 실행 결과."""

    status: str  # 'succeeded' | 'degraded' | 'held' | 'failed' | 'skipped'
    detail: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    processor_version: str = "v1"

    @classmethod
    def success(
        cls, detail: dict[str, Any] | None = None, version: str = "v1"
    ) -> StageResult:
        return cls(
            status=STAGE_STATUS_SUCCEEDED,
            detail=detail or {},
            processor_version=version,
        )

    @classmethod
    def held(
        cls,
        reason: str,
        detail: dict[str, Any] | None = None,
        version: str = "v1",
    ) -> StageResult:
        d = {"reason": reason}
        if detail:
            d.update(detail)
        return cls(
            status=STAGE_STATUS_HELD,
            detail=d,
            error_message=reason,
            processor_version=version,
        )

    @classmethod
    def degraded(
        cls,
        reason: str,
        detail: dict[str, Any] | None = None,
        version: str = "v1",
    ) -> StageResult:
        d = {"reason": reason}
        if detail:
            d.update(detail)
        return cls(
            status=STAGE_STATUS_DEGRADED,
            detail=d,
            error_message=reason,
            processor_version=version,
        )

    @classmethod
    def failed(
        cls,
        error_message: str,
        detail: dict[str, Any] | None = None,
        version: str = "v1",
    ) -> StageResult:
        return cls(
            status=STAGE_STATUS_FAILED,
            detail=detail or {},
            error_message=error_message,
            processor_version=version,
        )


class StageProcessor(Protocol):
    """개별 단계 실행을 담당하는 호출 가능 객체 프로토콜."""

    async def __call__(self, ctx: StageContext) -> StageResult: ...
