"""자연어 접수→도구 작업 세션→실제 소스→독립 검증 연결 계약."""
import base64
import hashlib
import json
import uuid

from sqlalchemy import select

from ttakkari import reverse_tasks
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, ReverseJob, Run, RunStatus, Workspace
from ttakkari.runs.manager import manager


async def test_description_creates_tool_capable_general_worker(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    description = '소스가 없는 계산기 동작을 재현해줘. 12 + 3 입력은 15, 잘못된 입력은 오류 메시지야.'
    response = await client.post('/api/reverse', headers=auth, json={'description': description,
        'title': '두 배 CLI', 'target_type': 'cli', 'mode': 'reconstruct', 'references': ['fixture:double'],
        'max_repair_attempts': 2})
    assert response.status_code == 202, response.text
    data = response.json()
    assert data['kind'] == 'reconstruction'
    assert data['target'] == description
    assert (data['title'], data['target_type'], data['mode']) == ('두 배 CLI', 'cli', 'reconstruct')
    assert data['recommendation']['target_type'] == 'cli' and data['plan']
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(data['id']))
        sess = await db.get(ChatSession, job.session_id)
        assert sess.engine == 'auto' and sess.worker_task_type == 'general'
        assert sess.kind == 'worker'
        run = await db.scalar(select(Run).where(Run.session_id == sess.id))
        assert run.usage['reverse']['job_id'] == str(job.id)
        assert '소스코드' in run.prompt and '관찰' in run.prompt
        assert '전체 명령 경로' in run.prompt and 'fixture:double' in run.prompt
        assert job.output_path.startswith((await db.get(Workspace, job.workspace_id)).root_path)
    detail = (await client.get('/api/reverse/' + data['id'], headers=auth)).json()
    assert detail['session_id'] == str(sess.id)
    assert detail['result']['schema_version'] == 2
    assert detail['result']['latest'] == detail['latest'] is None
    assert detail['result']['history'] == detail['history'] == []
    assert [s['key'] for s in detail['stages']] == ['request', 'observe', 'infer', 'implement', 'verify', 'deliver']


async def test_repo_only_input_does_not_start_old_clone_pipeline(client, auth):
    for body in [{'target': 'morluto/rea'}, {'description': 'https://github.com/morluto/rea'}]:
        res = await client.post('/api/reverse', headers=auth, json=body)
        assert res.status_code == 422
    invalid = await client.post('/api/reverse', headers=auth, json={'description': '잘못된 모드 확인', 'mode': 'magic'})
    assert invalid.status_code == 422


async def test_analysis_mode_and_analyze_to_reconstruct_reuses_observations(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    response = await client.post('/api/reverse', headers=auth, json={'description': '웹 화면 동작을 조사해줘.', 'mode': 'analyze'})
    assert response.status_code == 202, response.text
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(response.json()['id']))
        run = await db.scalar(select(Run).where(Run.session_id == job.session_id))
        assert '소스 재구현·테스트 실행을 강제하지 말고' in run.prompt
        run.status = RunStatus.succeeded
        root = __import__('pathlib').Path(job.output_path)
        (root / '.reverse').mkdir(parents=True)
        (root / '.reverse' / f'{run.id}.json').write_text(json.dumps({'mode': 'analyze', 'summary': '상태 전이 관찰',
            'observations': [{'finding': '새로고침 후 상태 유지', 'evidence': 'evidence/state.txt', 'level': 'direct'}],
            'hypotheses': ['localStorage 복원'], 'unknowns': ['다른 기기 동기화 여부']}))
        await reverse_tasks.on_finished(db, run)
        assert job.status == 'done'
        await db.commit()
    detail = (await client.get('/api/reverse/' + response.json()['id'], headers=auth)).json()
    assert detail['result_state']['source']['status'] == 'unverified'
    assert detail['result_state']['independent_verification']['status'] == 'not_required'
    assert detail['stage'] == 'infer'
    assert next(s for s in detail['stages'] if s['key'] == 'implement')['status'] == 'skipped'
    updated = await client.patch('/api/reverse/' + response.json()['id'], headers=auth,
                                 json={'title': '상태 재현', 'mode': 'reconstruct', 'target_type': 'web'})
    assert updated.status_code == 202, updated.text
    assert updated.json()['mode'] == 'reconstruct'
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(response.json()['id']))
        queued = next(r for r in await db.scalars(select(Run).where(Run.session_id == job.session_id))
                      if '[이어서 받은 설명·지시]' in r.prompt)
        assert '새로고침 후 상태 유지' in queued.prompt
        assert '분석 모드의 기존 관찰·가설을 구현 입력으로 재사용' in queued.prompt
        assert job.result['history'][0]['report']['summary'] == '상태 전이 관찰'


async def test_auto_mode_recommends_analysis_only_and_short_title(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    response = await client.post('/api/reverse', headers=auth, json={
        'description': '계산기 앱의 소수점 입력 동작을 분석만 해줘. 재현하지 말고 차이와 미확인을 정리해줘.'})
    assert response.status_code == 202, response.text
    data = response.json()
    assert data['mode'] == 'analyze'
    assert len(data['title']) <= 48
    assert data['title'] == '계산기 앱의 소수점 입력 동작'
    assert data['recommendation']['title'] == data['title']


async def test_completion_requires_files_then_queues_independent_verification(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    res = await client.post('/api/reverse', headers=auth, json={
        'description': '입력한 숫자의 두 배를 출력하는 프로그램을 동작 기준으로 재현해줘.',
        'target_type': 'cli'})
    assert res.status_code == 202, res.text
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(res.json()['id']))
        run = await db.scalar(select(Run).where(Run.session_id == job.session_id))
        run.status = RunStatus.succeeded
        await reverse_tasks.on_finished(db, run)
        assert job.status == 'partial'  # 성공 답변만으로 소스 생성 완료로 판정하지 않는다.
        from pathlib import Path
        root = Path(job.output_path)
        (root / 'src').mkdir(parents=True)
        (root / '.reverse').mkdir()
        (root / 'src/main.py').write_text('print(int(input()) * 2)\n')
        (root / 'README.md').write_text('환경: Python 3.13\n의존성: 없음\n실행: python src/main.py\n테스트: pytest -q\n')
        (root / 'test.log').write_text('input 2 -> 4, input -1 -> -2\n')
        report = {'summary': '두 배 CLI', 'source_files': ['src/main.py'],
                  'observations': [{'finding': '2 -> 4', 'evidence': '사용자 설명'}],
                  'checks': [{'command': 'python src/main.py', 'exit_code': 0, 'log': 'test.log'}], 'limitations': []}
        def add_match(report, run_id):
            run_id = str(run_id)
            output = {'stdout': '4\n', 'stderr': '', 'exit_code': 0,
                      'stdout_bytes_b64': base64.b64encode(b'4\n').decode(),
                      'stderr_bytes_b64': base64.b64encode(b'').decode(), 'argv': ['double', '2']}
            raw = {'schema_version': 2, 'target_type': 'cli', 'run_id': run_id, 'complete': True,
                   'input': {'argv': ['double', '2']}, 'pre_state': {}, 'environment': {'os': 'test'},
                   'original': output, 'replica': output}
            raw['execution_log'] = [{'side': side, **raw[key]} for side, key in
                                    (('original', 'original'), ('replica', 'replica'))]
            raw_path = root / f'.reverse/comparisons/case-{run_id}.json'
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(raw).encode()
            raw_path.write_bytes(encoded)
            report['comparisons'] = [{'id': 'case', 'behavior': '두 배 계산', 'target_type': 'cli', 'input': raw['input'],
                'pre_state': {}, 'original': raw['original'], 'replica': raw['replica'], 'environment': {'os': 'test'},
                'evidence_level': 'direct', 'evidence': [{'kind': 'execution', 'path': raw_path.relative_to(root).as_posix(),
                    'sha256': hashlib.sha256(encoded).hexdigest()}], 'verified_at': '2026-11-07T00:00:00Z',
                'run_id': run_id, 'result': 'mismatch', 'difference': {'field': 'stdout'}}]
        # A completed callback is idempotent. Model the user's follow-up as a
        # fresh Run after the source and report have been added.
        await reverse_tasks.follow_up(db, job, '소스와 비교 결과를 확인해 독립 검증을 이어서 진행해줘.')
        run = await db.scalar(select(Run).where(Run.session_id == job.session_id).order_by(Run.created_at.desc()))
        run.status = RunStatus.succeeded
        add_match(report, run.id)
        (root / '.reverse' / f'{run.id}.json').write_text(json.dumps(report))
        await reverse_tasks.on_finished(db, run)
        verifier = await db.get(ChatSession, job.verifier_session_id)
        assert verifier.worker_task_type == 'verification' and verifier.pool == 'personal'
        review = await db.scalar(select(Run).where(Run.session_id == verifier.id))
        assert job.status == 'running' and review.status == 'queued'
        # 중복 callback가 검증 Run을 추가하지 않는다.
        await reverse_tasks.on_finished(db, run)
        assert len(list(await db.scalars(select(Run).where(Run.session_id == verifier.id)))) == 1
        review.status = RunStatus.succeeded
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'partial'  # 독립 검증 파일 없는 성공은 불충분.

        async def new_verification_run():
            next_run = Run(session_id=verifier.id, workspace_id=verifier.workspace_id,
                           prompt='', engine='auto',
                           usage={'reverse': {'job_id': str(job.id), 'role': 'verify'}})
            db.add(next_run)
            await db.flush()
            next_run.status = RunStatus.succeeded
            return next_run

        review = await new_verification_run()
        add_match(report, review.id)
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps({**report, 'verdict': 'passed', 'comparisons': []}))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'partial'  # 구현자의 직접 비교를 verifier 자신의 비교로 대체할 수 없다.
        assert job.result['latest']['independent_verification']['status'] == 'failed'
        assert job.result['latest']['verification_report']['comparisons'] == []
        review = await new_verification_run()
        add_match(report, review.id)
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps({**report, 'verdict': 'passed'}))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'done'
        # 정상 검사 하나가 있어도 실패한 필수 회귀 검사는 완료 판정을 막는다.
        failed = {**report, 'verdict': 'passed', 'checks': [*report['checks'],
                  {'command': 'pytest failing_regression', 'exit_code': 1, 'log': 'test.log'}]}
        review = await new_verification_run()
        add_match(report, review.id)
        failed['comparisons'] = report['comparisons']
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps(failed))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'partial'
        assert job.result['latest']['independent_verification']['status'] == 'failed'
        review = await new_verification_run()
        add_match(report, review.id)
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps({**report, 'comparisons': [], 'verdict': 'passed'}))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'partial'  # 검증자 비교 0건은 계속 완료를 막는다.
        review = await new_verification_run()
        add_match(report, review.id)
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps({**report, 'verdict': 'passed'}))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'done'
        await db.commit()
    data = (await client.get('/api/reverse/' + res.json()['id'], headers=auth)).json()
    assert data['result']['latest'] == data['latest']
    assert data['result']['history'] == data['history']
    assert data['result_state']['direct_comparison']['count'] == 1
    assert data['result_state']['source']['files'] == ['src/main.py']
    assert data['result_state']['independent_verification']['status'] == 'passed'
    followup = await client.post('/api/reverse/' + res.json()['id'] + '/messages', headers=auth,
                                json={'prompt': '소수 계산도 재현해줘.'})
    assert followup.status_code == 202, followup.text
    history = (await client.get('/api/reverse/' + res.json()['id'], headers=auth)).json()
    assert len(history['rounds']) >= 3
    assert any(r['role'] == 'verify' for r in history['rounds'])
    assert '소수 계산도 재현해줘.' in history['target']
    assert history['history'] and history['history'][-1]['report']['source_files'] == ['src/main.py']


async def test_direct_mismatch_queues_bounded_repair_and_preserves_failed_round(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    response = await client.post('/api/reverse', headers=auth, json={'description': 'CLI에서 2를 두 배로 출력하는 동작을 재현해줘.',
        'mode': 'reconstruct', 'target_type': 'cli', 'max_repair_attempts': 1})
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(response.json()['id']))
        impl = await db.scalar(select(Run).where(Run.session_id == job.session_id))
        root = __import__('pathlib').Path(job.output_path)
        (root / 'src').mkdir(parents=True)
        (root / 'src/main.py').write_text('print(4)\n')
        (root / 'README.md').write_text('환경 Python 3.13, 의존성 없음, 실행 python src/main.py, 테스트 pytest -q')
        (root / 'run.log').write_text('4\n')
        (root / '.reverse').mkdir()

        def write_report(run, replica_out):
            original = {'stdout': '4\n', 'stderr': '', 'exit_code': 0}
            replica = {'stdout': replica_out, 'stderr': '', 'exit_code': 0}
            raw = {'schema_version': 2, 'target_type': 'cli', 'run_id': str(run.id), 'complete': True,
                   'input': {'argv': ['double', '2']}, 'pre_state': {}, 'environment': {'os': 'test'},
                   'original': original, 'replica': replica}
            raw['execution_log'] = [{'side': side, **raw[key]} for side, key in
                                    (('original', 'original'), ('replica', 'replica'))]
            path = root / '.reverse/comparisons/repair.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(raw).encode()
            path.write_bytes(encoded)
            report = {'summary': 'CLI 계산', 'source_files': ['src/main.py'],
                'observations': [{'finding': '2 → 4', 'evidence': '사용자 설명'}],
                'checks': [{'command': 'python src/main.py', 'exit_code': 0, 'log': 'run.log'}],
                'comparisons': [{'id': 'double-2', 'behavior': '2를 두 배로 출력', 'target_type': 'cli',
                    'input': raw['input'], 'pre_state': {}, 'original': original, 'replica': replica,
                    'environment': {'os': 'test'}, 'evidence_level': 'direct',
                    'evidence': [{'kind': 'execution', 'path': '.reverse/comparisons/repair.json',
                                  'sha256': hashlib.sha256(encoded).hexdigest()}],
                    'verified_at': '2026-11-07T00:00:00Z', 'run_id': str(run.id),
                    'result': 'match', 'difference': None}], 'limitations': []}
            (root / '.reverse' / f'{run.id}.json').write_text(json.dumps(report))

        impl.status = RunStatus.succeeded
        write_report(impl, '5\n')
        await reverse_tasks.on_finished(db, impl)
        repair = await db.scalar(select(Run).where(Run.session_id == job.session_id).order_by(Run.created_at.desc()))
        assert repair.usage['reverse']['role'] == 'repair'
        assert '결함 중심 수정 요청' in repair.prompt
        assert job.result['latest']['direct_comparison']['status'] == 'mismatch'
        repair.status = RunStatus.succeeded
        write_report(repair, '5\n')
        verifier_id = await reverse_tasks.on_finished(db, repair)
        assert verifier_id == job.verifier_session_id
        assert job.result['latest']['direct_comparison']['status'] == 'mismatch'
        assert job.result['job']['repair_attempts'] == 1
        assert job.result['job']['repair_attempts'] == job.result['job']['max_repair_attempts']
        assert job.result['history'][0]['direct_comparison']['status'] == 'mismatch'


async def test_verifier_pass_without_direct_comparison_cannot_mark_done(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    response = await client.post('/api/reverse', headers=auth, json={'description': '숫자 두 배 CLI 재현', 'mode': 'reconstruct'})
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(response.json()['id']))
        impl = await db.scalar(select(Run).where(Run.session_id == job.session_id))
        root = __import__('pathlib').Path(job.output_path)
        (root / 'src').mkdir(parents=True)
        (root / '.reverse').mkdir()
        (root / 'src/main.py').write_text('print(4)\n')
        (root / 'README.md').write_text('환경 Python 3.13, 의존성 없음, 실행 python src/main.py, 테스트 pytest -q')
        (root / 'ok.log').write_text('passed\n')
        report = {'summary': '구현 보고', 'source_files': ['src/main.py'],
                  'observations': [{'finding': '2 → 4', 'evidence': '사용자 설명'}],
                  'checks': [{'command': 'pytest', 'exit_code': 0, 'log': 'ok.log'}], 'comparisons': []}
        impl.status = RunStatus.succeeded
        (root / '.reverse' / f'{impl.id}.json').write_text(json.dumps(report))
        await reverse_tasks.on_finished(db, impl)
        verifier = await db.get(ChatSession, job.verifier_session_id)
        review = await db.scalar(select(Run).where(Run.session_id == verifier.id))
        review.status = RunStatus.succeeded
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps({**report, 'verdict': 'passed'}))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'partial'
        assert job.result['latest']['direct_comparison']['count'] == 0
        assert job.result['latest']['independent_verification']['status'] == 'failed'


async def test_verifier_mismatch_queues_bounded_repair_then_fresh_verification(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    response = await client.post('/api/reverse', headers=auth, json={
        'description': 'CLI 숫자 변환 동작을 재현해줘.', 'mode': 'reconstruct', 'target_type': 'cli',
        'max_repair_attempts': 1})
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(response.json()['id']))
        root = __import__('pathlib').Path(job.output_path)
        (root / 'src').mkdir(parents=True)
        (root / '.reverse').mkdir()
        (root / 'src/main.py').write_text('print("4")\n')
        (root / 'README.md').write_text('환경 Python, 의존성 없음, 실행 python src/main.py, 테스트 pytest -q')
        (root / 'ok.log').write_text('passed\n')

        def write_report(run, replica='4\n', verdict=None):
            original = {'stdout': '4\n', 'stderr': '', 'exit_code': 0}
            replica_output = {'stdout': replica, 'stderr': '', 'exit_code': 0}
            raw = {'schema_version': 2, 'target_type': 'cli', 'run_id': str(run.id), 'complete': True,
                   'input': {'argv': ['convert', '2']}, 'pre_state': {}, 'environment': {'os': 'test'},
                   'original': original, 'replica': replica_output}
            raw['execution_log'] = [{'side': side, **raw[key]} for side, key in
                                    (('original', 'original'), ('replica', 'replica'))]
            raw_path = root / f'.reverse/comparisons/{run.id}.json'
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(raw).encode()
            raw_path.write_bytes(encoded)
            report = {'summary': '숫자 변환', 'source_files': ['src/main.py'],
                'observations': [{'finding': '2 입력은 4 출력', 'evidence': 'evidence/original.txt', 'level': 'direct'}],
                'checks': [{'command': 'pytest -q', 'exit_code': 0, 'log': 'ok.log'}],
                'comparisons': [{'id': 'convert-2', 'behavior': '2 변환', 'target_type': 'cli',
                    'input': raw['input'], 'pre_state': {}, 'original': original, 'replica': replica_output,
                    'environment': raw['environment'], 'evidence_level': 'direct',
                    'evidence': [{'kind': 'execution', 'path': raw_path.relative_to(root).as_posix(),
                                  'sha256': hashlib.sha256(encoded).hexdigest()}],
                    'verified_at': '2026-11-07T00:00:00Z', 'run_id': str(run.id),
                    'result': 'match', 'difference': None}], 'limitations': []}
            if verdict:
                report['verdict'] = verdict
            (root / '.reverse' / f'{run.id}.json').write_text(json.dumps(report))

        impl = await db.scalar(select(Run).where(Run.session_id == job.session_id))
        impl.status = RunStatus.succeeded
        write_report(impl)
        await reverse_tasks.on_finished(db, impl)
        verifier = await db.get(ChatSession, job.verifier_session_id)
        review = await db.scalar(select(Run).where(Run.session_id == verifier.id))
        review.status = RunStatus.succeeded
        write_report(review, replica='5\n', verdict='failed')
        repair_session_id = await reverse_tasks.on_finished(db, review)
        assert repair_session_id == job.session_id
        assert job.result['job']['repair_attempts'] == 1
        assert job.result['latest']['verification_direct_comparison']['status'] == 'mismatch'
        assert job.result['latest']['verification_comparisons'][0]['result'] == 'mismatch'
        repair = await db.scalar(select(Run).where(Run.session_id == job.session_id).order_by(Run.created_at.desc()))
        assert repair.usage['reverse']['role'] == 'repair'
        assert '독립 검증 결함 중심 수정 요청' in repair.prompt
        assert job.verifier_session_id is None
        assert await reverse_tasks.on_finished(db, review) is None  # 늦은 verifier callback 무시

        repair.status = RunStatus.succeeded
        write_report(repair)
        next_verifier_id = await reverse_tasks.on_finished(db, repair)
        assert next_verifier_id is not None and next_verifier_id != verifier.id
        verifier2 = await db.get(ChatSession, next_verifier_id)
        review2 = await db.scalar(select(Run).where(Run.session_id == verifier2.id))
        review2.status = RunStatus.succeeded
        write_report(review2, verdict='passed')
        await reverse_tasks.on_finished(db, review2)
        assert job.status == 'done'
        assert job.result['job']['repair_attempts'] == job.result['job']['max_repair_attempts'] == 1


async def test_cancelled_verifier_is_partial_and_retry_resumes_with_history(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    response = await client.post('/api/reverse', headers=auth, json={
        'description': '정수 입력에 따른 CLI 출력 동작을 재현해줘.', 'mode': 'reconstruct'})
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(response.json()['id']))
        root = __import__('pathlib').Path(job.output_path)
        (root / 'src').mkdir(parents=True)
        (root / 'src/main.py').write_text('print(4)\n')
        (root / 'README.md').write_text('환경 Python, 의존성 없음, 실행 python src/main.py, 테스트 pytest -q')
        (root / 'ok.log').write_text('passed\n')
        impl = await db.scalar(select(Run).where(Run.session_id == job.session_id))
        impl.status = RunStatus.succeeded
        (root / '.reverse').mkdir(parents=True)
        (root / '.reverse' / f'{impl.id}.json').write_text(json.dumps({
            'summary': 'CLI 정수 출력', 'source_files': ['src/main.py'],
            'observations': [{'finding': '입력 2에서 4 출력', 'evidence': 'evidence/case.txt', 'level': 'direct'}],
            'checks': [{'command': 'pytest -q', 'exit_code': 0, 'log': 'ok.log'}]}))
        await reverse_tasks.on_finished(db, impl)
        verifier = await db.get(ChatSession, job.verifier_session_id)
        review = await db.scalar(select(Run).where(Run.session_id == verifier.id))
        review.status = RunStatus.cancelled
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'partial'
        assert job.result['latest']['independent_verification']['status'] == 'cancelled'
        await db.commit()
    retried = await client.post('/api/reverse/' + response.json()['id'] + '/retry', headers=auth)
    assert retried.status_code == 202, retried.text
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(response.json()['id']))
        assert job.result['latest'] is None
        assert job.result['history'][-1]['run_id'] == str(impl.id)
        resumed = await db.scalar(select(Run).where(Run.session_id == job.session_id).order_by(Run.created_at.desc()))
        assert resumed.id != impl.id and resumed.status == RunStatus.queued
        assert resumed.usage['reverse']['role'] == 'implement'
        assert job.verifier_session_id is None


async def test_workspace_validation_and_description_limit(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    unknown = await client.post('/api/reverse', headers=auth, json={'description': '화면의 동작을 재현해서 소스로 만들어줘.', 'workspace_id': str(uuid.uuid4())})
    assert unknown.status_code == 404
    text = '대상 동작을 자세하게 설명합니다.' * 100
    good = await client.post('/api/reverse', headers=auth, json={'description': text})
    assert good.status_code == 202, good.text


async def test_real_manager_dispatches_implementation_and_verification(client, auth, make_git_dir, monkeypatch):
    """실제 DB/Run manager/프로세스/아티팩트 수집. 모델만 결정적 테스트 실행물이다."""
    import asyncio
    from pathlib import Path

    from ttakkari.config import get_settings
    from ttakkari.models import EngineAccount
    folder = make_git_dir()
    fake = folder / 'fake_reconstruct.py'
    fake.write_text('''#!/usr/bin/env python3
import json, re, subprocess, sys, uuid
from pathlib import Path
args=sys.argv[1:]
is_claude='exec' not in args  # codex 는 -p <프로파일> 도 쓴다(SEC2-AGT-01)
prompt=args[args.index('-p')+1] if is_claude else ' '.join(args[args.index('--')+1:])
root=Path(re.search(r'^작업 폴더: (.+)$',prompt,re.M).group(1))
report_path=Path(re.search(r'최종 기록은 (.+?\\.json)',prompt).group(1))
(root/'src').mkdir(parents=True,exist_ok=True)
(root/'evidence').mkdir(exist_ok=True)
(root/'.reverse').mkdir(exist_ok=True)
(root/'src/main.py').write_text('import sys\\nprint(int(sys.argv[1])*2)\\n')
(root/'README.md').write_text('환경: Python 3.13\\n의존성: 없음\\n실행: python src/main.py\\n테스트: pytest -q\\n')
check=subprocess.run([sys.executable,str(root/'src/main.py'),'3'],capture_output=True,text=True)
assert check.stdout.strip()=='6'
(root/'evidence/test.log').write_text(check.stdout)
(root/'.reverse/comparisons').mkdir(parents=True,exist_ok=True)
raw={'schema_version':2,'target_type':'cli','run_id':str(report_path.stem),'complete':True,'input':{'argv':['double','3']},'pre_state':{},'environment':{'os':'test'},'original':{'stdout':'6\\n','stderr':'','exit_code':0},'replica':{'stdout':check.stdout,'stderr':check.stderr,'exit_code':check.returncode}}
raw['execution_log']=[dict(side=side,**raw[key]) for side,key in [('original','original'),('replica','replica')]]
encoded=json.dumps(raw).encode(); raw_path=root/'.reverse/comparisons/double.json'; raw_path.write_bytes(encoded)
report={'summary':'두 배 계산 재현','source_files':['src/main.py'],'observations':[{'finding':'3 → 6','evidence':'사용자 설명'}],'checks':[{'command':'python src/main.py 3','exit_code':check.returncode,'log':'evidence/test.log'}],'limitations':[], 'comparisons':[{'id':'double','behavior':'두 배 출력','target_type':'cli','input':raw['input'],'pre_state':{},'original':raw['original'],'replica':raw['replica'],'environment':{'os':'test'},'evidence_level':'direct','evidence':[{'kind':'execution','path':'.reverse/comparisons/double.json','sha256':__import__('hashlib').sha256(encoded).hexdigest()}],'verified_at':'2026-11-07T00:00:00Z','run_id':str(report_path.stem),'result':'match','difference':None}], 'verdict':'passed'}
report_path.write_text(json.dumps(report))
sid=str(uuid.uuid4())
def emit(data): print(json.dumps(data),flush=True)
if is_claude:
 emit({'type':'system','subtype':'init','session_id':sid})
 emit({'type':'result','subtype':'success','is_error':False,'result':'실제 소스 실행: 3 → 6','session_id':sid})
else:
 emit({'type':'thread.started','thread_id':sid})
 emit({'type':'item.completed','item':{'id':'m','type':'agent_message','text':'독립 실행 검증: 3 → 6'}})
 emit({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':10}})
''')
    fake.chmod(0o755)
    monkeypatch.setattr(get_settings(), 'claude_bin', str(fake))
    monkeypatch.setattr(get_settings(), 'codex_bin', str(fake))
    async with sessionmaker()() as db:
        db.add_all([EngineAccount(name='reconstruct-claude',engine='claude'),
                    EngineAccount(name='reconstruct-codex',engine='codex')])
        await db.commit()
    res = await client.post('/api/reverse', headers=auth, json={'description': '숫자를 넣으면 2배를 돌려주는 소스 없는 프로그램을, 예시 3→6을 근거로 소스로 재현해줘.'})
    assert res.status_code == 202, res.text
    data = None
    for _ in range(100):
        data = (await client.get('/api/reverse/' + res.json()['id'], headers=auth)).json()
        if data['status'] in ('done', 'failed', 'partial'):
            break
        await asyncio.sleep(.1)
    assert data['status'] == 'done', f"{data.get('error')} {data.get('result_state')} {data.get('reconstruction')}"
    assert len(data['rounds']) == 2
    assert {r['engine'] for r in data['rounds']} == {'claude', 'codex'}
    assert Path(data['output_path'], 'src/main.py').is_file()
    assert any(a['filename'] == 'reconstruction-source.zip' for a in data['outputs'])
    assert data['result_state']['independent_verification']['status'] == 'passed'


def test_validate_report_checks_tolerance(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/main.py').write_text('print(1)\n')
    (tmp_path / 'README.md').write_text('환경 Python 3.13, 의존성 없음, 실행 python src/main.py, 테스트 pytest -q')
    (tmp_path / 'ok.log').write_text('passed\n')
    (tmp_path / 'fail.log').write_text('failed\n')

    base = {
        'summary': '테스트',
        'source_files': ['src/main.py'],
        'observations': [{'finding': '관찰', 'evidence': '근거'}],
        'limitations': [],
    }

    # 필수 검사 성공 + 사유를 명시한 선택적 환경 진단 실패는 함께 보존한다.
    report_mixed = {
        **base,
        'checks': [
            {'command': 'pytest ok', 'exit_code': 0, 'log': 'ok.log'},
            {'command': 'python -m pytest', 'exit_code': 1, 'log': 'fail.log', 'required': False,
             'reason': '기본 Python에 pytest가 없어 설치된 환경에서 필수 전체 검사를 실행했다.'},
        ],
        'verdict': 'passed',
    }
    validated = reverse_tasks.validate_report(tmp_path, report_mixed)
    assert validated['ready'] is True
    assert len(validated['checks']) == 2
    assert validated['problems'] == []

    # 실패 로그만 있지만 verdict=failed인 경우 유효
    report_failed = {
        **base,
        'checks': [{'command': 'pytest fail', 'exit_code': 1, 'log': 'fail.log'}],
        'verdict': 'failed',
    }
    validated = reverse_tasks.validate_report(tmp_path, report_failed)
    assert validated['ready'] is True

    # 실패 로그만 있는데 verdict=passed인 경우 거부
    report_unearned_pass = {
        **base,
        'checks': [{'command': 'pytest fail', 'exit_code': 1, 'log': 'fail.log'}],
        'verdict': 'passed',
    }
    validated = reverse_tasks.validate_report(tmp_path, report_unearned_pass)
    assert validated['ready'] is False
    assert '성공 종료 코드' in validated['problems'][0]

    # 로그 파일이 존재하지 않는 경우 거부
    report_missing_file = {
        **base,
        'checks': [{'command': 'pytest ok', 'exit_code': 0, 'log': 'nonexistent.log'}],
        'verdict': 'passed',
    }
    validated = reverse_tasks.validate_report(tmp_path, report_missing_file)
    assert validated['ready'] is False

