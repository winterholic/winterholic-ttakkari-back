"""자연어 역분석은 관찰 증거와 작업공간의 재현 소스로 닫혀야 한다."""
import hashlib
import json
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ttakkari import reverse
from ttakkari import reverse_tasks as tasks
from ttakkari.reverse import ReverseError


def test_description_keeps_multiline_behaviour_and_optional_url():
    text = '계산기 앱을 재현해 줘.\n12 + 3을 입력하면 15가 보여. 참고 https://example.org'
    assert tasks.clean_description(text) == text
    for value in ['', '  ', 'morluto/rea', 'https://example.org', '/tmp/program']:
        with pytest.raises(ReverseError):
            tasks.clean_description(value)


def report(root, **overrides):
    (root / 'src').mkdir(exist_ok=True)
    (root / 'src/main.py').write_text('print(15)\n')
    (root / 'README.md').write_text('환경: Python 3.13\n의존성: 없음\n실행: python src/main.py\n테스트: pytest -q\n')
    (root / 'check.log').write_text('1 passed\n')
    data = {'summary': '덧셈 재현', 'source_files': ['src/main.py'],
            'observations': [{'finding': '12 + 3 = 15', 'evidence': '사용자 설명'}],
            'checks': [{'command': 'python -m pytest', 'exit_code': 0, 'log': 'check.log'}],
            'limitations': []}
    return {**data, **overrides}


@pytest.mark.asyncio
async def test_start_analysis_forwards_caller_pool_and_v2_options(monkeypatch):
    captured = {}

    async def fake_start(description, db, workspace_id, **options):
        captured.update(description=description, db=db, workspace_id=workspace_id, options=options)
        return {"id": "job"}

    monkeypatch.setattr(tasks, "start", fake_start)
    workspace_id = uuid4()
    result = await reverse.start_analysis(
        "설명", object(), workspace_id, pool="work", title="작업", target_type="api",
        mode="analyze", references=["fixture:reference"], max_repair_attempts=2,
    )

    assert result == {"id": "job"}
    assert captured["options"] == {
        "pool": "work", "title": "작업", "target_type": "api", "mode": "analyze",
        "references": ["fixture:reference"], "max_repair_attempts": 2,
    }


def test_source_and_observation_and_executed_check_are_required(tmp_path):
    data = report(tmp_path)
    assert tasks.validate_report(tmp_path, data)['ready'] is True
    for field in ['source_files', 'observations', 'checks']:
        assert tasks.validate_report(tmp_path, {**data, field: []})['ready'] is False
    data['checks'][0]['exit_code'] = 1
    assert tasks.validate_report(tmp_path, data)['ready'] is False


def test_reports_never_accept_sources_or_logs_outside_job(tmp_path):
    root = tmp_path / 'job'
    root.mkdir()
    outside = tmp_path / 'outside.py'
    outside.write_text('print(1)')
    (root / 'link.py').symlink_to(outside)
    for path in ['../outside.py', str(outside), 'link.py', '.env']:
        assert tasks.validate_report(root, report(root, source_files=[path]))['ready'] is False
    data = report(root)
    data['checks'][0]['log'] = '../outside.py'
    assert tasks.validate_report(root, data)['ready'] is False


def test_document_only_and_empty_sources_are_not_implementation(tmp_path):
    (tmp_path / 'README.md').write_text('설계만 했습니다')
    assert tasks.validate_report(tmp_path, report(tmp_path, source_files=['README.md']))['ready'] is False
    (tmp_path / 'src/main.py').write_text('')
    assert tasks.validate_report(tmp_path, report(tmp_path, source_files=['missing.py']))['ready'] is False


def test_manifest_reader_uses_this_run_and_handles_invalid_json(tmp_path):
    path = tmp_path / '.reverse'
    path.mkdir()
    (path / 'old.json').write_text(json.dumps(report(tmp_path)))
    assert tasks.read_report(tmp_path, 'new')['ready'] is False
    (path / 'new.json').write_text('{')
    assert tasks.read_report(tmp_path, 'new')['ready'] is False


@pytest.mark.asyncio
async def test_successful_verifier_callback_is_idempotent(monkeypatch):
    from ttakkari.models import RunStatus

    verifier_session_id, implementation_session_id = uuid4(), uuid4()
    job_id, verifier_run_id = uuid4(), uuid4()
    implementation = SimpleNamespace(id=uuid4(), usage={})
    verifier = SimpleNamespace(id=verifier_run_id, session_id=verifier_session_id,
        status=RunStatus.succeeded, error=None,
        usage={'reverse': {'job_id': str(job_id), 'role': 'verify'}})
    previous = {'run_id': str(implementation.id), 'role': 'implement',
        'source': {'status': 'verified', 'files': ['src/main.py']},
        'execution': {'status': 'verified', 'checks': [{'command': 'pytest', 'exit_code': 0}]},
        'report': {'ready': True, 'observations': [{'finding': 'observed'}]},
        'implementation_status': 'passed',
        'direct_comparison': {'status': 'match', 'count': 1, 'matched': 1, 'mismatched': 0, 'unverified': 0},
        'independent_verification': {'status': 'pending', 'run_id': None, 'verdict': None}}
    job = SimpleNamespace(id=job_id, kind=tasks.KIND, session_id=implementation_session_id,
        verifier_session_id=verifier_session_id, status='running', error=None, updated_at=None,
        output_path='.',
        result={'schema_version': 2, 'job': {'repair_attempts': 0, 'max_repair_attempts': 1},
                'latest': previous, 'history': []})

    class FakeDb:
        def __init__(self):
            self.latest_lookup = 0
            self.runs = [implementation, verifier]

        async def get(self, _model, _id, **_kwargs):
            return job

        async def scalar(self, _statement):
            self.latest_lookup += 1
            return verifier.id if self.latest_lookup % 2 else implementation

    db = FakeDb()
    reads = []
    monkeypatch.setattr(tasks, 'read_report', lambda *_args, **_kwargs: reads.append(verifier.id) or {
        'ready': True, 'verdict': 'passed', 'source_files': ['src/main.py'],
        'observations': [{'finding': 'observed'}], 'checks': [], 'execution_ready': True,
        'comparisons': [{'result': 'match'}],
        'direct_comparison': {'status': 'match', 'count': 1, 'matched': 1, 'mismatched': 0, 'unverified': 0},
        'problems': []})

    await tasks.on_finished(db, verifier)
    assert job.status == 'done'
    history_count = len(job.result['history'])
    round_count = len(db.runs)
    repair_attempts = job.result['job']['repair_attempts']
    await tasks.on_finished(db, verifier)
    assert len(job.result['history']) == history_count
    assert len(db.runs) == round_count
    assert job.result['job']['repair_attempts'] == repair_attempts
    assert len(reads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_type", "implementation_direct", "implementation_visual", "expected"),
    [
        ("web", {"status": "match", "count": 1, "matched": 1, "mismatched": 0, "unverified": 0},
         {"status": "mismatch", "count": 1, "matched": 0, "mismatched": 1, "unverified": 0}, "partial"),
        ("web", {"status": "unverified", "count": 1, "matched": 0, "mismatched": 0, "unverified": 1},
         {"status": "unverified", "count": 1, "matched": 0, "mismatched": 0, "unverified": 1}, "partial"),
        ("cli", {"status": "unverified", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0},
         {"status": "not_required", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0}, "partial"),
        ("api", {"status": "unverified", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0},
         {"status": "not_required", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0}, "partial"),
        ("cli", {"status": "mismatch", "count": 1, "matched": 0, "mismatched": 1, "unverified": 0},
         {"status": "not_required", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0}, "partial"),
        ("api", {"status": "mismatch", "count": 1, "matched": 0, "mismatched": 1, "unverified": 0},
         {"status": "not_required", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0}, "partial"),
        ("cli", {"status": "match", "count": 1, "matched": 1, "mismatched": 0, "unverified": 0},
         {"status": "not_required", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0}, "done"),
    ],
)
async def test_verifier_completion_requires_implementation_evidence_and_keeps_role_reports(
        monkeypatch, target_type, implementation_direct, implementation_visual, expected):
    from ttakkari.models import ReverseJob, Run, RunStatus

    job_id, impl_session_id, verify_session_id = uuid4(), uuid4(), uuid4()
    impl_run = SimpleNamespace(id=uuid4(), session_id=impl_session_id, status=RunStatus.succeeded,
                               error=None, usage={"reverse": {"job_id": str(job_id), "role": "implement"}})
    verify_run = SimpleNamespace(id=uuid4(), session_id=verify_session_id, status=RunStatus.succeeded,
                                 error=None, usage={"reverse": {"job_id": str(job_id), "role": "verify"}})
    job = SimpleNamespace(id=job_id, kind=tasks.KIND, target="reproduce target", workspace_id=uuid4(), session_id=impl_session_id,
                          verifier_session_id=verify_session_id, status="running", error=None, updated_at=None,
                          output_path=".", result={"schema_version": 2,
                              "job": {"mode": "reconstruct", "target_type": target_type,
                                      "repair_attempts": 0, "max_repair_attempts": 0},
                              "latest": None, "history": []})
    sessions = {impl_session_id: SimpleNamespace(id=impl_session_id, workspace_id=uuid4(), pool="work"),
                verify_session_id: SimpleNamespace(id=verify_session_id, workspace_id=uuid4(), pool="work")}

    class Query:
        def __init__(self, target):
            self.target = target

        def where(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, *_args):
            return self

    class FakeDb:
        def __init__(self):
            self.latest_run_id = impl_run.id
            self.added = []

        async def get(self, model, identity, **_kwargs):
            return job if model is ReverseJob else sessions.get(identity)

        async def scalar(self, query):
            if query.target is Run.id:
                return self.latest_run_id
            if query.target is Run:
                return impl_run
            raise AssertionError(query.target)

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            for value in self.added:
                if isinstance(value, Run) and value.id is None:
                    value.id = uuid4()

    ready_report = {
        "ready": True, "analysis_ready": False, "execution_ready": True, "verdict": "passed",
        "source_files": ["src/main.py"], "observations": [{"finding": "observed", "evidence": "log"}],
        "checks": [{"command": "pytest", "exit_code": 0, "log": "check.log"}],
        "problems": [], "summary": "role-specific report",
        "comparisons": [{"result": "match"}],
        "direct_comparison": {"status": "match", "count": 1, "matched": 1, "mismatched": 0, "unverified": 0},
        "visual_comparison": {"status": "match" if target_type == "web" else "not_required",
                              "count": 1 if target_type == "web" else 0,
                              "matched": 1 if target_type == "web" else 0,
                              "mismatched": 0, "unverified": 0},
    }
    impl_report = {**ready_report, "summary": "implementation report",
                   "direct_comparison": implementation_direct,
                   "visual_comparison": implementation_visual,
                   "comparisons": [] if implementation_direct["count"] == 0 else [{"result": "mismatch"}]}
    verifier_report = {**ready_report, "summary": "verifier report"}
    reports = {str(impl_run.id): impl_report, str(verify_run.id): verifier_report}
    monkeypatch.setattr(tasks, "select", Query)
    monkeypatch.setattr(tasks, "read_report", lambda _root, run_id, **_kwargs: reports[run_id])
    db = FakeDb()

    await tasks.on_finished(db, impl_run)
    latest = tasks._metadata(job.result)["latest"]
    assert job.status == "running"
    assert latest["report"]["summary"] == "implementation report"
    db.latest_run_id = verify_run.id
    await tasks.on_finished(db, verify_run)

    latest = tasks._metadata(job.result)["latest"]
    assert job.status == expected
    assert latest["report"]["summary"] == "implementation report"
    assert latest["verification_report"]["summary"] == "verifier report"
    assert latest["verification_checks"] == verifier_report["checks"]
    assert latest["verification_problems"] == []
    assert latest["implementation_status"] == ("passed" if expected == "done" else "failed")
    assert latest["independent_verification"]["status"] == "passed"


@pytest.mark.asyncio
async def test_repair_that_keeps_implementation_mismatch_finishes_partial_and_duplicate_is_noop(monkeypatch):
    from ttakkari.models import ChatSession, ReverseJob, Run, RunStatus

    job_id, session_id = uuid4(), uuid4()
    implementation = SimpleNamespace(id=uuid4(), session_id=session_id, status=RunStatus.succeeded,
        error=None, usage={"reverse": {"job_id": str(job_id), "role": "implement"}})
    job = SimpleNamespace(id=job_id, kind=tasks.KIND, target="reproduce target", workspace_id=uuid4(), session_id=session_id, verifier_session_id=None,
        status="running", error=None, updated_at=None, output_path=".",
        result={"schema_version": 2, "job": {"mode": "reconstruct", "target_type": "web",
                "repair_attempts": 0, "max_repair_attempts": 1}, "latest": None, "history": []})
    session = SimpleNamespace(id=session_id, workspace_id=uuid4(), pool="work")

    class Query:
        def __init__(self, target): self.target = target
        def where(self, *_args): return self
        def order_by(self, *_args): return self
        def limit(self, *_args): return self

    class FakeDb:
        def __init__(self):
            self.latest_run_id = implementation.id
            self.added = []

        async def get(self, model, _identity, **_kwargs): return job if model is ReverseJob else session
        async def scalar(self, query):
            if query.target is Run.id: return self.latest_run_id
            if query.target is Run: return implementation
            raise AssertionError(query.target)
        def add(self, value): self.added.append(value)
        async def flush(self):
            for value in self.added:
                if isinstance(value, (Run, ChatSession)) and value.id is None: value.id = uuid4()
                if isinstance(value, Run) and value.last_seq is None: value.last_seq = 0

    bad = {"ready": True, "verdict": "passed", "source_files": ["src/main.py"],
        "observations": [{"finding": "observed", "evidence": "log"}], "execution_ready": True,
        "checks": [{"command": "pytest", "exit_code": 0, "log": "check.log"}], "problems": [],
        "comparisons": [{"result": "mismatch"}],
        "direct_comparison": {"status": "match", "count": 1, "matched": 1, "mismatched": 0, "unverified": 0},
        "visual_comparison": {"status": "mismatch", "count": 1, "matched": 0, "mismatched": 1, "unverified": 0}}
    good = {**bad, "summary": "verifier", "comparisons": [{"result": "match"}],
        "direct_comparison": {"status": "match", "count": 1, "matched": 1, "mismatched": 0, "unverified": 0},
        "visual_comparison": {"status": "match", "count": 1, "matched": 1, "mismatched": 0, "unverified": 0}}
    reports = {str(implementation.id): bad}
    monkeypatch.setattr(tasks, "select", Query)
    monkeypatch.setattr(tasks, "read_report", lambda _root, run_id, **_kwargs: reports[run_id])
    db = FakeDb()

    await tasks.on_finished(db, implementation)
    repair = next(item for item in db.added if isinstance(item, Run))
    assert tasks._metadata(job.result)["job"]["repair_attempts"] == 1
    before_duplicate, rows_before_duplicate = job.result.copy(), len(db.added)
    await tasks.on_finished(db, implementation)
    assert job.result == before_duplicate
    assert len(db.added) == rows_before_duplicate
    repair.status = RunStatus.succeeded
    repair.usage = {"reverse": {"job_id": str(job.id), "role": "repair"}}
    db.latest_run_id = repair.id
    reports[str(repair.id)] = bad
    await tasks.on_finished(db, repair)
    verifier = next(item for item in db.added if isinstance(item, Run) and item.usage["reverse"]["role"] == "verify")
    verifier.status = RunStatus.succeeded
    verifier.error = None
    db.latest_run_id = verifier.id
    reports[str(verifier.id)] = good
    await tasks.on_finished(db, verifier)
    assert job.status == "partial", job.error
    assert tasks._metadata(job.result)["job"]["repair_attempts"] == 1
    assert "visual_comparison" in repair.prompt
    assert "implementation_problems" in repair.prompt
    assert "implementation_report" in repair.prompt
    before, status_before, row_count = job.result.copy(), job.status, len(db.added)
    await tasks.on_finished(db, verifier)
    assert job.result == before
    assert job.status == status_before
    assert len(db.added) == row_count


@pytest.mark.asyncio
async def test_analyze_mode_completes_without_reconstruction_gate(monkeypatch):
    from ttakkari.models import RunStatus

    job_id, session_id, run_id = uuid4(), uuid4(), uuid4()
    run = SimpleNamespace(id=run_id, session_id=session_id, status=RunStatus.succeeded, error=None,
        usage={"reverse": {"job_id": str(job_id), "role": "implement"}})
    job = SimpleNamespace(id=job_id, kind=tasks.KIND, target="analyze target", session_id=session_id, verifier_session_id=None,
        status="running", error=None, updated_at=None, output_path=".",
        result={"schema_version": 2, "job": {"mode": "analyze", "target_type": "cli",
                "repair_attempts": 0, "max_repair_attempts": 0}, "latest": None, "history": []})

    class Query:
        def __init__(self, target): self.target = target
        def where(self, *_args): return self
        def order_by(self, *_args): return self
        def limit(self, *_args): return self

    class FakeDb:
        async def get(self, _model, _identity, **_kwargs): return job
        async def scalar(self, _query): return run.id

    analysis = {"ready": False, "analysis_ready": True, "verdict": "passed", "summary": "analysis",
        "source_files": [], "observations": [{"finding": "observed", "evidence": "source"}],
        "checks": [], "problems": [], "comparisons": [],
        "direct_comparison": {"status": "unverified", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0},
        "visual_comparison": {"status": "not_required", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0}}
    monkeypatch.setattr(tasks, "select", Query)
    monkeypatch.setattr(tasks, "read_report", lambda *_args, **_kwargs: analysis)

    await tasks.on_finished(FakeDb(), run)

    assert job.status == "done"
    metadata = tasks._metadata(job.result)
    assert metadata["latest"]["independent_verification"]["status"] == "not_required"
    assert metadata["latest"]["implementation_status"] == "not_required"
    assert metadata["history"] == []


def test_zip_contains_sources_but_excludes_target_and_secrets(tmp_path):
    root = tmp_path / 'job'
    root.mkdir()
    data = report(root)
    (root / 'target.bin').write_bytes(b'original target')
    (root / '.env').write_text('DO_NOT_EXPORT')
    (root / 'README.md').write_text('python src/main.py')
    import zipfile
    outbox = tmp_path / 'outbox'
    outbox.mkdir()
    path = tasks.bundle(root, outbox, tasks.validate_report(root, data))
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {'src/main.py', 'check.log', 'README.md'}


def test_bundle_includes_comparison_manifest_raw_evidence_tests_and_safe_dependencies(tmp_path):
    root = tmp_path / 'job'
    root.mkdir()
    data = report(root)
    (root / 'requirements.txt').write_text('requests==2.0\n')
    (root / 'setup.cfg').write_text('password=embedded-secret\n')
    (root / 'tests').mkdir()
    (root / 'tests/test_cli.py').write_text('def test_cli(): pass\n')
    raw_path = root / '.reverse/comparisons/case.json'
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text('{"schema_version":2}')
    (root / '.reverse/comparisons/linked.json').symlink_to(root / 'setup.cfg')
    data['comparisons'] = [{'id': 'case', 'result': 'match', 'evidence': [
        {'kind': 'execution', 'path': '.reverse/comparisons/case.json',
         'sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest()},
        {'kind': 'execution', 'path': '.reverse/comparisons/linked.json', 'sha256': 'def'},
        {'kind': 'execution', 'path': '.reverse/comparisons/../../setup.cfg', 'sha256': 'ghi'}]}]
    path = tasks.bundle(root, tmp_path / 'outbox', tasks.validate_report(root, data))
    import zipfile
    with zipfile.ZipFile(path) as archive:
        assert {'README.md', 'requirements.txt', 'tests/test_cli.py',
                '.reverse/comparisons/case.json', 'reverse-comparison-manifest.json'} <= set(archive.namelist())
        assert '.reverse/comparisons/linked.json' not in archive.namelist()
        assert 'setup.cfg' not in archive.namelist()
        manifest = json.loads(archive.read('reverse-comparison-manifest.json'))
        assert [item['included'] for item in manifest[0]['evidence']] == [True, False, False]


def test_web_prompt_gives_role_specific_browser_capture_and_verification_commands(tmp_path):
    job = SimpleNamespace(output_path=str(tmp_path / 'job folder'), target='행동을 재현한다', result={
        'schema_version': 2, 'job': {'mode': 'reconstruct', 'target_type': 'web', 'title': 'web sample'}})
    run = SimpleNamespace(id='run-123', session_id='session-456')
    verifier_run = SimpleNamespace(id='verifier-run-789', session_id='verifier-session')

    implementation = tasks._prompt(job, run)
    verifier = tasks._prompt(job, verifier_run, review=True)

    for prompt, run_id in ((implementation, 'run-123'), (verifier, 'verifier-run-789')):
        assert 'ttakkari_browser' in prompt
        assert 'same input' in prompt.lower() or '동일한 입력' in prompt or '같은 입력' in prompt
        assert 'same viewport' in prompt.lower() or 'viewport' in prompt.lower()
        assert 'reverse_web_evidence_capture.py' in prompt
        assert 'reverse_web_evidence_verify.py' in prompt
        assert run_id in prompt
        assert 'unverified' in prompt
        assert 'needs_input' in prompt
        assert f"cd '{tmp_path / 'job folder'}'" in prompt
        assert 'mkdir -p .reverse/input .reverse/web-captures .reverse/comparisons' in prompt
        assert f'PYTHONPATH={shlex.quote(str(tasks.Path(tasks.__file__).resolve().parents[1]))}' in prompt
        assert f'{shlex.quote(sys.executable)} ' in prompt
        checkout = tasks.Path(tasks.__file__).resolve().parents[2]
        assert shlex.quote(str(checkout / 'scripts' / 'reverse_web_evidence_capture.py')) in prompt
        assert shlex.quote(str(checkout / 'scripts' / 'reverse_web_evidence_verify.py')) in prompt
    assert 'implementation' in implementation.lower() or '구현자' in implementation
    assert 'verifier' in verifier.lower() or '검증자' in verifier


@pytest.mark.parametrize('layout', ['development', 'release'])
def test_prompt_capture_and_verify_commands_run_for_both_roles_in_spaced_job_paths(tmp_path, monkeypatch, layout):
    from unit_tests.test_reverse_web_core_integration import _web_capture

    checkout = tasks.Path(tasks.__file__).resolve().parents[2]
    if layout == 'development':
        python = sys.executable
        source_root = checkout / 'src'
        script_root = checkout / 'scripts'
    else:
        release = tmp_path / 'release tree' / 'releases' / 'fixed-sha'
        source_root = release / 'src'
        script_root = release / 'scripts'
        source_root.mkdir(parents=True)
        script_root.mkdir(parents=True)
        release_venv = release / '.venv'
        release_venv.symlink_to(checkout.parent / 'back' / '.venv', target_is_directory=True)
        import shutil

        shutil.copytree(checkout / 'src' / 'ttakkari', source_root / 'ttakkari')
        shutil.copy2(checkout / 'scripts' / 'reverse_web_evidence_capture.py', script_root)
        shutil.copy2(checkout / 'scripts' / 'reverse_web_evidence_verify.py', script_root)
        python_path = release_venv / 'bin' / 'python'
        python = str(python_path)
        monkeypatch.setattr(sys, 'executable', python)
        monkeypatch.setattr(tasks, '__file__', str(source_root / 'ttakkari' / 'reverse_tasks.py'))

    for role, run_id in (('implementation', f'{layout}-impl-run'), ('verifier', f'{layout}-verify-run')):
        root = tmp_path / f'{layout} {role} job'
        root.mkdir()
        capture = _web_capture(root, run_id=run_id, comparison_id=f'{layout}-{role}')
        capture_path = root / capture['evidence'][0]['capture']['path']
        raw_capture = json.loads(capture_path.read_text(encoding='utf-8'))
        spec = {key: value for key, value in raw_capture.items()
                if key in {'comparison_id', 'behavior', 'input', 'original', 'replica', 'run_id',
                           'expected_viewport', 'origin_map', 'environment', 'pre_state', 'verified_at'}}
        cli_case_id = f'{layout}-{role}-cli'
        spec['comparison_id'] = cli_case_id
        observations = root / '.reverse/input/web-observations.json'
        observations.parent.mkdir(parents=True)
        observations.write_text(json.dumps(spec), encoding='utf-8')
        job = SimpleNamespace(output_path=str(root), target='capture test', result={
            'schema_version': 2, 'job': {'mode': 'reconstruct', 'target_type': 'web'}})
        run = SimpleNamespace(id=run_id, session_id=f'{run_id}-session')
        prompt = tasks._prompt(job, run, review=(role == 'verifier'))
        commands = [line for line in prompt.splitlines() if line.startswith('cd ') and '&& PYTHONPATH=' in line]
        assert len(commands) == 2
        assert f'PYTHONPATH={shlex.quote(str(source_root))}' in commands[0]
        assert f' {shlex.quote(python)} ' in commands[0]
        assert shlex.quote(str(script_root / 'reverse_web_evidence_capture.py')) in commands[0]
        assert shlex.quote(str(script_root / 'reverse_web_evidence_verify.py')) in commands[1]
        capture_result = subprocess.run(commands[0], shell=True, text=True, capture_output=True, timeout=15,
                                        check=False)
        assert capture_result.returncode == 0, capture_result.stderr
        verify_command = commands[1].replace('<comparison-id>', cli_case_id)
        verify_result = subprocess.run(verify_command, shell=True, text=True, capture_output=True, timeout=15,
                                       check=False)
        assert verify_result.returncode == 0, verify_result.stderr


def test_bundle_rejects_fifo_without_blocking_and_records_reason(tmp_path):
    root = tmp_path / 'job'
    root.mkdir()
    fifo = root / 'source.fifo'
    os.mkfifo(fifo)
    code = (
        'import json; from pathlib import Path; from ttakkari.reverse_tasks import _bounded_evidence_bytes; '
        f'print(json.dumps(_bounded_evidence_bytes(Path({str(root)!r}), "source.fifo", limit=1024)))'
    )
    env = {**os.environ, 'PYTHONPATH': str(tasks.Path(tasks.__file__).resolve().parents[1])}
    try:
        result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True,
                                timeout=2, env=env, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail('FIFO evidence read blocked instead of being rejected')
    assert result.returncode == 0
    assert json.loads(result.stdout) == [None, 'not a regular file']


def test_bundle_rechecks_and_archives_role_separated_web_evidence(tmp_path):
    import zipfile

    from unit_tests.test_reverse_web_core_integration import _web_capture

    root = tmp_path / 'job'
    root.mkdir()
    data = report(root)
    impl = _web_capture(root, run_id='impl-run', comparison_id='impl-case')
    verifier = _web_capture(root, run_id='verify-run', comparison_id='verify-case', state='different')
    impl_report = {**data, 'comparisons': [impl]}
    verify_report = {**data, 'comparisons': [verifier]}
    roles = [
        {'role': 'implementation', 'run_id': 'impl-run', 'session_id': 'impl-session', 'report': impl_report},
        {'role': 'verifier', 'run_id': 'verify-run', 'session_id': 'verify-session', 'report': verify_report},
    ]

    path = tasks.bundle(root, tmp_path / 'outbox', tasks.validate_report(root, impl_report),
                        evidence_roles=roles)

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read('reverse-web-evidence-manifest.json'))
        assert '.reverse/web-captures/impl-case-impl-run.json' in names
        assert '.reverse/web-captures/verify-case-verify-run.json' in names
        assert any(n.startswith('.reverse/web-captures/impl-case/') and n.endswith('.png') for n in names)
        assert any(n.startswith('.reverse/web-captures/verify-case/') and n.endswith('.png') for n in names)
        assert [item['role'] for item in manifest['runs']] == ['implementation', 'verifier']
        assert manifest['runs'][0]['run_id'] == 'impl-run'
        assert manifest['runs'][1]['run_id'] == 'verify-run'
        assert manifest['runs'][0]['comparisons'][0]['functional_result'] == 'match'
        assert manifest['runs'][0]['comparisons'][0]['visual_result'] == 'mismatch'
        assert manifest['runs'][1]['comparisons'][0]['functional_result'] == 'mismatch'
        assert manifest['runs'][1]['comparisons'][0]['visual_result'] == 'mismatch'
        assert all(item['comparisons'][0]['provenance']['status'] == 'unverified'
                   for item in manifest['runs'])
        assert 'execution_checks' in manifest['runs'][0]
        assert 'evidence/roles/implementation/checks/000-check.log' in names
        assert 'evidence/roles/verifier/checks/000-check.log' in names
        assert manifest['runs'][0]['execution_checks'][0]['included'] is True


def test_bundle_refuses_web_evidence_with_tampered_sha_wrong_run_or_symlink(tmp_path):
    import zipfile

    from unit_tests.test_reverse_web_core_integration import _web_capture

    root = tmp_path / 'job'
    root.mkdir()
    data = report(root)
    capture = _web_capture(root, run_id='real-run', comparison_id='web-case')
    capture['evidence'][0]['capture']['sha256'] = '0' * 64
    role = {'role': 'verifier', 'run_id': 'wrong-run', 'session_id': 'session',
            'report': {**data, 'comparisons': [capture]}}
    outbox = tmp_path / 'outbox'

    path = tasks.bundle(root, outbox, tasks.validate_report(root, data), evidence_roles=[role])

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('reverse-web-evidence-manifest.json'))
        assert not any(name.startswith('.reverse/web-captures/') and name.endswith('.json') for name in archive.namelist())
        assert manifest['runs'][0]['comparisons'][0]['integrity']['status'] == 'unverified'
        assert manifest['runs'][0]['comparisons'][0]['provenance']['status'] == 'unverified'
        assert manifest['runs'][0]['comparisons'][0]['included'] is False


def test_bundle_manifest_records_traversal_absolute_symlink_size_and_secret_rejections(tmp_path):
    import zipfile

    root = tmp_path / 'job'
    root.mkdir()
    data = report(root)
    outside = tmp_path / 'outside.py'
    outside.write_text('outside')
    (root / 'linked.py').symlink_to(outside)
    (root / 'huge.bin').write_bytes(b'x' * (tasks.MAX_REPORT + 1))
    (root / '.env').write_text('API_KEY=do-not-export')
    untrusted = {**tasks.validate_report(root, data),
                 'source_files': ['../outside.py', str(outside), 'linked.py', 'huge.bin', '.env',
                                  'src/main.py']}

    path = tasks.bundle(root, tmp_path / 'outbox', untrusted, evidence_roles=[])

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read('reverse-web-evidence-manifest.json'))
        rejected = {item['path']: item['reason'] for item in manifest['files'] if not item['included']}
        assert '../outside.py' in rejected
        assert str(outside) in rejected
        assert 'linked.py' in rejected
        assert 'huge.bin' in rejected
        assert '.env' in rejected
        assert 'src/main.py' in names
        assert not any(name.startswith(('../', '/')) for name in names)


def test_bundle_refuses_symlink_as_zip_destination(tmp_path):
    root = tmp_path / 'job'
    root.mkdir()
    data = tasks.validate_report(root, report(root))
    outbox = tmp_path / 'outbox'
    outbox.mkdir()
    victim = tmp_path / 'victim.zip'
    victim.write_bytes(b'keep unchanged')
    (outbox / 'reconstruction-source.zip').symlink_to(victim)

    with pytest.raises(ValueError, match='symlink archive destination'):
        tasks.bundle(root, outbox, data)

    assert victim.read_bytes() == b'keep unchanged'


def test_bundle_keeps_verified_png_over_one_mib_under_png_specific_limit(tmp_path):
    import hashlib
    import random
    import zipfile

    from PIL import Image

    from ttakkari.reverse_web_evidence import create_web_capture
    from unit_tests.test_reverse_web_core_integration import _web_capture

    root = tmp_path / 'job'
    root.mkdir()
    seed = _web_capture(root, run_id='large-run', comparison_id='large-case')
    viewport = {'width': 1024, 'height': 1024, 'device_scale_factor': 1}
    image_dir = root / '.reverse/web-captures/large'
    image_dir.mkdir(parents=True)
    pixels = random.Random(7).randbytes(viewport['width'] * viewport['height'] * 3)
    paths = []
    for side in ('original', 'replica'):
        image_path = image_dir / f'{side}.png'
        Image.frombytes('RGB', (viewport['width'], viewport['height']), pixels).save(image_path, format='PNG')
        blob = image_path.read_bytes()
        assert len(blob) > 1024 * 1024
        paths.append({'path': image_path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(blob).hexdigest(),
                      'size_bytes': len(blob), 'viewport': viewport, 'format': 'png'})
    original = {**seed['original'], 'viewport': viewport, 'screenshot': paths[0]}
    replica = {**seed['replica'], 'viewport': viewport, 'screenshot': paths[1]}
    capture = create_web_capture(
        root, comparison_id='large-case-zip', behavior='submit', input_data=seed['input'],
        original=original, replica=replica, run_id='large-run', expected_viewport=viewport,
        pre_state=seed['pre_state'], environment=seed['environment'], verified_at='2026-11-07T00:01:00Z')
    data = report(root)
    role = {'role': 'implementation', 'run_id': 'large-run', 'session_id': 'large-session',
            'report': {**data, 'comparisons': [capture]}}

    path = tasks.bundle(root, tmp_path / 'outbox', tasks.validate_report(root, data), evidence_roles=[role])

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('reverse-web-evidence-manifest.json'))
        assert manifest['runs'][0]['comparisons'][0]['included'] is True
        for item in manifest['runs'][0]['comparisons'][0]['files']:
            if item['path'].endswith('.png'):
                blob = archive.read(item['path'])
                assert len(blob) == item['size_bytes'] > 1024 * 1024
                assert hashlib.sha256(blob).hexdigest() == item['sha256']
                Image.open(__import__('io').BytesIO(blob)).verify()


def test_bundle_does_not_include_web_capture_mutated_after_creation_or_symlinked(tmp_path):
    import zipfile

    from unit_tests.test_reverse_web_core_integration import _web_capture

    root = tmp_path / 'job'
    root.mkdir()
    mutated = _web_capture(root, run_id='run-archive', comparison_id='mutated-case')
    linked = _web_capture(root, run_id='run-archive', comparison_id='linked-case')
    mutated_path = root / mutated['original']['screenshot']['path']
    mutated_path.write_bytes(mutated_path.read_bytes() + b'changed after capture')
    external = tmp_path / 'external.png'
    external.write_bytes((root / linked['original']['screenshot']['path']).read_bytes())
    linked_path = root / linked['original']['screenshot']['path']
    linked_path.unlink()
    linked_path.symlink_to(external)
    data = report(root)
    role = {'role': 'implementation', 'run_id': 'run-archive', 'session_id': 'session-archive',
            'report': {**data, 'comparisons': [mutated, linked]}}

    path = tasks.bundle(root, tmp_path / 'outbox', tasks.validate_report(root, data), evidence_roles=[role])

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('reverse-web-evidence-manifest.json'))
        comparisons = manifest['runs'][0]['comparisons']
        assert [item['included'] for item in comparisons] == [False, False]
        assert all(item['provenance']['status'] == 'unverified' for item in comparisons)
        assert not any(name.startswith('.reverse/web-captures/') for name in archive.namelist())


def test_bundle_rejects_png_over_web_specific_twelve_mib_limit(tmp_path):
    import zipfile

    from unit_tests.test_reverse_web_core_integration import _web_capture

    root = tmp_path / 'job'
    root.mkdir()
    capture = _web_capture(root, run_id='large-run', comparison_id='oversized')
    image = root / capture['original']['screenshot']['path']
    image.write_bytes(b'P' * (tasks.MAX_WEB_PNG + 1))
    data = report(root)
    role = {'role': 'implementation', 'run_id': 'large-run', 'session_id': 'session',
            'report': {**data, 'comparisons': [capture]}}

    path = tasks.bundle(root, tmp_path / 'outbox', tasks.validate_report(root, data), evidence_roles=[role])

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('reverse-web-evidence-manifest.json'))
        comparison = manifest['runs'][0]['comparisons'][0]
        assert comparison['included'] is False
        assert 'size' in comparison['reason'].lower()
        assert not any(name.startswith('.reverse/web-captures/') for name in archive.namelist())


@pytest.mark.asyncio
async def test_collect_outputs_bundles_implementation_and_verifier_reports_separately(monkeypatch, tmp_path):
    job_id = uuid4()
    job = SimpleNamespace(output_path=str(tmp_path), result={
        'schema_version': 2, 'job': {'mode': 'reconstruct', 'target_type': 'web'},
        'latest': {'run_id': 'impl-run', 'report': {'comparisons': ['implementation']},
                   'verification_run_id': 'verify-run',
                   'verification_report': {'comparisons': ['verifier']}}},
        session_id='impl-session', verifier_session_id='verify-session')

    class FakeDb:
        async def get(self, _model, _id):
            assert _id == job_id
            return job

    captured = {}
    monkeypatch.setattr(tasks, 'read_report', lambda *_args, **_kwargs: {'ready': True})
    monkeypatch.setattr(tasks, 'bundle', lambda *_args, **kwargs: captured.update(kwargs))
    run = SimpleNamespace(id='verify-run', session_id='verify-session',
                          usage={'reverse': {'job_id': str(job_id), 'role': 'verify'}})

    await tasks.collect_outputs(FakeDb(), run, tmp_path / 'outbox')

    assert {entry['role'] for entry in captured['evidence_roles']} == {'implementation', 'verifier'}
    by_role = {entry['role']: entry for entry in captured['evidence_roles']}
    assert by_role['implementation']['run_id'] == 'impl-run'
    assert by_role['implementation']['session_id'] == 'impl-session'
    assert by_role['verifier']['run_id'] == 'verify-run'
    assert by_role['verifier']['session_id'] == 'verify-session'


def test_phase_notes_cannot_self_approve_independent_verification(tmp_path):
    folder = tmp_path / '.reverse'
    folder.mkdir()
    (folder / 'progress.json').write_text(json.dumps([
        {'key': 'observe', 'status': 'done', 'evidence': ['실제 입력→출력']},
        {'key': 'verify', 'status': 'done', 'evidence': ['내가 맞다고 생각함']},
    ]))
    stages = tasks.progress_stages(tmp_path)
    assert next(s for s in stages if s['key'] == 'observe')['status'] == 'done'
    assert next(s for s in stages if s['key'] == 'verify')['status'] == 'pending'


def test_required_failure_cannot_be_hidden_by_an_unrelated_success(tmp_path):
    data = report(tmp_path)
    (tmp_path / 'failed.log').write_text('regression failed\n')
    data['verdict'] = 'passed'
    data['checks'].append({'command': 'pytest regression', 'exit_code': 1, 'log': 'failed.log'})
    assert tasks.validate_report(tmp_path, data)['ready'] is False


def test_optional_unavailable_check_requires_explicit_reason(tmp_path):
    data = report(tmp_path)
    (tmp_path / 'unavailable.log').write_text('No module named pytest\n')
    diagnostic = {'command': 'python -m pytest', 'exit_code': 1, 'log': 'unavailable.log', 'required': False}
    data['checks'].append(diagnostic)
    assert tasks.validate_report(tmp_path, data)['ready'] is False
    diagnostic['reason'] = '기본 Python에는 pytest가 없어 설치된 venv에서 전체 검사를 별도로 실행했다.'
    assert tasks.validate_report(tmp_path, data)['ready'] is True


def test_observed_error_cases_declare_expected_exit_status(tmp_path):
    data = report(tmp_path)
    (tmp_path / 'error.log').write_text('usage: basename string [suffix]\n')
    data['checks'].append({'command': 'python src/main.py', 'exit_code': 1, 'expected_exit_code': 1, 'log': 'error.log'})
    assert tasks.validate_report(tmp_path, data)['ready'] is True


def test_comparisons_are_recomputed_from_hashed_raw_evidence(tmp_path):
    data = report(tmp_path)
    raw = {'schema_version': 2, 'target_type': 'cli', 'run_id': 'run-1', 'complete': True,
           'input': {'argv': ['slugify', 'two words.txt']}, 'pre_state': {}, 'environment': {'os': 'test'},
           'original': {'stdout': 'two-words\n', 'stderr': '', 'exit_code': 0},
           'replica': {'stdout': 'two_words\n', 'stderr': '', 'exit_code': 0}}
    raw['execution_log'] = [{'side': side, **raw[key]} for side, key in
                            (('original', 'original'), ('replica', 'replica'))]
    path = tmp_path / '.reverse/comparisons/slug.json'
    path.parent.mkdir(parents=True)
    encoded = json.dumps(raw).encode()
    path.write_bytes(encoded)
    comparison = {'id': 'slug', 'behavior': 'basename 정규화', 'target_type': 'cli',
                  'input': raw['input'], 'pre_state': {}, 'original': raw['original'], 'replica': raw['replica'],
                  'environment': {'os': 'test'}, 'evidence_level': 'direct',
                  'evidence': [{'kind': 'execution', 'path': '.reverse/comparisons/slug.json',
                                'sha256': hashlib.sha256(encoded).hexdigest()}],
                  'verified_at': '2026-11-07T00:00:00Z', 'run_id': 'run-1',
                  'result': 'match', 'difference': None}
    data['comparisons'] = [comparison]
    checked = tasks.validate_report(tmp_path, data)
    assert checked['comparisons'][0]['result'] == 'mismatch'
    assert checked['comparisons'][0]['difference']['field'] == 'stdout'
    encoded = encoded + b' '
    path.write_bytes(encoded)
    assert tasks.validate_report(tmp_path, data)['comparisons'][0]['result'] == 'unverified'


def test_legacy_report_has_zero_unverified_direct_comparisons(tmp_path):
    checked = tasks.validate_report(tmp_path, report(tmp_path))
    assert checked['comparisons'] == []
    assert checked['direct_comparison'] == {'status': 'unverified', 'count': 0, 'matched': 0,
                                             'mismatched': 0, 'unverified': 0}


def test_analysis_mode_accepts_observations_without_source_or_checks(tmp_path):
    checked = tasks.validate_report(tmp_path, {'mode': 'analyze', 'summary': '분석',
        'observations': [{'finding': '입력에 따라 출력이 달라짐', 'evidence': 'evidence/capture.txt'}],
        'hypotheses': ['옵션이 기본값을 바꿀 수 있음'], 'unknowns': ['오류 종료 코드']})
    assert checked['analysis_ready'] is False  # 출처 수준이 없는 레거시 관찰은 미확인이다.
    assert checked['ready'] is False


def test_observation_provenance_is_preserved_and_missing_level_is_unverified(tmp_path):
    checked = tasks.validate_report(tmp_path, {'mode': 'analyze', 'summary': '분석',
        'observations': [
            {'finding': '화면에서 직접 확인', 'evidence': 'evidence/screen.txt', 'level': 'direct'},
            {'finding': '문서에 기록됨', 'evidence': 'docs/spec.md', 'level': 'document'},
            {'finding': '한 번의 관찰에서 추론', 'evidence': '직접 관찰 1', 'level': 'inferred'},
            {'finding': '기존 보고서 형식', 'evidence': '사용자 설명'},
        ], 'hypotheses': ['상태가 저장됨'], 'unknowns': ['다른 기기 동기화']})
    assert [o['level'] for o in checked['observations']] == ['direct', 'document', 'inferred', 'unverified']
    assert checked['analysis_ready'] is True


def test_reconstruction_requires_readme_environment_dependencies_run_and_test_commands(tmp_path):
    data = report(tmp_path)
    (tmp_path / 'README.md').write_text('python src/main.py')
    checked = tasks.validate_report(tmp_path, data)
    assert checked['ready'] is False
    assert checked['documentation']['status'] == 'unverified'
    assert 'README.md' in ' '.join(checked['problems'])


def test_invalid_comparison_is_preserved_as_unverified_instead_of_dropped(tmp_path):
    checked = tasks.validate_report(tmp_path, {**report(tmp_path), 'comparisons': [{'id': 'bad'}]})
    assert len(checked['comparisons']) == 1
    assert checked['comparisons'][0]['result'] == 'unverified'
    assert checked['direct_comparison']['unverified'] == 1


def test_pre_v2_result_remains_readable_without_inventing_direct_comparisons():
    legacy = {'summary': '기존 재현 결과', 'source_files': ['src/main.py'],
              'checks': [{'command': 'pytest', 'exit_code': 0}], 'verification': 'passed'}
    metadata = tasks._metadata(legacy)
    assert metadata['latest']['report']['summary'] == '기존 재현 결과'
    assert metadata['latest']['direct_comparison'] == {'status': 'unverified', 'count': 0,
        'matched': 0, 'mismatched': 0, 'unverified': 0}


def test_unhashable_target_type_is_preserved_as_unverified(tmp_path):
    data = report(tmp_path)
    data['comparisons'] = [{'id': 'bad-type', 'behavior': 'invalid', 'target_type': [], 'input': {},
        'pre_state': {}, 'original': None, 'replica': None, 'environment': {}, 'evidence_level': 'direct',
        'evidence': [], 'verified_at': '2026-11-07T00:00:00Z', 'run_id': 'run-1',
        'result': 'match', 'difference': None}]
    checked = tasks.validate_report(tmp_path, data)
    assert checked['comparisons'][0]['result'] == 'unverified'
    assert checked['comparisons'][0]['difference']['reason']


def test_api_stateful_comparison_without_state_capture_is_unverified(tmp_path):
    import base64

    body = base64.b64encode(b'{"ok":true}').decode()
    output = {'status': 200, 'body': '{"ok":true}', 'body_bytes_b64': body, 'headers': {'content-type': 'application/json'}}
    raw = {'schema_version': 2, 'target_type': 'api', 'run_id': 'api-run', 'complete': True,
        'input': {'state_path': '/state'}, 'pre_state': {}, 'environment': {},
        'original': output, 'replica': output,
        'execution_log': [{'side': side, 'method': 'GET', 'url': 'http://localhost/api',
            'request_headers': {}, 'output': output} for side in ('original', 'replica')]}
    path = tmp_path / '.reverse/comparisons/api.json'
    path.parent.mkdir(parents=True)
    encoded = json.dumps(raw).encode()
    path.write_bytes(encoded)
    comparison = {'id': 'api-case', 'behavior': '상태 변화', 'target_type': 'api', 'input': raw['input'],
        'pre_state': {}, 'original': output, 'replica': output, 'environment': {}, 'evidence_level': 'direct',
        'evidence': [{'kind': 'execution', 'path': '.reverse/comparisons/api.json',
                      'sha256': hashlib.sha256(encoded).hexdigest()}],
        'verified_at': '2026-11-07T00:00:00Z', 'run_id': 'api-run', 'result': 'match', 'difference': None}
    checked = tasks.verify_comparisons(tmp_path, [comparison], expected_run_id='api-run')[0]
    assert checked['result'] == 'unverified'
    assert checked['evidence_level'] == 'observed'


def test_api_location_header_query_secret_is_unverified(tmp_path):
    body = ''
    output = {'status': 302, 'body': body, 'body_bytes_b64': '',
              'headers': {'location': 'http://localhost/next?token=leaked'}}
    raw = {'schema_version': 2, 'target_type': 'api', 'run_id': 'api-run', 'complete': True,
        'input': {}, 'pre_state': {}, 'environment': {}, 'original': output, 'replica': output,
        'execution_log': [{'side': side, 'method': 'GET', 'url': 'http://localhost/start',
            'request_headers': {}, 'output': output} for side in ('original', 'replica')]}
    path = tmp_path / '.reverse/comparisons/api-location.json'
    path.parent.mkdir(parents=True)
    encoded = json.dumps(raw).encode()
    path.write_bytes(encoded)
    comparison = {'id': 'api-location', 'behavior': 'Location 비밀 쿼리', 'target_type': 'api', 'input': {},
        'pre_state': {}, 'original': output, 'replica': output, 'environment': {}, 'evidence_level': 'direct',
        'evidence': [{'kind': 'execution', 'path': '.reverse/comparisons/api-location.json',
                      'sha256': hashlib.sha256(encoded).hexdigest()}],
        'verified_at': '2026-11-07T00:00:00Z', 'run_id': 'api-run', 'result': 'match', 'difference': None}
    checked = tasks.verify_comparisons(tmp_path, [comparison], expected_run_id='api-run')[0]
    assert checked['result'] == 'unverified'
    assert checked['evidence_level'] == 'observed'
