"""Disposable ASGI/Run-PG fixture for the >500-record archive UI acceptance.

Run from the backend worktree with PYTHONPATH=src:tests and RESEARCH_PAGING_LIVE_INFO
pointing inside the current Run outbox. Lifespan/background engines are disabled.
"""

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import conftest  # noqa: F401 -- applies the existing isolated test settings before app imports
import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.api.research import NodeIn, SourceIn
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.main import create_app
from ttakkari.models import Base, Credential
from ttakkari.research import service
from ttakkari.research.models import Node, Study
from ttakkari.security.passwords import hash_password


async def prepare():
    settings = get_settings()
    supplied = make_url(os.environ['TTAKKARI_TEST_DB'])
    assert supplied.database != 'ttakkari'
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        identity = list((await connection.execute(text('select current_database(), current_user'))).one())
        assert identity[0] == supplied.database
        if supplied.username:
            assert identity[1] == supplied.username
        await connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await connection.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    async with sessionmaker()() as db:
        info_path = Path(os.environ['RESEARCH_PAGING_LIVE_INFO'])
        previous = json.loads(info_path.read_text()) if info_path.exists() else None
        if previous and previous['identity'] == identity:
            import uuid
            existing = await db.get(Study, uuid.UUID(previous['study_id']))
            if existing:
                print('paging fixture reused:', previous['study_id'], flush=True)
                return
        credential = await db.get(Credential, 1)
        if credential is None:
            credential = Credential(id=1, password_hash='')
            db.add(credential)
        credential.password_hash = hash_password('research-paging-test-only-2026')
        study = Study(title='500건 이후 연구 기록 · 합성 인수', goal='모든 기록을 페이지로 읽고 정확한 인용을 확인한다.', scope='personal')
        db.add(study)
        await db.flush()
        db.add_all([Node(study_id=study.id, kind='method', title=f'누적 기록 {i + 1:03d}', summary='합성 입력이며 실제 연구 성과가 아닙니다.',
                         epistemic_status='unverified', created_at=datetime(2026, 1, 1, tzinfo=UTC)) for i in range(505)])
        source = await service.import_source(db, study, SourceIn(source_key='paging-browser-source', title='Synthetic page source',
            locator='https://example.invalid/paging-browser', body='이 인용은 500건 이후에도 원문 그대로 남아 있습니다.'), 'fixture')
        quote = await service.add_node(db, study, NodeIn(kind='method', title='506번째 기록 · 원문 연결',
            unit_id=source['units'][0]['id'], start=0, quote='이 인용은 500건 이후에도 원문 그대로 남아 있습니다.'), 'fixture')
        await db.commit()
        info = {'study_id': str(study.id), 'quoted_node_id': quote['id'], 'nodes': 506, 'identity': identity,
                'scope': 'Run test PostgreSQL + own temporary HOME; synthetic input; no model/GPU/production execution'}
        if previous:
            info['previous_fixture'] = previous
    Path(os.environ['RESEARCH_PAGING_LIVE_INFO']).write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('paging fixture ready:', info['study_id'], 'nodes=506', flush=True)


@asynccontextmanager
async def no_background(app):
    # Prepare on uvicorn's event loop; pooled asyncpg connections cannot cross loops.
    await prepare()
    yield


get_settings().cors_origins = ['http://127.0.0.1:18872']
app = create_app()
app.router.lifespan_context = no_background
uvicorn.run(app, host='127.0.0.1', port=18871, log_level='warning')
