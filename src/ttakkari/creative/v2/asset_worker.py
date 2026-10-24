"""Runs Creative V2 asset generation that the API accepted into production.json as `queued`.

The executor owns this loop in remote mode. Local mode keeps the API's own task and shares `run_asset_generation`.
Only `queued` records are candidates: a `running` record left by a dead process is never re-run blindly, because it may carry
side effects (a submitted Comfy task, a leased GPU) that only an explicit user retry reconciles.
"""
from __future__ import annotations

import asyncio
import logging

from ttakkari import queued_work
from ttakkari.creative.v2 import asset_generation
from ttakkari.creative.v2.plans import PlanStore, default_plan_store

log = logging.getLogger(__name__)


async def run_asset_generation(
    plan_id: str, revision: int, *, plans: PlanStore | None = None, generator=None
) -> str | None:
    """생성을 끝까지 실행한다. 생성 service 가 거부한 이유가 있으면 돌려준다(실행 기록은 service 가 이미 남겼다)."""
    from ttakkari.creative.v2.asset_generation_managed import ManagedAssetGenerator

    try:
        await asyncio.to_thread(
            asset_generation.generate_assets,
            plans or default_plan_store(),
            plan_id,
            revision,
            # TTS 실행 예산은 이 프로세스의 이벤트 루프에 묶여 있어 생성기가 같은 루프를 쓰게 한다.
            generator or ManagedAssetGenerator(loop=asyncio.get_running_loop()),
        )
    except asset_generation.AssetGenerationBusy:
        raise
    except asset_generation.AssetGenerationError as exc:
        return str(exc)
    return None


async def consume_once(*, plans: PlanStore | None = None, generator=None) -> bool:
    """가장 오래된 대기 요청 하나를 끝까지 실행한다. 실행했거나 실패로 닫았으면 True, 할 일이 없으면 False."""
    if queued_work.draining():
        return False
    plans = plans or default_plan_store()
    for request in await asyncio.to_thread(asset_generation.queued_requests, plans):
        if queued_work.draining():
            return False
        try:
            refused = await queued_work.run_tracked(
                run_asset_generation(request["plan_id"], request["revision"], plans=plans, generator=generator)
            )
        except asset_generation.AssetGenerationBusy:
            continue  # 살아 있는 다른 실행자가 같은 키를 돌린다. 요청은 queued 로 보존하고 다음 요청을 본다.
        except Exception as exc:  # 요청 하나의 예상 밖 오류가 소비자 전체를 막지 않게 한다
            log.exception("creative asset generation %s failed unexpectedly", request["plan_id"])
            await asyncio.to_thread(
                asset_generation.fail_if_queued, plans, request["plan_id"], request["id"],
                f"{type(exc).__name__}: {exc}"[:500],
            )
            return True
        # 시작 전 검증(승인 revision 등)에서 막히면 기록이 queued 로 남는다. 닫지 않으면 이 요청이 매번 맨 앞을 막는다.
        await asyncio.to_thread(
            asset_generation.fail_if_queued, plans, request["plan_id"], request["id"],
            refused or "자산 생성을 시작하지 못했습니다.",
        )
        return True
    return False
