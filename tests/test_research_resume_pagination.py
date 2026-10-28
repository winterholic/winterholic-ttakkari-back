"""Actual ASGI/PG: page past the 500-row snapshot without losing immutable history."""

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from ttakkari.db import sessionmaker
from ttakkari.research.models import Event, Experiment, Node, Relation


async def test_resume_pages_all_collections_past_500_and_keeps_page_quotes(client, auth):
    async def post(path, body):
        response = await client.post('/api/research' + path, headers=auth, json=body)
        assert response.status_code == 201, response.text
        return response.json()

    study = await post('/studies', {'title': 'Large synthetic history', 'goal': 'Read every record', 'scope': 'personal'})
    sid = study['id']
    source = await post(f'/studies/{sid}/sources', {'source_key': 'page-' + uuid.uuid4().hex,
        'title': 'Synthetic quote', 'locator': 'https://example.invalid/pagination', 'body': 'A preserved quotation beyond row 500.'})
    quoted = await post(f'/studies/{sid}/nodes', {'kind': 'method', 'title': 'Quoted record beyond 500',
        'unit_id': source['units'][0]['id'], 'start': 0, 'quote': 'A preserved quotation beyond row 500.'})
    url = f'/api/research/studies/{sid}/resume'
    initial = (await client.get(url, headers=auth)).json()
    classes = {'nodes': Node, 'relations': Relation, 'experiments': Experiment, 'timeline': Event}
    expected = {kind: {row['id'] for row in initial[kind]} for kind in classes}
    async with sessionmaker()() as db:
        identity = list((await db.execute(text('select current_database(), current_user'))).one())
        supplied = make_url(os.environ['TTAKKARI_TEST_DB'])
        assert identity[0] == supplied.database != 'ttakkari'
        if supplied.username:
            assert identity[1] == supplied.username
        # Synthetic rows test scale/ordering only. No model or adapter execution is represented.
        when = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(505):
            node_id = uuid.UUID(int=i + 1)
            rows = {
                'nodes': Node(id=node_id, study_id=uuid.UUID(sid), kind='hypothesis', title=f'History {i}',
                              summary='', epistemic_status='unverified', created_at=when),
                'relations': Relation(id=uuid.UUID(int=i + 2000), study_id=uuid.UUID(sid), from_id=node_id,
                    to_id=uuid.UUID(quoted['id']), predicate='combination_hypothesis', rationale='Synthetic edge', created_at=when),
                'experiments': Experiment(id=uuid.UUID(int=i + 5000), study_id=uuid.UUID(sid), title=f'Draft {i}',
                    hypothesis='Unverified', rationale='Scale fixture', adapter='retrieval_eval', created_at=when),
                'timeline': Event(id=uuid.UUID(int=i + 7000), study_id=uuid.UUID(sid), kind='fixture_recorded',
                    actor='synthetic', payload={'index': i}, created_at=when),
            }
            # Node consistency triggers require endpoint rows to exist before edges are inserted.
            db.add(rows['nodes'])
            await db.flush()
            db.add_all([row for kind, row in rows.items() if kind != 'nodes'])
            for kind, row in rows.items():
                expected[kind].add(str(row.id))
        await db.commit()
    default = (await client.get(url, headers=auth)).json()
    assert default['truncated'] is True and all(len(default[kind]) == 500 for kind in classes)
    seen = {kind: [] for kind in classes}
    pages = []
    for offset in range(0, 600, 100):
        response = await client.get(url, headers=auth, params={'offset': offset, 'limit': 100})
        assert response.status_code == 200, response.text
        page = response.json()
        assert all(len(page[kind]) <= 100 for kind in classes)
        assert page['pagination']['offset'] == offset and page['pagination']['limit'] == 100
        assert page['pagination']['totals'] == {kind: len(ids) for kind, ids in expected.items()}
        for kind in classes:
            seen[kind].extend(row['id'] for row in page[kind])
        pages.append(page)
    for kind, ids in seen.items():
        assert len(ids) == len(set(ids)) and set(ids) == expected[kind]
    final_quote = next(row for row in pages[-1]['evidence'] if row['id'] == quoted['evidence_id'])
    assert final_quote['quote'] == 'A preserved quotation beyond row 500.'
    assert not pages[-1]['pagination']['has_more']
    assert pages[-1]['truncated'] is True  # A tail page is still only part of the study.
    empty = (await client.get(url, headers=auth, params={'offset': 10000, 'limit': 100})).json()
    assert all(empty[kind] == [] for kind in classes) and not empty['pagination']['has_more']
    if target := os.environ.get('RESEARCH_PAGING_RECEIPT'):
        Path(target).write_text(json.dumps({'scope': 'Actual ASGI/Run PG; 505 synthetic rows per collection',
            'identity': identity, 'counts': {kind: len(ids) for kind, ids in seen.items()},
            'page_lengths': [{kind: len(page[kind]) for kind in classes} for page in pages],
            'last_page': pages[-1], 'empty': empty}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


@pytest.mark.parametrize('params', [{'offset': -1}, {'limit': 0}, {'limit': 501}, {'offset': 'invalid'}])
async def test_resume_rejects_invalid_page_bounds(client, auth, params):
    study = await client.post('/api/research/studies', headers=auth, json={'title': 'Bounds', 'goal': 'Stay bounded'})
    assert study.status_code == 201
    response = await client.get(f"/api/research/studies/{study.json()['id']}/resume", headers=auth, params=params)
    assert response.status_code == 422, response.text


async def test_agent_resume_forwards_pages_across_display_groups(client, auth, make_dir):
    from ttakkari.orchestrator.runtime import issue_token

    async def agent(pool):
        workspace = await client.post('/api/workspaces', headers=auth,
            json={'name': 'Paged resume', 'root_path': str(make_dir())})
        assert workspace.status_code == 201, workspace.text
        session = await client.post('/api/sessions', headers=auth,
            json={'workspace_id': workspace.json()['id'], 'pool': pool})
        assert session.status_code == 201, session.text
        return {'Authorization': 'Bearer ' + issue_token(uuid.UUID(session.json()['id']))}

    personal, work = await agent('personal'), await agent('work')
    created = await client.post('/api/agent/research', headers=personal,
        json={'operation': 'create', 'data': {'title': 'Paged agent', 'goal': 'Keep scope'}})
    assert created.status_code == 200, created.text
    body = {'operation': 'resume', 'study_id': created.json()['id'], 'data': {'offset': 500, 'limit': 25}}
    response = await client.post('/api/agent/research', headers=personal, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['nodes'] == []
    assert response.json()['pagination']['offset'] == 500
    assert response.json()['pagination']['limit'] == 25
    assert (await client.post('/api/agent/research', headers=work, json=body)).status_code == 200
    assert (await client.post('/api/agent/research', headers=personal,
        json={**body, 'data': {'offset': -1}})).status_code == 422


async def test_paged_quotes_use_observation_currentness_instead_of_highest_revision(client, auth):
    from ttakkari.collector.control_models import KbObservation

    created = await client.post('/api/research/studies', headers=auth, json={'title': 'Observed sources', 'goal': 'Read correct provenance'})
    sid = created.json()['id']
    body = {'source_key': 'observed-page-' + uuid.uuid4().hex, 'title': 'Observed source', 'locator': 'https://example.invalid/observed-page'}
    revisions = []
    for quote in ['Synthetic snapshot A', 'Synthetic snapshot B']:
        source = await client.post(f'/api/research/studies/{sid}/sources', headers=auth, json={**body, 'body': quote})
        assert source.status_code == 201, source.text
        revisions.append(source.json()['revision'])
        node = await client.post(f'/api/research/studies/{sid}/nodes', headers=auth,
            json={'kind': 'method', 'title': quote, 'unit_id': source.json()['units'][0]['id'], 'start': 0, 'quote': quote})
        assert node.status_code == 201, node.text
    async with sessionmaker()() as db:
        # A is observed after B even though its stored revision number is lower.
        db.add(KbObservation(document_id=uuid.UUID(revisions[0]['document_id']),
            revision_id=uuid.UUID(revisions[0]['id']), envelope_id=uuid.uuid4(),
            event_time=datetime(2026, 10, 26, tzinfo=UTC)))
        await db.commit()
    response = await client.get(f'/api/research/studies/{sid}/resume', headers=auth, params={'limit': 100})
    assert response.status_code == 200, response.text
    evidence = {row['revision_id']: row for row in response.json()['evidence']}
    assert evidence[revisions[0]['id']]['active'] is True
    assert evidence[revisions[1]['id']]['active'] is False
