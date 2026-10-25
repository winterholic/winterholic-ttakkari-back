"""Actual HTTP/PG selection preserves condition-bound contradictory research."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

from ttakkari.db import sessionmaker
from ttakkari.research.models import Relation


async def test_selection_reads_same_scope_relations_and_preserves_them_in_event(client, auth):
    async with sessionmaker()() as db:
        identity = (await db.execute(text('select current_database(), current_user'))).one()
        expected = make_url(os.environ['TTAKKARI_TEST_DB'])
        assert identity[0] == expected.database != 'ttakkari'
        if expected.username:
            assert identity[1] == expected.username
    async def post(path, body):
        r = await client.post('/api/research' + path, headers=auth, json=body)
        assert r.status_code in (200, 201), r.text
        return r.json()

    origin = await post('/studies', {"title": "Selection counterexamples", "goal": "Preserve uncertainty", "scope": "personal"})
    sid = origin['id']
    method = await post(f'/studies/{sid}/nodes', {"kind": "method", "title": "Familiar method " + uuid.uuid4().hex,
        "conditions": {"backend": "metal"}, "details": {"cost_seconds": 2}})
    challenge = await post(f'/studies/{sid}/nodes', {"kind": "hypothesis", "title": "Conflicting effect on A",
        "conditions": {"dataset": "A"}})
    relation = await post(f'/studies/{sid}/relations', {"from_id": challenge['id'], "to_id": method['id'],
        "predicate": "challenges_under_conditions", "rationale": "Unresolved report; do not treat links as quality",
        "conditions": {"dataset": "A"}})
    followup = await post('/studies', {"title": "Followup", "goal": "Investigate conditions", "scope": "personal"})
    selected = await post(f"/studies/{followup['id']}/select", {"environment": {"backend": "metal", "dataset": "A"}})
    row = next(x for x in selected['candidates'] if x['id'] == method['id'])
    assert row['eligibility'] == 'needs_review'
    context = row['reason']['research_context']
    assert context['truncated'] is False
    saved = next(x for x in context['relations'] if x['id'] == relation['id'])
    assert saved['other_node']['id'] == challenge['id']
    assert saved['other_node']['epistemic_status'] == 'unverified'
    assert saved['applicability']['status'] == 'applicable'
    assert row['reason']['expected_effect'] == 'unknown_until_measured'
    resumed = await client.get(f"/api/research/studies/{followup['id']}/resume", headers=auth)
    assert resumed.status_code == 200, resumed.text
    event = next(e for e in resumed.json()['timeline'] if e['kind'] == 'selection_recorded')
    assert event['payload']['candidates'][0]['reason']['research_context'] == context

    changed = await post(f"/studies/{followup['id']}/select", {"environment": {"backend": "metal", "dataset": "B"}})
    row = next(x for x in changed['candidates'] if x['id'] == method['id'])
    assert row['eligibility'] == 'candidate'
    assert row['reason']['research_context']['relations'][0]['applicability']['status'] == 'incompatible'
    if receipt_path := os.environ.get('RESEARCH_SELECTION_RECEIPT'):
        root = Path(__file__).resolve().parents[1]
        files = ['src/ttakkari/api/research.py', 'src/ttakkari/research/service.py', 'src/ttakkari/research/rules.py']
        Path(receipt_path).write_text(json.dumps({
            'scope': 'real ASGI/Run PostgreSQL; synthetic research notes, no model or GPU execution',
            'identity': list(identity), 'original_method': method, 'original_relation': relation,
            'same_conditions': selected, 'changed_conditions': changed, 'fresh_resume': resumed.json(),
            'source_sha256': {f: hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files},
        }, ensure_ascii=False, indent=2) + '\n')


async def test_selection_scopes_relations_and_flags_incomplete_context(client, auth):
    async def post(path, body):
        r = await client.post('/api/research' + path, headers=auth, json=body)
        assert r.status_code in (200, 201), r.text
        return r.json()

    study = await post('/studies', {'title': 'Bounded context', 'goal': 'No silent context loss', 'scope': 'personal'})
    sid = study['id']
    method = await post(f'/studies/{sid}/nodes', {'kind': 'method', 'title': 'Method', 'conditions': {'backend': 'metal'}, 'details': {'cost_seconds': 1}})
    hypothesis = await post(f'/studies/{sid}/nodes', {'kind': 'hypothesis', 'title': 'Combination, not established gain'})
    work = await post('/studies', {'title': 'Work private', 'goal': 'Do not leak', 'scope': 'work'})
    private = await post(f"/studies/{work['id']}/nodes", {'kind': 'hypothesis', 'title': 'PRIVATE WORK NOTE'})
    private_method = await post(f"/studies/{work['id']}/nodes", {'kind': 'method', 'title': 'PRIVATE WORK METHOD',
        'conditions': {'backend': 'metal'}, 'details': {'cost_seconds': 1}})
    await post(f"/studies/{work['id']}/relations", {'from_id': private['id'], 'to_id': private_method['id'],
        'predicate': 'challenges_under_conditions', 'rationale': 'MUST NOT LEAK', 'conditions': {'backend': 'metal'}})
    first = await post(f'/studies/{sid}/select', {'environment': {'backend': 'metal'}})
    assert first['candidates'][0]['eligibility'] == 'candidate'
    assert first['candidates'][0]['reason']['research_context'] == {'relations': [], 'truncated': False}
    assert {m['id'] for m in first['candidates']} == {method['id']}
    assert 'PRIVATE WORK NOTE' not in json.dumps(first) and 'MUST NOT LEAK' not in json.dumps(first)
    async with sessionmaker()() as db:
        db.add_all([Relation(study_id=uuid.UUID(sid), from_id=uuid.UUID(hypothesis['id']), to_id=uuid.UUID(method['id']),
                             predicate='combination_hypothesis', rationale='Separate experiment required', conditions={'backend': 'metal'})
                    for _ in range(201)])
        await db.commit()
    selected = await post(f'/studies/{sid}/select', {'environment': {'backend': 'metal'}})
    row = selected['candidates'][0]
    assert row['eligibility'] == 'needs_review'
    assert row['reason']['research_context']['truncated'] is True
    assert len(row['reason']['research_context']['relations']) == 200
    assert {r['predicate'] for r in row['reason']['research_context']['relations']} == {'combination_hypothesis'}
