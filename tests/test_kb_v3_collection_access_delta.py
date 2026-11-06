"""0076 + C2: engine-access revocation must constrain every KB read surface."""
import uuid

import pytest
from sqlalchemy import select
from test_collection_scope_merge import (  # noqa: F401
    _operation_generation,
    _restore_schema_version,
)
from test_kb_agent_api import headers  # noqa: F401 -- real personal/work session tokens

from ttakkari.collector import scope_merge
from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbPassage


async def test_scope_merge_operates_on_current_0077_without_weakening_runtime_guards(monkeypatch):
    await _operation_generation(monkeypatch, version='0077_artcraft_account_pin')
    async with sessionmaker()() as db:
        report = await scope_merge.operate(db, apply=False)
        assert report['applied'] is False
        assert report['tables']['collector_sources']['normalized'] == 1


async def test_scope_merge_rejects_previous_0076_head(monkeypatch):
    await _operation_generation(monkeypatch, version='0076_kb_v3_collection_merge')
    async with sessionmaker()() as db:
        with pytest.raises(RuntimeError, match='schema_not_ready'):
            await scope_merge.operate(db, apply=False)


async def _register(client, auth, *, work=False):
    body = {'title': 'Access delta', 'text': 'AuroraDeltaNeedle access revocation source evidence.',
            'source_key': 'access-delta-' + uuid.uuid4().hex}
    if work:
        body['scope'] = 'work'
    r = await client.post('/api/assistant/kb/documents', headers=auth, json=body)
    assert r.status_code == 201, r.text
    d = r.json()
    async with sessionmaker()() as db:
        passage = await db.scalar(select(KbPassage).where(KbPassage.revision_id == uuid.UUID(d['revision_id'])))
        d['passage_id'] = str(passage.id)
    return d


async def test_work_agent_grant_then_revoke_blocks_list_search_detail_and_evidence(client, auth, headers):  # noqa: F811
    d = await _register(client, auth, work=True)
    did, pid = d['document_id'], d['passage_id']
    url = '/api/assistant/kb/documents/' + did + '/engine-access'
    assert (await client.put(url, headers=auth, json={'company_engine': True})).status_code == 200
    assert (await client.get('/api/agent/kb/documents/' + did, headers=headers['work'])).status_code == 200
    assert (await client.put(url, headers=auth, json={'company_engine': False})).status_code == 200
    for path in ('documents/' + did, 'passages/' + pid + '/evidence'):
        denied = await client.get('/api/agent/kb/' + path, headers=headers['work'])
        assert denied.status_code == 404, (path, denied.text)
    docs = await client.get('/api/agent/kb/documents', headers=headers['work'])
    assert did not in {item['id'] for item in docs.json()['items']}
    for mode in ('lexical', 'hybrid'):
        result = await client.get('/api/agent/kb/search', headers=headers['work'],
                                  params={'q': 'AuroraDeltaNeedle', 'mode': mode})
        assert result.status_code == 200, result.text
        assert did not in {hit['document']['id'] for hit in result.json()['items']}


async def test_engine_grant_preserves_source_provenance_and_personal_visibility(client, auth, headers):  # noqa: F811
    d = await _register(client, auth)
    did, pid = d['document_id'], d['passage_id']
    for enabled in (True, False):
        changed = await client.put('/api/assistant/kb/documents/' + did + '/engine-access', headers=auth,
                                   json={'company_engine': enabled})
        assert changed.status_code == 200
        async with sessionmaker()() as db:
            a = await db.get(KbDocumentAccess, uuid.UUID(did))
            assert (a.origin_scope, a.visibility) == ('personal', 'global')
            assert a.pool == ('work' if enabled else 'personal')
        # An engine grant is not a reclassification of the source or a revocation for its owner.
        assert (await client.get('/api/agent/kb/documents/' + did, headers=headers['personal'])).status_code == 200
        assert (await client.get('/api/agent/kb/passages/' + pid + '/evidence', headers=headers['personal'])).status_code == 200
        assert (await client.get('/api/agent/kb/documents/' + did, headers=headers['work'])).status_code == (200 if enabled else 404)


async def test_owner_single_library_includes_work_material_without_opening_personal_agent(client, auth, headers):  # noqa: F811
    d = await _register(client, auth, work=True)
    did = d['document_id']
    detail = await client.get('/api/assistant/kb/documents/' + did, headers=auth)
    assert detail.status_code == 200, detail.text
    docs = await client.get('/api/assistant/kb/documents', headers=auth)
    assert did in {item['id'] for item in docs.json()['items']}
    assert (await client.get('/api/agent/kb/documents/' + did, headers=headers['personal'])).status_code == 404


async def test_revocation_filters_derived_entities_assertions_and_relations(client, auth):
    """Graph expansion must lose source-backed knowledge when its engine grant is removed."""
    from ttakkari.kb_access import (
        assertion_access_filter,
        entity_access_filter,
        from_agent_pool,
        relation_access_filter,
    )
    from ttakkari.kb_models import KbEvidence
    from ttakkari.kb_ontology_models import (
        KbAssertion,
        KbAssertionEvidence,
        KbDocumentEntity,
        KbEntity,
        KbRelation,
        KbRelationEvidence,
    )
    d = await _register(client, auth)
    did, pid, rid = (uuid.UUID(d[k]) for k in ('document_id', 'passage_id', 'revision_id'))
    async with sessionmaker()() as db:
        p = await db.get(KbPassage, pid)
        e = KbEntity(canonical_name='AuroraDeltaNeedle', norm_name='auroradeltaneedle',
                     entity_kind='concept', origin_scope='personal', visibility='global')
        other = KbEntity(canonical_name='Access target', norm_name='access target',
                         entity_kind='concept', origin_scope='personal', visibility='global')
        evidence = KbEvidence(passage_id=pid, span_start=0, span_end=len(p.body), sha256=p.sha256)
        db.add_all([e, other, evidence])
        await db.flush()
        db.add(KbDocumentEntity(entity_id=e.id, document_id=did, revision_id=rid, passage_id=pid))
        claim = KbAssertion(document_id=did, revision_id=rid, subject_entity_id=e.id,
                            predicate='uses', object_value='Access target', norm_object_value='access target',
                            origin_scope='personal', visibility='global')
        edge = KbRelation(source_entity_id=e.id, target_entity_id=other.id, relation_type='uses',
                          status='candidate', evidence_count=1, origin_scope='personal', visibility='global')
        db.add_all([claim, edge])
        await db.flush()
        db.add_all([KbAssertionEvidence(assertion_id=claim.id, evidence_id=evidence.id),
                    KbRelationEvidence(relation_id=edge.id, evidence_id=evidence.id)])
        ids = [e.id, claim.id, edge.id]
        await db.commit()
    contexts = (from_agent_pool('work'), from_agent_pool('personal'))
    for enabled in (True, False):
        response = await client.put('/api/assistant/kb/documents/' + str(did) + '/engine-access',
                                    headers=auth, json={'company_engine': enabled})
        assert response.status_code == 200
        async with sessionmaker()() as db:
            for ctx in contexts:
                for model, obj_id, condition in (
                    (KbEntity, ids[0], entity_access_filter(ctx)),
                    (KbAssertion, ids[1], assertion_access_filter(ctx)),
                    (KbRelation, ids[2], relation_access_filter(ctx, include_candidates=True)),
                ):
                    found = await db.scalar(select(model.id).where(model.id == obj_id, condition))
                    assert bool(found) == (enabled or ctx.caller_role == 'personal_agent')
