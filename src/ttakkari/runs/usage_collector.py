"""계정별 사용량 주기 수집. 모델을 부르지 않고 사용량 조회 API(Claude OAuth·Codex wham)만 쓴다(usage.py).

계정마다 다음 수집 시각을 따로 둔다.
- 평소 usage_poll_seconds, Run 이 그 계정을 쓰는 중이거나 잔여가 usage_low_remaining_percent 미만이면 usage_poll_busy_seconds.
- 알려진 초기화 시각이 지나면 바로 한 번 더 읽는다(한도가 풀렸는지 빨리 알려고).
- 실패(API 를 못 써서 스트림·세션 로그로 대신한 경우 포함)하면 주기를 두 배씩 늘린다(최대 usage_backoff_max_seconds).
- 주기에는 ±usage_poll_jitter 지터를 넣고, 한 번에 한 계정씩 잠깐 쉬며 읽어 상대 서비스를 몰아치지 않는다.
- 꺼진 계정은 읽지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ttakkari.config import Settings, get_settings
from ttakkari.db import sessionmaker
from ttakkari.runs import profiles, usage_store
from ttakkari.runs import usage as usage_mod

log = logging.getLogger(__name__)
TICK_SECONDS = 15.0
GAP_SECONDS = (0.5, 2.0)  # 계정 사이 간격
MIN_REFETCH = 30.0  # 초기화 시각 직후 재조회가 연달아 일어나지 않게
PRUNE_EVERY = 3600.0

Fetch = Callable[[uuid.UUID, str, str | None], Awaitable[usage_mod.Usage]]


@dataclass
class _State:
    next_due: float
    failures: int = 0
    last_fetch: float = 0.0


def lowest_remaining(u: usage_mod.Usage | None, now: float) -> int | None:
    """5시간·주간 창의 가장 낮은 잔여 퍼센트. 초기화 시각이 지난 창은 새로 시작된 것으로 보고 뺀다."""
    left = [100 - w.percent for w in (u.windows if u else [])
            if w.key in ("five_hour", "seven_day") and not (w.resets_at and w.resets_at <= now)]
    return min(left) if left else None


def interval_for(s: Settings, *, running: int, remaining: int | None, failures: int, rng: random.Random | None = None) -> float:
    """다음 수집까지의 초. 바쁘거나 잔여가 낮으면 busy 주기, 실패가 쌓이면 늘린다."""
    busy = running > 0 or (remaining is not None and remaining < s.usage_low_remaining_percent)
    base = float(s.usage_poll_busy_seconds if busy else s.usage_poll_seconds)
    if failures:
        base = min(base * 2 ** min(failures, 8), float(s.usage_backoff_max_seconds))
    j = s.usage_poll_jitter
    return base * (1 + (rng or random).uniform(-j, j))


def reset_passed(u: usage_mod.Usage | None, now: float) -> bool:
    """측정한 뒤에 초기화 시각이 지난 창이 있나."""
    if u is None or not u.updated_at:
        return False
    return any(w.resets_at and u.updated_at < w.resets_at <= now for w in u.windows)


class Collector:
    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or self._default_fetch
        self._state: dict[uuid.UUID, _State] = {}
        self._last_prune = 0.0

    @staticmethod
    async def _default_fetch(account_id: uuid.UUID, engine: str, config_dir: str | None) -> usage_mod.Usage:
        return await usage_mod.usage_for(account_id, engine, config_dir, refresh=True)

    async def tick(self, now: float | None = None) -> list[uuid.UUID]:
        """수집할 때가 된 켜진 계정을 읽는다. 읽은 계정 id 를 돌려준다."""
        s = get_settings()
        now = now if now is not None else time.time()
        async with sessionmaker()() as db:
            rows = [(a.id, a.engine, a.config_dir) for a in await profiles.accounts(db) if profiles.is_on(a) or profiles.is_on(a, "work")]
        live = {r[0] for r in rows}
        for gone in set(self._state) - live:
            del self._state[gone]
        counts = profiles.running_counts()
        done: list[uuid.UUID] = []
        for i, (aid, engine, cdir) in enumerate(rows):
            st = self._state.get(aid)
            if st is None:
                # 시작 직후에 몰리지 않게 계정마다 몇 초씩 엇갈려 첫 수집을 한다.
                self._state[aid] = st = _State(next_due=now + 1 + i * 3 + random.uniform(0, 2))
            cached = usage_mod.cached(aid)
            due = now >= st.next_due or (reset_passed(cached, now) and now - st.last_fetch >= MIN_REFETCH)
            if not due:
                continue
            if done:
                await asyncio.sleep(random.uniform(*GAP_SECONDS))
            try:
                u = await self._fetch(aid, engine, cdir)
            except Exception as e:  # noqa: BLE001
                log.warning("usage fetch crashed: %s", type(e).__name__)
                u = usage_mod.Usage(error=type(e).__name__)
            # 실패 판단: API 로 못 읽었다(스트림·세션 로그 대용이거나 값이 없음).
            ok = u.source in ("api", "antigravity_cli") and bool(u.windows)
            st.failures = 0 if ok else st.failures + 1
            st.last_fetch = now
            st.next_due = now + interval_for(s, running=counts.get(aid, 0), remaining=lowest_remaining(usage_mod.cached(aid), now),
                                             failures=st.failures)
            done.append(aid)
        if now - self._last_prune >= PRUNE_EVERY:
            self._last_prune = now
            try:
                async with sessionmaker()() as db:
                    n = await usage_store.prune(db, s.usage_history_days)
                if n:
                    log.info("pruned %d usage samples", n)
            except Exception as e:  # noqa: BLE001
                log.warning("usage prune failed: %s", type(e).__name__)
        return done

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001
                log.warning("usage collector tick failed: %s", type(e).__name__)
            await asyncio.sleep(TICK_SECONDS)
