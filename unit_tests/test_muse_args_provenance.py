"""MSP item.args provenance tells missing args from explicit {} without storing values."""
from pathlib import Path

from ttakkari.agents.base import ParseState
from ttakkari.agents.muse import MuseAdapter
from ttakkari.runs.diagnostics import _stored_provenance


def tool_item(iid, revision=2, status='failed', output='boom', **extra):
    item = {'kind': 'toolCall', 'itemId': iid, 'revision': revision,
            'tool': 'mcp__ttakkari__spawn_worker', 'status': status,
            'visibleOutput': output}
    item.update(extra)
    return item


def run(items):
    """Drive parse_line over (method, item) pairs; return (state, tool.call payloads)."""
    adapter, state = MuseAdapter(), ParseState()
    calls = []
    for method, item in items:
        events = adapter.parse_line({'method': method, 'params': {'item': item}},
                                    state, Path('/tmp'))
        calls.extend(event.payload for event in events if event.type == 'tool.call')
    return state, calls


def test_missing_args_and_explicit_empty_share_input_but_not_provenance():
    _, [missing] = run([('item/completed', tool_item('a'))])
    _, [explicit] = run([('item/completed', tool_item('b', args='{}'))])
    assert missing['input'] == explicit['input'] == {}
    assert missing['input_provenance'] == {'args_present': False, 'args_type': 'NoneType',
                                           'args_parsed': False}
    assert explicit['input_provenance'] == {'args_present': True, 'args_type': 'str',
                                            'args_parsed': True}


def test_empty_string_args_marked_present_but_unparsed():
    _, [call] = run([('item/completed', tool_item('a', args=''))])
    assert call['input'] == {}
    assert call['input_provenance'] == {'args_present': True, 'args_type': 'str',
                                        'args_parsed': False}


def test_valid_args_keep_keys_and_parse_flag():
    _, [call] = run([('item/completed', tool_item('a', args='{"title":"t"}'))])
    assert call['input'] == {'title': 't'}
    assert call['input_provenance'] == {'args_present': True, 'args_type': 'str',
                                        'args_parsed': True}


def test_unparseable_args_preserve_existing_raw_input():
    _, [call] = run([('item/completed', tool_item('a', args='not-json{{'))])
    assert call['input'] == {'raw': 'not-json{{'}
    assert call['input_provenance'] == {'args_present': True, 'args_type': 'str',
                                        'args_parsed': False}
    assert 'not-json' not in repr(call['input_provenance'])


def test_non_string_args_record_type_without_value():
    _, [call] = run([('item/completed', tool_item('a', args={'title': 'SECRET_VALUE'}))])
    assert call['input_provenance'] == {'args_present': True, 'args_type': 'dict',
                                        'args_parsed': False}
    assert 'SECRET_VALUE' not in repr(call['input_provenance'])


def test_provenance_never_carries_argument_values_or_secrets():
    args = '{"prompt":"PROMPT_SECRET","api_key":"sk-SECRET","title":"t"}'
    _, [call] = run([('item/completed', tool_item('a', args=args))])
    blob = repr(call['input_provenance'])
    for secret in ('PROMPT_SECRET', 'sk-SECRET', 'api_key', 'prompt'):
        assert secret not in blob, secret


def test_started_call_keeps_started_provenance_when_completed_adds_args():
    _, calls = run([('item/started', tool_item('a', revision=1, status='running', output='')),
                    ('item/completed', tool_item('a', revision=2, args='{}'))])
    assert len(calls) == 1
    assert calls[0]['input_provenance']['args_present'] is False


def test_completed_without_started_uses_completed_provenance():
    _, calls = run([('item/completed', tool_item('a', args='{}'))])
    assert len(calls) == 1
    assert calls[0]['input_provenance']['args_present'] is True


def test_stored_provenance_echoes_shape_only():
    assert _stored_provenance({'args_present': True, 'args_type': 'str',
                               'args_parsed': True}) == {'args_present': True, 'args_type': 'str',
                                                         'args_parsed': True}


def test_stored_provenance_stays_null_for_legacy_rows():
    assert _stored_provenance(None) is None
    assert _stored_provenance('str') is None
    assert _stored_provenance(['args_present']) is None


def test_stored_provenance_masks_and_bounds_type_text():
    echoed = _stored_provenance({'args_present': 1, 'args_type': 'x' * 100, 'extra': 'dropped'})
    assert echoed == {'args_present': True, 'args_type': 'x' * 32, 'args_parsed': False}
