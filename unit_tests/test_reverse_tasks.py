"""자연어 역분석은 관찰 증거와 작업공간의 재현 소스로 닫혀야 한다."""
import json

import pytest

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
    (root / 'check.log').write_text('1 passed\n')
    data = {'summary': '덧셈 재현', 'source_files': ['src/main.py'],
            'observations': [{'finding': '12 + 3 = 15', 'evidence': '사용자 설명'}],
            'checks': [{'command': 'python -m pytest', 'exit_code': 0, 'log': 'check.log'}],
            'limitations': []}
    return {**data, **overrides}


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
