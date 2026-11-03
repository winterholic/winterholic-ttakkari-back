"""KB v3 origin_scope / visibility 정책의 단일 진입점.

파이프라인 오케스트레이터, 추출기, 영속화(writer), 해소기 호출 지점이 모두 이 모듈만 쓴다.
각자 'global' 기본값이나 doc_key 휴리스틱을 두지 않는다.

확정 정책
1. 문서 접근(KbDocumentAccess)이 없거나 scope 를 알 수 없으면 ('work', 'work_only')로 격리(fail-closed).
2. scope == 'work' 또는 origin_scope == 'work' 면 항상 ('work', 'work_only'). 어떤 visibility 값도 global 로 올리지 못한다.
3. scope == 'personal' 이면 visibility 가 비었거나 'global' 이어도 ('personal', 'global').
   명시적 'work_only' 는 존중하고, 알 수 없는 값은 fail-closed 로 work_only 로 본다.
4. 파생 객체(엔터티·주장·관계·정본)는 원천 중 하나라도 work_only 면 work_only (kb_ontology_models.derive_derived_visibility 와 같은 규칙).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.kb_ontology_models import derive_derived_visibility

SCOPE_WORK = "work"
SCOPE_PERSONAL = "personal"
VISIBILITY_WORK_ONLY = "work_only"
VISIBILITY_GLOBAL = "global"

FAIL_CLOSED: tuple[str, str] = (SCOPE_WORK, VISIBILITY_WORK_ONLY)


def derive_scope_and_visibility(access: Any | None) -> tuple[str, str]:
    """원천 문서의 접근 레코드(KbDocumentAccess)에서 (origin_scope, visibility) 를 유도한다."""
    if access is None:
        return FAIL_CLOSED

    scope = getattr(access, "scope", None)
    origin_scope = getattr(access, "origin_scope", None) or scope
    if scope == SCOPE_WORK or origin_scope == SCOPE_WORK:
        return FAIL_CLOSED

    if scope == SCOPE_PERSONAL:
        visibility = getattr(access, "visibility", None)
        if visibility in (None, "", VISIBILITY_GLOBAL):
            return SCOPE_PERSONAL, VISIBILITY_GLOBAL
        return SCOPE_PERSONAL, VISIBILITY_WORK_ONLY

    return FAIL_CLOSED


async def resolve_document_policy(
    db: AsyncSession, document_id: uuid.UUID
) -> tuple[str, str]:
    """문서 id 의 접근 레코드로 (origin_scope, visibility) 를 유도한다. 접근 레코드가 없으면 fail-closed."""
    from ttakkari.collector.control_models import KbDocumentAccess

    return derive_scope_and_visibility(await db.get(KbDocumentAccess, document_id))


def _source_policy(source: Any) -> tuple[str, str]:
    """원천 하나를 (origin_scope, visibility) 로 바꾼다. 알 수 없거나 비어 있으면 fail-closed."""
    if source is None:
        return FAIL_CLOSED
    if isinstance(source, tuple) and len(source) == 2:
        origin, visibility = source
        if visibility not in (VISIBILITY_GLOBAL, VISIBILITY_WORK_ONLY) or origin not in (
            SCOPE_PERSONAL,
            SCOPE_WORK,
        ):
            return FAIL_CLOSED
        if origin == SCOPE_WORK:
            return FAIL_CLOSED
        return origin, visibility
    if isinstance(source, str):
        token = source.strip().lower()
        if token in (SCOPE_PERSONAL, VISIBILITY_GLOBAL):
            return SCOPE_PERSONAL, VISIBILITY_GLOBAL
        return FAIL_CLOSED
    return derive_scope_and_visibility(source)


def derive_derived_policy(sources: Iterable[Any]) -> tuple[str, str]:
    """파생 객체(엔터티·주장·관계·정본·해소 결과)의 (origin_scope, visibility).

    원천은 접근 레코드, (origin_scope, visibility) 튜플, 'work'/'personal'/'global'/'work_only' 문자열 어느 쪽이든 된다.
    원천이 없거나 하나라도 work/미상이면 ('work', 'work_only') 이다. visibility 합성은 스키마의 derive_derived_visibility 규칙이다.
    """
    policies = [_source_policy(s) for s in sources]
    if not policies:
        return FAIL_CLOSED
    visibility = derive_derived_visibility([v for _, v in policies])
    origin = SCOPE_WORK if any(o == SCOPE_WORK for o, _ in policies) else SCOPE_PERSONAL
    if origin == SCOPE_WORK:
        visibility = VISIBILITY_WORK_ONLY
    return origin, visibility
