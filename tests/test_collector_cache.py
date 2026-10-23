"""CP-S2-05: cache keys isolate scope, model, prompt, policy and query; negative entries need a reason and expire."""
from datetime import UTC, datetime, timedelta

import pytest

from ttakkari.collector import cache
from ttakkari.db import sessionmaker

BASE = {"tenant_id": "owner", "scope": "personal", "text_sha256": "a" * 64, "model_revision": "m@1", "preprocess": "nfc-1",
            "rubric": "utility-v1", "policy": "p1"}


def test_every_key_component_changes_the_key():
    base = cache.cache_key(**BASE)
    assert cache.cache_key(**BASE) == base
    for name, other in {"tenant_id": "other", "scope": "work", "text_sha256": "b" * 64, "model_revision": "m@2",
                            "preprocess": "nfc-2", "rubric": "utility-v2", "policy": "p2", "query_hash": "q", "context_hash": "c"}.items():
        assert cache.cache_key(**{**BASE, name: other}) != base, name
    with pytest.raises(ValueError):
        cache.cache_key(**{**BASE, "model_revision": ""})


async def test_hit_is_scoped_and_expires():
    personal, work = cache.cache_key(**BASE), cache.cache_key(**{**BASE, "scope": "work"})
    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        await cache.put(db, personal, tenant_id="owner", scope="personal", kind="positive", result={"s": 1}, now=now)
        await db.commit()
    async with sessionmaker()() as db:
        assert (await cache.get(db, work, now=now)) is None
        hit = await cache.get(db, personal, now=now)
        assert hit.result == {"s": 1} and hit.hits == 1
        assert await cache.get(db, personal, now=now + timedelta(days=31)) is None
        await db.commit()


async def test_negative_and_unknown_entries_need_a_reason_and_a_short_ttl():
    key = cache.cache_key(**{**BASE, "query_hash": "q1"})
    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        with pytest.raises(ValueError):
            await cache.put(db, key, tenant_id="owner", scope="personal", kind="negative", result={})
        await cache.put(db, key, tenant_id="owner", scope="personal", kind="unknown", result={"v": None}, reason="no evidence", now=now)
        await db.commit()
    async with sessionmaker()() as db:
        assert await cache.get(db, key, now=now + timedelta(hours=1)) is not None
        assert await cache.get(db, key, now=now + timedelta(hours=7)) is None
        assert await cache.purge_expired(db, now=now + timedelta(hours=7)) == 1
        await db.commit()
