"""Lossless source accounting is distinct from semantic extraction or indexing."""
from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def inventory():
    assert importlib.util.find_spec('ttakkari.context.inventory') is not None, 'Source inventory feature is missing'
    return importlib.import_module('ttakkari.context.inventory')


def conversation(cid='c1', *, current='a', extra=None):
    mapping = {
        'root': {'id': 'root', 'parent': None, 'message': None},
        'u': {'id': 'u', 'parent': 'root', 'message': {
            'id': 'm-user', 'author': {'role': 'user'}, 'create_time': 123,
            'content': {'content_type': 'text', 'parts': ['PRIVATE HEALTH AND CAREER TEXT']}}},
        'a': {'id': 'a', 'parent': 'u', 'message': {
            'id': 'm-assistant', 'author': {'role': 'assistant'},
            'content': {'content_type': 'text', 'parts': ['PRIVATE AI PROPOSAL']}}},
        'alt': {'id': 'alt', 'parent': 'u', 'message': {
            'id': 'm-alt', 'author': {'role': 'assistant'},
            'content': {'content_type': 'multimodal_text', 'parts': [
                {'content_type': 'image_asset_pointer', 'asset_pointer': 'PRIVATE-ASSET'}]}}},
    }
    mapping.update(extra or {})
    return {'id': cid, 'title': 'PRIVATE TITLE', 'current_node': current, 'mapping': mapping}


def shard(root: Path, value, name='conversations-000.json'):
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def test_all_nodes_branches_and_unsupported_content_have_a_disposition(tmp_path):
    shard(tmp_path, [conversation()])
    result = inventory().inspect_export(tmp_path)
    assert result['status'] == 'complete'
    assert result['counts']['conversations'] == 1
    assert result['counts']['nodes'] == 4
    assert result['counts']['messages'] == 3
    assert sum(result['counts']['outcomes'].values()) == 4
    nodes = {r['node_id']: r for r in result['units']}
    assert nodes['root']['outcome'] == 'no_message'
    assert nodes['u']['outcome'] == 'pending_extraction'
    assert nodes['u']['speaker'] == 'user'
    assert nodes['u']['active_branch'] is True
    assert nodes['alt']['active_branch'] is False
    assert nodes['alt']['outcome'] == 'unsupported_content'
    assert nodes['alt']['nontext_parts'] == 1
    assert nodes['u']['json_pointer'] == '/0/mapping/u'
    assert nodes['u']['message_id'] == 'm-user'
    assert nodes['u']['text_characters'] == len('PRIVATE HEALTH AND CAREER TEXT')
    assert all(r['extraction_state'] == 'not_run' for r in result['units'])


def test_inventory_never_emits_message_title_or_attachment_payload(tmp_path):
    shard(tmp_path, [conversation()])
    report = json.dumps(inventory().inspect_export(tmp_path))
    assert 'PRIVATE' not in report
    assert 'pending_extraction' in report
    assert 'knowledge_retention' not in report


def test_stable_unit_identity_survives_reexport_but_revision_changes(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    shard(a, [conversation()])
    changed = conversation()
    changed['mapping']['u']['message']['content']['parts'][0] = 'corrected statement'
    shard(b, [changed], name='conversations-001.json')
    one, two = inventory().inspect_export(a), inventory().inspect_export(b)
    units1, units2 = one['units'], two['units']
    assert {r['unit_key'] for r in units1} == {r['unit_key'] for r in units2}
    assert one['corpus_fingerprint'] != two['corpus_fingerprint']
    u1, u2 = next(r for r in units1 if r['node_id'] == 'u'), next(r for r in units2 if r['node_id'] == 'u')
    assert u1['content_sha256'] != u2['content_sha256']
    assert len(u1['source_sha256']) == 64


@pytest.mark.parametrize('body', ['[', '[{},]', '[{},,{}]', '[] trailing', '{}', '[NaN]', '[{"id":1,"id":2}]'])
def test_malformed_or_ambiguous_json_is_incomplete(tmp_path, body):
    (tmp_path / 'conversations.json').write_text(body)
    result = inventory().inspect_export(tmp_path)
    assert result['status'] == 'incomplete'
    assert result['counts']['failed_shards'] == 1
    assert result['errors']


def test_invalid_conversation_keeps_an_error_and_other_records(tmp_path):
    shard(tmp_path, [conversation(), {'id': 'broken', 'mapping': None}, None])
    result = inventory().inspect_export(tmp_path)
    assert result['status'] == 'incomplete'
    assert result['counts']['conversation_records'] == 3
    assert result['counts']['nodes'] == 4
    assert len(result['errors']) == 2
    assert all('PRIVATE' not in str(e) for e in result['errors'])


def test_unknown_node_shape_is_not_silently_discarded(tmp_path):
    shard(tmp_path, [conversation(extra={'bad': []})])
    result = inventory().inspect_export(tmp_path)
    assert result['status'] == 'incomplete'
    assert result['counts']['nodes'] == 5
    assert next(r for r in result['units'] if r['node_id'] == 'bad')['outcome'] == 'invalid_node'


@pytest.mark.parametrize('case', ['cycle', 'dangling', 'current_missing'])
def test_broken_branch_structure_is_reported_without_inventing_active_path(tmp_path, case):
    c = conversation()
    if case == 'cycle':
        c['mapping']['u']['parent'] = 'a'
    elif case == 'dangling':
        c['mapping']['u']['parent'] = 'missing'
    else:
        c['current_node'] = 'missing'
    shard(tmp_path, [c])
    result = inventory().inspect_export(tmp_path)
    assert result['status'] == 'incomplete'
    assert result['counts']['nodes'] == 4
    assert all(r['active_branch'] is None for r in result['units'])


def test_exact_duplicate_occurrences_are_preserved_and_conflicts_fail(tmp_path):
    c = conversation()
    shard(tmp_path, [c])
    shard(tmp_path, [c], 'conversations-001.json')
    good = inventory().inspect_export(tmp_path)
    assert good['status'] == 'complete'
    assert good['counts']['conversations'] == 1
    assert good['counts']['duplicate_conversations'] == 1
    assert good['counts']['nodes'] == 8
    changed = conversation()
    changed['mapping']['u']['message']['content']['parts'] = ['different']
    shard(tmp_path, [changed], 'conversations-001.json')
    bad = inventory().inspect_export(tmp_path)
    assert bad['status'] == 'incomplete'
    assert bad['counts']['conflicting_conversations'] == 1
    assert bad['counts']['nodes'] == 8


def test_no_input_is_not_success_and_symlink_shards_are_not_followed(tmp_path):
    empty = inventory().inspect_export(tmp_path)
    assert empty['status'] == 'incomplete' and empty['errors']
    real = shard(tmp_path / 'other', [conversation()])
    (tmp_path / 'conversations.json').symlink_to(real)
    linked = inventory().inspect_export(tmp_path)
    assert linked['status'] == 'incomplete'
    assert linked['counts']['messages'] == 0
    assert any(e['code'] == 'symlink_shard' for e in linked['errors'])


def test_oversize_input_fails_with_reason(tmp_path):
    shard(tmp_path, [conversation()])
    result = inventory().inspect_export(tmp_path, max_shard_bytes=10)
    assert result['status'] == 'incomplete'
    assert result['errors'][0]['code'] == 'shard_too_large'


def test_json_pointer_escapes_mapping_keys(tmp_path):
    c = conversation(extra={'x/y~z': {'parent': 'u', 'message': None}})
    shard(tmp_path, [c])
    unit = next(r for r in inventory().inspect_export(tmp_path)['units'] if r['node_id'] == 'x/y~z')
    assert unit['json_pointer'] == '/0/mapping/x~1y~0z'


def test_cli_writes_private_atomic_bundle_without_db_or_model(tmp_path):
    source, output = tmp_path / 'raw', tmp_path / 'bundle'
    shard(source, [conversation()])
    run = subprocess.run([sys.executable, '-m', 'ttakkari.cli', 'context-inventory', str(source), '--output', str(output)],
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    summary = json.loads((output / 'summary.json').read_text())
    assert summary['counts']['nodes'] == 4
    assert 'units' not in summary
    assert len((output / 'units.jsonl').read_text().splitlines()) == 4
    assert (output.stat().st_mode & 0o077) == 0
    assert 'PRIVATE' not in run.stdout + run.stderr + (output / 'summary.json').read_text()
    retry = subprocess.run(run.args, capture_output=True, text=True, check=False)
    assert retry.returncode != 0
    assert len((output / 'units.jsonl').read_text().splitlines()) == 4


def test_cli_partial_has_nonzero_exit_and_durable_error_ledger(tmp_path):
    source, output = tmp_path / 'raw', tmp_path / 'bundle'
    shard(source, [conversation()])
    (source / 'conversations-001.json').write_text('[broken')
    run = subprocess.run([sys.executable, '-m', 'ttakkari.cli', 'context-inventory', str(source), '--output', str(output)],
                         capture_output=True, text=True, check=False)
    assert run.returncode == 2
    assert json.loads((output / 'summary.json').read_text())['status'] == 'incomplete'
    assert len((output / 'units.jsonl').read_text().splitlines()) == 4
    assert (output / 'errors.jsonl').read_text().strip()


def test_cli_refuses_to_create_output_inside_original_source(tmp_path):
    source = tmp_path / 'raw'
    shard(source, [conversation()])
    run = subprocess.run([sys.executable, '-m', 'ttakkari.cli', 'context-inventory', str(source), '--output', str(source / 'out')],
                         capture_output=True, text=True, check=False)
    assert run.returncode != 0
    assert not (source / 'out').exists()


def test_cli_deadline_records_blocked_directory_enumeration(tmp_path):
    source, output = tmp_path / 'raw', tmp_path / 'bundle'
    source.mkdir()
    script = '''
import sys, time
from pathlib import Path
from ttakkari.cli import main
original = Path.iterdir
def stalled(self):
    if str(self) == sys.argv[1]:
        time.sleep(10)
    return original(self)
Path.iterdir = stalled
main(['context-inventory', sys.argv[1], '--output', sys.argv[2], '--timeout', '0.05'])
'''
    run = subprocess.run([sys.executable, '-c', script, str(source), str(output)],
                         capture_output=True, text=True, check=False, timeout=3)
    assert run.returncode == 2
    report = json.loads((output / 'summary.json').read_text())
    assert report['status'] == 'incomplete'
    assert report['counts'] == {}, 'Unobserved input is unknown, not zero'
    assert json.loads((output / 'errors.jsonl').read_text())['code'] == 'inventory_deadline_exceeded'


@pytest.mark.parametrize('body', ['[1e999]', '[-Infinity]'])
def test_nonfinite_numbers_are_not_accepted_as_valid_inventory(tmp_path, body):
    (tmp_path / 'conversations.json').write_text(body)
    result = inventory().inspect_export(tmp_path)
    assert result['status'] == 'incomplete'
    assert result['errors'][0]['code'] == 'nonfinite_json_number'
