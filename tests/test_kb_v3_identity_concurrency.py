"""Concurrent entity identity creation must converge without transaction retries.

The yield widens the real SELECT/INSERT race; database I/O and constraints remain real.
These functions can also run on a populated owned review DB: every test has a fresh tenant.
"""
import asyncio
import uuid

from sqlalchemy import func, select

from ttakkari.db import sessionmaker
from ttakkari.kb_ontology.resolver import resolve_entity
from ttakkari.kb_ontology.seed_aliases import ensure_seed_entity, get_seed_entities
from ttakkari.kb_ontology_models import KbEntity, KbEntityAlias


def widen_identity_read_window(db):
    execute = db.execute

    async def delayed(statement, *args, **kwargs):
        result = await execute(statement, *args, **kwargs)
        sql = str(statement)
        if sql.lstrip().startswith('SELECT') and 'FROM kb_entities' in sql:
            await asyncio.sleep(0.05)
        return result

    db.execute = delayed


async def test_concurrent_seed_creation_converges_without_unique_errors():
    tenant = 'race-seed-' + uuid.uuid4().hex
    seed = next(s for s in get_seed_entities() if s.canonical_name == 'Kubernetes')

    async def create():
        async with sessionmaker()() as db:
            widen_identity_read_window(db)
            entity, created = await ensure_seed_entity(db, seed, tenant, 'work_only', 'work')
            identity = entity.id
            await db.commit()
            return identity, created

    results = await asyncio.gather(*(create() for _ in range(4)), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, [f'{type(e).__name__}: {str(e)[:180]}' for e in errors]
    assert len({r[0] for r in results}) == 1
    assert sum(r[1] for r in results) == 1
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbEntity).where(KbEntity.tenant_id == tenant)) == 1
        aliases = (await db.execute(select(KbEntityAlias.norm_alias).join(KbEntity).where(KbEntity.tenant_id == tenant))).scalars().all()
        assert len(aliases) == len(set(aliases)) and len(aliases) > 1


async def test_concurrent_nonseed_resolution_converges_without_unique_errors():
    tenant = 'race-resolver-' + uuid.uuid4().hex
    name = 'UnlistedConcurrencyConcept' + uuid.uuid4().hex

    async def create():
        async with sessionmaker()() as db:
            widen_identity_read_window(db)
            result = await resolve_entity(db, name, tenant_id=tenant, entity_kind='concept', domain='general')
            identity = result.entity.id
            await db.commit()
            return identity, result.created

    results = await asyncio.gather(*(create() for _ in range(4)), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, [f'{type(e).__name__}: {str(e)[:180]}' for e in errors]
    assert len({r[0] for r in results}) == 1
    assert sum(r[1] for r in results) == 1


async def test_opposite_entity_order_commits_without_deadlock():
    tenant = 'race-order-' + uuid.uuid4().hex
    labels = ('Kubernetes', 'PostgreSQL')

    async def create(names):
        async with sessionmaker()() as db:
            widen_identity_read_window(db)
            identities = []
            for name in names:
                result = await resolve_entity(db, name, tenant_id=tenant, entity_kind='technology', domain='infra')
                identities.append(str(result.entity.id))
            await db.commit()
            return set(identities)

    results = await asyncio.wait_for(asyncio.gather(create(labels), create(tuple(reversed(labels))), return_exceptions=True), timeout=15)
    assert not any(isinstance(r, BaseException) for r in results), [str(r)[:180] for r in results]
    assert results[0] == results[1] and len(results[0]) == 2
