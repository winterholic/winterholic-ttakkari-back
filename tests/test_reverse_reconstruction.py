"""자연어 접수→도구 작업 세션→실제 소스→독립 검증 연결 계약."""
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
    response = await client.post('/api/reverse', headers=auth, json={'description': description})
    assert response.status_code == 202, response.text
    data = response.json()
    assert data['kind'] == 'reconstruction'
    assert data['target'] == description
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(data['id']))
        sess = await db.get(ChatSession, job.session_id)
        assert sess.engine == 'auto' and sess.worker_task_type == 'general'
        assert sess.kind == 'worker'
        run = await db.scalar(select(Run).where(Run.session_id == sess.id))
        assert run.usage['reverse']['job_id'] == str(job.id)
        assert '소스코드' in run.prompt and '관찰' in run.prompt
        assert job.output_path.startswith((await db.get(Workspace, job.workspace_id)).root_path)
    detail = (await client.get('/api/reverse/' + data['id'], headers=auth)).json()
    assert detail['session_id'] == str(sess.id)
    assert detail['result'] is None
    assert [s['key'] for s in detail['stages']] == ['request', 'observe', 'infer', 'implement', 'verify', 'deliver']


async def test_repo_only_input_does_not_start_old_clone_pipeline(client, auth):
    for body in [{'target': 'morluto/rea'}, {'description': 'https://github.com/morluto/rea'}]:
        res = await client.post('/api/reverse', headers=auth, json=body)
        assert res.status_code == 422


async def test_completion_requires_files_then_queues_independent_verification(client, auth, monkeypatch):
    async def dispatch(_):
        pass
    monkeypatch.setattr(manager, 'dispatch', dispatch)
    res = await client.post('/api/reverse', headers=auth, json={'description': '입력한 숫자의 두 배를 출력하는 프로그램을 동작 기준으로 재현해줘.'})
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
        (root / 'test.log').write_text('input 2 -> 4, input -1 -> -2\n')
        report = {'summary': '두 배 CLI', 'source_files': ['src/main.py'],
                  'observations': [{'finding': '2 -> 4', 'evidence': '사용자 설명'}],
                  'checks': [{'command': 'python src/main.py', 'exit_code': 0, 'log': 'test.log'}], 'limitations': []}
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
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps({**report, 'verdict': 'passed'}))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'done'
        # 정상 검사 하나가 있어도 실패한 필수 회귀 검사는 완료 판정을 막는다.
        failed = {**report, 'verdict': 'passed', 'checks': [*report['checks'],
                  {'command': 'pytest failing_regression', 'exit_code': 1, 'log': 'test.log'}]}
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps(failed))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'partial'
        assert job.result['verification'] != 'passed'
        (root / '.reverse' / f'{review.id}.json').write_text(json.dumps({**report, 'verdict': 'passed'}))
        await reverse_tasks.on_finished(db, review)
        assert job.status == 'done'
        await db.commit()
    data = (await client.get('/api/reverse/' + res.json()['id'], headers=auth)).json()
    assert data['reconstruction']['source_files'] == ['src/main.py']
    assert data['reconstruction']['verification'] == 'passed'
    followup = await client.post('/api/reverse/' + res.json()['id'] + '/messages', headers=auth,
                                json={'prompt': '소수 계산도 재현해줘.'})
    assert followup.status_code == 202, followup.text
    history = (await client.get('/api/reverse/' + res.json()['id'], headers=auth)).json()
    assert len(history['rounds']) == 3
    assert any(r['role'] == 'verify' for r in history['rounds'])
    assert '소수 계산도 재현해줘.' in history['target']


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
check=subprocess.run([sys.executable,str(root/'src/main.py'),'3'],capture_output=True,text=True)
assert check.stdout.strip()=='6'
(root/'evidence/test.log').write_text(check.stdout)
report={'summary':'두 배 계산 재현','source_files':['src/main.py'],'observations':[{'finding':'3 → 6','evidence':'사용자 설명'}],'checks':[{'command':'python src/main.py 3','exit_code':check.returncode,'log':'evidence/test.log'}],'limitations':[],'verdict':'passed'}
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
    assert data['status'] == 'done', data
    assert len(data['rounds']) == 2
    assert {r['engine'] for r in data['rounds']} == {'claude', 'codex'}
    assert Path(data['output_path'], 'src/main.py').is_file()
    assert any(a['filename'] == 'reconstruction-source.zip' for a in data['outputs'])
    assert data['reconstruction']['verification'] == 'passed'


def test_validate_report_checks_tolerance(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/main.py').write_text('print(1)\n')
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

