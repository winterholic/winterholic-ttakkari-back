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


def _normalize_source(source: Any) -> Any:
    """알 수 없는 값은 work_only 로 취급해 derive_derived_visibility 가 global 로 새지 않게 한다."""
    if source is None:
        return VISIBILITY_WORK_ONLY
    if isinstance(source, tuple) and len(source) == 2:
        return source[1] if source[1] in (VISIBILITY_GLOBAL, VISIBILITY_WORK_ONLY) else VISIBILITY_WORK_ONLY
    if isinstance(source, str):
        return source if source in (VISIBILITY_GLOBAL, SCOPE_PERSONAL) else VISIBILITY_WORK_ONLY
    return source


def derive_derived_policy(sources: Iterable[Any]) -> tuple[str, str]:
    """파생 객체의 (origin_scope, visibility). 원천은 (origin_scope, visibility) 튜플, visibility 문자열 또는 접근 레코드."""
    items = list(sources)
    visibility = derive_derived_visibility([_normalize_source(s) for s in items])
    origin = SCOPE_PERSONAL
    for s in items:
        if s is None:
            origin = SCOPE_WORK
        elif isinstance(s, tuple) and len(s) == 2:
            if s[0] == SCOPE_WORK or s[1] not in (VISIBILITY_GLOBAL, VISIBILITY_WORK_ONLY):
                origin = SCOPE_WORK
        elif getattr(s, "origin_scope", None) == SCOPE_WORK or getattr(s, "scope", None) == SCOPE_WORK:
            origin = SCOPE_WORK
    if not items:
        origin = SCOPE_WORK
    if origin == SCOPE_WORK:
        visibility = VISIBILITY_WORK_ONLY
    return origin, visibility
