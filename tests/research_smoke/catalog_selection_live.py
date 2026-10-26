"""Disposable real-HTTP catalog review UI fixture; synthetic records, no ML."""
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import conftest  # noqa: F401 -- isolated settings before app imports
import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.main import create_app
from ttakkari.models import Base, Credential
from ttakkari.research.models import Configuration, Experiment, Method, Run, Study
from ttakkari.security.passwords import hash_password


@asynccontextmanager
async def fixture(app):
    expected = make_url(os.environ['TTAKKARI_TEST_DB'])
    assert expected.database != 'ttakkari'
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as connection:
        identity = list((await connection.execute(text('select current_database(),current_user'))).one())
        assert identity[0] == expected.database
        if expected.username:
            assert identity[1] == expected.username
        await connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await connection.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    async with sessionmaker()() as db:
        credential = await db.get(Credential, 1)
        if credential is None:
            credential = Credential(id=1, password_hash='')
            db.add(credential)
        # The Run-provided, identity-checked test DB may already have its fixture
        # account. No tests run concurrently with this temporary HTTP fixture.
        credential.password_hash = hash_password('catalog-review-test-only-2026')
        study = Study(title='카탈로그 검토 · 합성 인수', goal='원본 실패를 검토하고 후속 설계로 이동', scope='personal', environment={'dataset': 'A'})
        method = Method(scope='personal', slug='catalog-ui-probe', name='Catalog review probe', name_ko='노트 없는 방법 · 합성 검토',
                        summary='합성 UI 인수이며 실제 연구 성과가 아닙니다.', origin='user_registered', lifecycle_status='candidate')
        db.add_all([study, method])
        await db.flush()
        exp = Experiment(study_id=study.id, title='합성 실패 이력', hypothesis='원문 보존', rationale='UI 인수',
                         method_id=method.id, adapter='retrieval_eval', adapter_id='retrieval_eval', state='failed', environment={'dataset': 'A'})
        db.add(exp)
        await db.flush()
        cfg = Configuration(experiment_id=exp.id, label='합성', config_hash=uuid.uuid4().hex, parameters={})
        db.add(cfg)
        await db.flush()
        run = Run(experiment_id=exp.id, study_id=study.id, configuration_id=cfg.id, adapter_id='retrieval_eval',
                  status='failed', execution_origin='external_report', error_code='KeyError', error_message='합성 입력의 원래 오류',
                  raw_output={'synthetic': True, 'provenance': 'manual_evaluation'}, environment_snapshot={'dataset': 'A'})
        db.add(run)
        await db.commit()
        Path(os.environ['RESEARCH_CATALOG_LIVE_INFO']).write_text(json.dumps({'study_id': str(study.id), 'method_id': str(method.id),
            'experiment_id': str(exp.id), 'run_id': str(run.id), 'identity': identity}, ensure_ascii=False, indent=2))
        print('catalog UI fixture ready', study.id, flush=True)
    yield


get_settings().cors_origins = ['http://127.0.0.1:18882']
app = create_app()
app.router.lifespan_context = fixture
uvicorn.run(app, host='127.0.0.1', port=18881, log_level='warning')
