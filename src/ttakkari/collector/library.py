"""단일 지식 도서관 규약(C2). 수집·도서관에는 personal/work 제품 구분이 없다.

- 저장 키: 모든 새 행은 `LIBRARY_SCOPE` 하나로 쓴다. `scope` 컬럼은 이전 세대(0072) 코드와 CHECK 제약이 아직 기대하므로
  지우지 않고 단일 값으로 정규화한다. 값 'personal' 은 옛 이름일 뿐 "개인용"이라는 뜻이 아니다.
- 조회: 소유자(owner JWT)는 tenant 안의 모든 행을 본다. 병합(`scope_merge`) 전후 어느 상태에서도 같은 결과가 나오도록
  옛 값 전부를 같은 도서관으로 취급한다(`in_library`).
- 접근 경계: 구분이 사라져도 회사 계정 엔진(pool=work 세션)이 개인 자료를 읽으면 안 된다는 격리는 남긴다. 이것은 도서관
  구획이 아니라 행마다 붙는 `pool`(그 자료를 읽을 수 있는 가장 좁은 엔진 범위) 접근 속성이다.
  pool=work 세션은 pool=work 인 행만, 그 밖(소유자·personal 세션)은 전부 본다. 개인 쪽이 전체를 보는 기존 계정 pool 의미와 같다.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import ColumnElement, or_, true

LIBRARY_SCOPE = "personal"
LEGACY_SCOPES = ("personal", "work", "shared")
WORK_POOL = "work"
PERSONAL_POOL = "personal"
POOLS = (PERSONAL_POOL, WORK_POOL)


def in_library(column) -> ColumnElement[bool]:
    """옛 scope 값 전부와 단일 값을 같은 도서관으로 본다."""
    return column.in_(LEGACY_SCOPES)


def job_visible(pool: str | None) -> ColumnElement[bool]:
    """pool=work 세션은 회사 엔진에 열린 job 만 본다. 그 밖은 전부 본다. 병합 전 옛 work 행도 work 로 취급한다."""
    from ttakkari.collector_models import CollectorJob

    if normalize_pool(pool) != WORK_POOL:
        return true()
    return or_(CollectorJob.pool == WORK_POOL, CollectorJob.scope == "work")


def normalize_pool(value: str | None) -> str:
    return WORK_POOL if value == WORK_POOL else PERSONAL_POOL


def effective_pool(pool: str | None, scope: str | None) -> str:
    """이전 세대가 병합 뒤에 쓴 옛 work 행(pool 기본값 personal, scope work)도 work 로 읽는다."""
    return WORK_POOL if WORK_POOL in (pool, scope) else PERSONAL_POOL


def pool_for_scope(scope: str | None) -> str:
    """옛 scope 값에서 접근 pool 을 정한다. 회사 엔진에 열려 있던 것은 work 뿐이다."""
    return WORK_POOL if scope == "work" else PERSONAL_POOL


def narrowest_pool(*pools: str | None) -> str:
    """합칠 때는 좁은 쪽을 택한다. 한쪽이라도 personal 이면 회사 엔진이 새로 읽을 수 없다."""
    return PERSONAL_POOL if any(normalize_pool(p) == PERSONAL_POOL for p in pools) else WORK_POOL


def collector_doc_keys(tenant_id: str, source_key: str, url: str) -> tuple[str, list[str]]:
    """수집 문서의 새 키(scope 없음)와 옛 scope 별 키. 같은 tenant·출처·URL 이면 같은 문서 계보다."""
    def digest(*parts: str) -> str:
        return "collector:" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    return digest(tenant_id, source_key, url), [digest(tenant_id, scope, source_key, url) for scope in LEGACY_SCOPES]


def legacy_work_upload_key(key: str) -> str:
    """옛 work 업로드는 키가 본문 키의 해시에 work: 접두사였다."""
    return "work:" + hashlib.sha256(key.encode()).hexdigest()
