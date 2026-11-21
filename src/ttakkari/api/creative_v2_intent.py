"""Creative Studio v2 제작 의도 v2 API. 계약: docs/design/creative-studio-v2-intent-v2.md

`api/creative_v2.py`와 같은 prefix를 쓰지만 별도 라우터다. 그 파일은 다른 작업(integ 이식)이 함께 바꾸므로
새 경로는 여기에 둔다. 상태를 저장하지 않는 계산 경로이며 파일 경로·URL을 입력으로 받지 않는다.
"""

from __future__ import annotations

from fastapi import APIRouter

from ttakkari.api.creative import perform
from ttakkari.creative.v2.facets import facet_contract
from ttakkari.creative.v2.intent_resolve import (
    ResolveRequest,
    ResolveResult,
    resolve_intent,
)
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/creative/v2", tags=["creative-studio-v2"])


@router.get("/intent/facets")
async def intent_facets(_: User):
    return facet_contract()


@router.post("/intent/resolve", response_model=ResolveResult)
async def intent_resolve(body: ResolveRequest, _: User):
    return perform(resolve_intent, body)
