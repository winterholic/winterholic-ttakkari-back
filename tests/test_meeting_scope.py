"""회의 분류 변경: 내용 보존·프로젝트 경계·즉시 검색 이동."""
import uuid

import pytest
from sqlalchemy import select
from test_meetings import TRANSCRIPT, _create, _project

from ttakkari.db import sessionmaker
from ttakkari.meetings import vectors
from ttakkari.models import Meeting, MeetingChunk

pytestmark = pytest.mark.asyncio


async def test_scope_move_preserves_content_detaches_incompatible_project_and_clears_old_index(client, auth, monkeypatch):
    monkeypatch.setattr(vectors, 'schedule', lambda *a, **kw: None)
    pid = await _project(scope='personal')
    meeting = await _create(client, auth, transcript=TRANSCRIPT, project_id=pid)
    mid = meeting['id']
    async with sessionmaker()() as db:
        m = await db.get(Meeting, uuid.UUID(mid))
        m.summary = '보존할 요약'
        db.add(MeetingChunk(meeting_id=m.id, kind='summary', ord=0, content='옛 범위 조각', scope='personal'))
        await db.commit()
    response = await client.patch(f'/api/meetings/{mid}', headers=auth, json={'scope': 'work'})
    assert response.status_code == 200, response.text
    moved = response.json()
    assert moved['scope'] == 'work'
    assert moved['project'] is None
    assert moved['transcript'] == TRANSCRIPT and moved['summary'] == '보존할 요약'
    async with sessionmaker()() as db:
        assert not list(await db.scalars(select(MeetingChunk).where(MeetingChunk.meeting_id == uuid.UUID(mid))))
    personal = (await client.get('/api/meetings', headers=auth, params={'scope': 'personal'})).json()['meetings']
    work = (await client.get('/api/meetings', headers=auth, params={'scope': 'work'})).json()['meetings']
    assert mid not in [m['id'] for m in personal] and mid in [m['id'] for m in work]
    back = await client.patch(f'/api/meetings/{mid}', headers=auth, json={'scope': 'personal'})
    assert back.status_code == 200 and back.json()['scope'] == 'personal'


async def test_scope_and_project_are_validated_together_and_shared_project_survives(client, auth):
    personal = await _project('개인', scope='personal')
    work = await _project('회사', scope='work')
    shared = await _project('공통', scope='shared')
    meeting = await _create(client, auth, transcript=TRANSCRIPT, project_id=personal)
    mid = meeting['id']
    bad = await client.patch(f'/api/meetings/{mid}', headers=auth, json={'scope': 'work', 'project_id': personal})
    assert bad.status_code == 400 and bad.json()['code'] == 'scope_mismatch'
    assert (await client.get(f'/api/meetings/{mid}', headers=auth)).json()['scope'] == 'personal'
    good = await client.patch(f'/api/meetings/{mid}', headers=auth, json={'scope': 'work', 'project_id': work})
    assert good.status_code == 200 and good.json()['project']['id'] == work
    await client.patch(f'/api/meetings/{mid}', headers=auth, json={'project_id': shared})
    kept = await client.patch(f'/api/meetings/{mid}', headers=auth, json={'scope': 'personal'})
    assert kept.status_code == 200 and kept.json()['project']['id'] == shared


@pytest.mark.parametrize('value', [None, 'shared', 'invalid'])
async def test_scope_rejects_null_or_unknown_values(client, auth, value):
    m = await _create(client, auth, transcript=TRANSCRIPT)
    response = await client.patch(f"/api/meetings/{m['id']}", headers=auth, json={'scope': value})
    assert response.status_code == 422


@pytest.mark.parametrize('state', ['transcribing', 'analyzing'])
async def test_scope_cannot_change_while_processing(client, auth, state):
    m = await _create(client, auth, transcript=TRANSCRIPT)
    async with sessionmaker()() as db:
        row = await db.get(Meeting, uuid.UUID(m['id']))
        row.status = state
        await db.commit()
    response = await client.patch(f"/api/meetings/{m['id']}", headers=auth, json={'scope': 'work'})
    assert response.status_code == 409 and response.json()['code'] == 'meeting_busy'


async def test_index_started_before_move_cannot_restore_old_scope(client, auth, monkeypatch):
    from ttakkari.context import embedder

    monkeypatch.setattr(vectors, 'schedule', lambda *a, **kw: None)
    meeting = await _create(client, auth, transcript=TRANSCRIPT)
    mid = uuid.UUID(meeting['id'])
    monkeypatch.setattr(embedder, 'enabled', lambda: True)
    monkeypatch.setattr(embedder, 'model_name', lambda: 'test')

    async def embed_then_move(texts):
        moved = await client.patch(f'/api/meetings/{mid}', headers=auth, json={'scope': 'work'})
        assert moved.status_code == 200
        return [[0.0] * 384 for _ in texts]

    monkeypatch.setattr(embedder, 'embed', embed_then_move)
    await vectors.reindex(mid)
    async with sessionmaker()() as db:
        m = await db.get(Meeting, mid)
        assert m.scope == 'work' and m.vector_status == 'pending'
        assert not list(await db.scalars(select(MeetingChunk).where(MeetingChunk.meeting_id == mid)))


async def test_move_preserves_approved_memory_and_blocks_old_scope_conflict(client, auth, monkeypatch):
    from test_meetings import _memory

    from ttakkari.models import CtxMemory, MeetingProposal

    monkeypatch.setattr(vectors, 'schedule', lambda *a, **kw: None)
    memory_id = await _memory('기존 개인 기억')
    meeting = await _create(client, auth, transcript=TRANSCRIPT)
    mid = uuid.UUID(meeting['id'])
    async with sessionmaker()() as db:
        proposal = MeetingProposal(meeting_id=mid, kind='supersede_memory', status='pending',
            payload={'content': '새 결정', 'targets': [{'id': memory_id}]})
        db.add(proposal)
        await db.commit()
        proposal_id = proposal.id
    moved = await client.patch(f'/api/meetings/{mid}', headers=auth, json={'scope': 'work'})
    assert moved.status_code == 200
    approved = await client.post(f'/api/meetings/{mid}/proposals/{proposal_id}/approve', headers=auth)
    assert approved.status_code == 409 and approved.json()['code'] == 'stale'
    async with sessionmaker()() as db:
        memory = await db.get(CtxMemory, uuid.UUID(memory_id))
        assert memory.scope == 'personal' and memory.archived_at is None
