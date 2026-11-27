"""목표 계약의 한도 대기 시각(_usage_wait_until)이 호출자 세션을 commit 하지 않고 계정 한도 힌트를 읽는지 시험."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

from sqlalchemy import func, select

from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting, EngineAccount, Run
from ttakkari.orchestrator import work
from ttakkari.runs import profiles

LIMIT_ERROR = "You've hit your usage limit."  # 초기화 시각·"try again in" 문구가 없어 계정 힌트로 넘어간다.


def _sess(pool: str = "personal") -> SimpleNamespace:
    return SimpleNamespace(engine="claude", pool=pool)


async def _add_account(name: str, *, pool: str = "personal") -> EngineAccount:
    async with sessionmaker()() as db:
        acc = EngineAccount(name=name, engine="claude", enabled=True, work_enabled=True, pool=pool, priority=0)
        db.add(acc)
        await db.commit()
        return acc


async def _wait_seconds(sess, *, attempt: int = 0) -> float:
    """호출자 트랜잭션을 연 채로 불러, 대기 시각까지 남은 초를 돌려준다."""
    async with sessionmaker()() as db:
        run = Run(error=LIMIT_ERROR)
        return await work._usage_wait_until(db, sess, run, attempt) - time.time()


async def test_empty_account_list_does_not_commit_caller_session_or_log_closed_transaction(caplog):
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(EngineAccount)) == 0
        db.add(AppSetting(key="usage-wait-hint-probe", value={"x": 1}))
        await db.flush()  # 호출자의 미커밋 변경. 힌트 조회가 같은 세션을 commit 하면 이 행이 남는다.
        with caplog.at_level(logging.ERROR, logger=work.log.name):
            until = await work._usage_wait_until(db, _sess(), Run(error=LIMIT_ERROR), 0)
        await db.rollback()
    assert not [r for r in caplog.records if "usage wait hint failed" in r.getMessage()], caplog.text
    assert "closed transaction" not in caplog.text
    # 힌트가 없으면 지수 백오프(첫 회 USAGE_WAIT_BASE_SECONDS)다.
    assert abs(until - time.time() - work.USAGE_WAIT_BASE_SECONDS) < 5
    async with sessionmaker()() as db:
        assert await db.get(AppSetting, "usage-wait-hint-probe") is None  # 호출자 롤백이 그대로 먹는다.
        assert await db.scalar(select(func.count()).select_from(EngineAccount)) > 0  # 시딩은 별도 세션에서 끝났다.


async def test_limited_account_reset_time_becomes_the_wait_time():
    acc = await _add_account("claude")
    profiles.cool_down(acc.id, 2 * 3600)
    wait = await _wait_seconds(_sess())
    assert abs(wait - 2 * 3600) < 10  # 백오프(900초)가 아니라 계정이 풀리는 시각이다.


async def test_free_account_keeps_backoff_even_when_another_account_is_limited():
    limited = await _add_account("claude-limited")
    await _add_account("claude-free")
    profiles.cool_down(limited.id, 2 * 3600)
    wait = await _wait_seconds(_sess())
    assert abs(wait - work.USAGE_WAIT_BASE_SECONDS) < 10


async def test_company_and_personal_scope_use_their_own_accounts():
    personal = await _add_account("claude-personal")
    company = await _add_account("claude-company", pool="work")
    profiles.cool_down(personal.id, 2 * 3600)
    profiles.cool_down(company.id, 4 * 3600)
    # 개인 세션은 모든 계정을 보고 가장 빨리 풀리는 쪽을 기다린다.
    assert abs(await _wait_seconds(_sess("personal")) - 2 * 3600) < 10
    # 회사 세션은 회사 계정만 본다. 개인 계정의 한도는 무관하다.
    assert abs(await _wait_seconds(_sess("work")) - 4 * 3600) < 10
    # 회사 계정이 놀고 있으면 개인 계정이 한도여도 기다리지 않고 백오프다.
    profiles.clear_cooldown(company.id)
    assert abs(await _wait_seconds(_sess("work")) - work.USAGE_WAIT_BASE_SECONDS) < 10
