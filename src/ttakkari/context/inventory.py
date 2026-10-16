"""Read-only source accounting, without semantic selection, database writes or model calls.

A complete inventory covers discovered shards; it does not certify that an export
contains every conversation, that attachments were interpreted, or that knowledge
was extracted/indexed. Each occurrence retains a raw revision and JSON locator.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

PARSER_VERSION = 'chatgpt-inventory-v1'
MAX_SHARD_BYTES = 64 * 1024 * 1024
SHARD_NAME = re.compile(r'^conversations(?:-\d+)?\.json$')
ROLES = {'user', 'assistant', 'system', 'tool', 'developer'}


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate_json_key')
        out[key] = value
    return out


def _invalid_constant(value: str) -> None:
    raise ValueError('nonfinite_json_number')


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('nonfinite_json_number')
    return parsed


def _pointer(value: str) -> str:
    return value.replace('~', '~0').replace('/', '~1')


def _active_nodes(conversation: dict, mapping: dict) -> tuple[set[str] | None, str | None]:
    # Validate all parent references, including inactive branches, in O(nodes).
    for node in mapping.values():
        if not isinstance(node, dict):
            continue  # The caller accounts for invalid node shape separately.
        parent = node.get('parent')
        if parent is not None and (not isinstance(parent, str) or parent not in mapping):
            return None, 'dangling_parent'
    # Check for cycles in every branch, with completed paths memoized.
    completed: set[str] = set()
    for start in mapping:
        visiting: set[str] = set()
        at = start
        while at is not None and at not in completed:
            if at in visiting:
                return None, 'branch_cycle'
            visiting.add(at)
            node = mapping[at]
            at = node.get('parent') if isinstance(node, dict) else None
        completed.update(visiting)
    current = conversation.get('current_node')
    if not isinstance(current, str) or current not in mapping:
        return None, 'current_node_missing'
    active: set[str] = set()
    at = current
    while at is not None:
        active.add(at)
        node = mapping[at]
        at = node.get('parent') if isinstance(node, dict) else None
    return active, None


def inspect_export(source: Path, *, max_shard_bytes: int = MAX_SHARD_BYTES,
                   expected_shards: int | None = None) -> dict:
    """Inventory all node occurrences, retaining partial results with explicit errors.

    Read at most one bounded shard at a time. Output contains identifiers, hashes,
    fixed reason codes and counts only: no message text, titles or asset locators.
    Corpus and unit identity are deterministic; extraction is always not_run.
    """
    if max_shard_bytes <= 0 or (expected_shards is not None and expected_shards < 1):
        raise ValueError('invalid_inventory_limit')
    source = Path(source).expanduser()
    errors: list[dict] = []
    shards: list[dict] = []
    units: list[dict] = []
    counts: Counter = Counter()
    outcomes: Counter = Counter()
    conversations: dict[str, str] = {}
    # Record compact failures; never include exception text (it can contain raw JSON).
    def error(code: str, shard: str | None = None, pointer: str | None = None) -> None:
        errors.append({'code': code, 'shard': shard, 'json_pointer': pointer})

    paths: list[Path] = []
    try:
        if source.is_symlink():
            error('symlink_source')
        elif source.is_file():
            if SHARD_NAME.fullmatch(source.name):
                paths = [source]
            else:
                error('unsupported_source_name')
        elif source.is_dir():
            paths = sorted(p for p in source.iterdir() if SHARD_NAME.fullmatch(p.name))
        else:
            error('source_missing')
    except OSError:
        error('source_unreadable')
    if not paths:
        error('no_shards')
    if expected_shards is not None and len(paths) != expected_shards:
        error('shard_count_mismatch')
    counts['discovered_shards'] = len(paths)

    for path in paths:
        meta = {'file': path.name, 'sha256': None, 'bytes': None, 'status': 'failed'}
        shards.append(meta)
        if path.is_symlink():
            error('symlink_shard', path.name)
            counts['failed_shards'] += 1
            continue
        try:
            with path.open('rb') as handle:
                # fstat and bounded read both matter if a file grows during inspection.
                before = os.fstat(handle.fileno())
                if before.st_size > max_shard_bytes:
                    raise ValueError('shard_too_large')
                raw = handle.read(max_shard_bytes + 1)
                after = os.fstat(handle.fileno())
            if len(raw) > max_shard_bytes:
                raise ValueError('shard_too_large')
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('source_changed_during_read')
            meta.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant, parse_float=_finite_float)
            del raw
            if not isinstance(data, list):
                raise TypeError('expected_conversation_array')
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            # Parser exception messages may contain user content; use a code allowlist.
            allowed = {'shard_too_large', 'source_changed_during_read', 'expected_conversation_array',
                       'duplicate_json_key', 'nonfinite_json_number'}
            code = str(exc) if type(exc) in (ValueError, TypeError) and str(exc) in allowed else 'invalid_or_unreadable_shard'
            error(code, path.name)
            counts['failed_shards'] += 1
            continue
        meta['status'] = 'parsed'
        counts['parsed_shards'] += 1
        for index, conv in enumerate(data):
            counts['conversation_records'] += 1
            pointer = f'/{index}'
            if not isinstance(conv, dict):
                error('invalid_conversation', path.name, pointer)
                continue
            cid = conv.get('id') or conv.get('conversation_id')
            mapping = conv.get('mapping')
            if not isinstance(cid, str) or not cid or not isinstance(mapping, dict) or not mapping:
                error('invalid_conversation', path.name, pointer)
                continue
            fingerprint = _hash(conv)
            if cid in conversations:
                if conversations[cid] == fingerprint:
                    counts['duplicate_conversations'] += 1
                else:
                    counts['conflicting_conversations'] += 1
                    error('conflicting_conversation_revision', path.name, pointer)
            else:
                conversations[cid] = fingerprint
            active, branch_error = _active_nodes(conv, mapping)
            if branch_error:
                error(branch_error, path.name, pointer)
            for nid, node in mapping.items():
                counts['nodes'] += 1
                locator = f'{pointer}/mapping/{_pointer(nid)}'
                unit = {'unit_key': _hash(['chatgpt', cid, nid]), 'conversation_id': cid,
                        'node_id': nid, 'message_id': None, 'parent_node_id': None,
                        'source_file': path.name, 'source_sha256': meta['sha256'],
                        'json_pointer': locator, 'content_sha256': _hash(node),
                        'active_branch': nid in active if active is not None else None,
                        'speaker': 'unknown', 'text_characters': 0, 'nontext_parts': 0,
                        'attachment_metadata_count': 0, 'outcome': 'invalid_node',
                        'reason': 'invalid_node_shape', 'extraction_state': 'not_run'}
                if not isinstance(node, dict) or 'message' not in node:
                    error('invalid_node', path.name, locator)
                else:
                    unit['parent_node_id'] = node.get('parent') if isinstance(node.get('parent'), str) else None
                    msg = node['message']
                    if msg is None:
                        unit.update(outcome='no_message', reason='structural_node')
                    elif not isinstance(msg, dict):
                        unit.update(outcome='invalid_message', reason='invalid_message_shape')
                        error('invalid_message', path.name, locator)
                    else:
                        counts['messages'] += 1
                        unit['message_id'] = msg.get('id') if isinstance(msg.get('id'), str) else None
                        author = msg.get('author')
                        role = author.get('role') if isinstance(author, dict) else None
                        unit['speaker'] = role if isinstance(role, str) and role in ROLES else 'unknown'
                        content = msg.get('content')
                        parts = content.get('parts') if isinstance(content, dict) else None
                        if isinstance(parts, list):
                            unit['text_characters'] = sum(len(p) for p in parts if isinstance(p, str))
                            unit['nontext_parts'] = sum(not isinstance(p, str) for p in parts)
                        metadata = msg.get('metadata')
                        attachments = metadata.get('attachments') if isinstance(metadata, dict) else None
                        if isinstance(attachments, list):
                            unit['attachment_metadata_count'] = len(attachments)
                        if unit['text_characters']:
                            unit.update(outcome='pending_extraction', reason='text_present_not_extracted')
                        elif unit['nontext_parts'] or not isinstance(parts, list):
                            unit.update(outcome='unsupported_content', reason='content_adapter_required')
                        else:
                            unit.update(outcome='empty_content', reason='empty_parts')
                outcomes[unit['outcome']] += 1
                units.append(unit)
        del data
    counts['conversations'] = len(conversations)
    for key in ('conversation_records', 'nodes', 'messages', 'parsed_shards', 'failed_shards',
                'duplicate_conversations', 'conflicting_conversations'):
        counts.setdefault(key, 0)
    return {'schema_version': 1, 'parser_version': PARSER_VERSION,
            'status': 'incomplete' if errors else 'complete',
            'completeness_scope': 'discovered_shards_only',
            'expected_shards': expected_shards,
            'corpus_fingerprint': _hash([(s['file'], s['sha256'], s['status']) for s in shards]),
            'counts': {**dict(counts), 'outcomes': dict(outcomes)},
            'shards': shards, 'units': units, 'errors': errors,
            'capabilities': {'semantic_extraction': 'not_run', 'index_reconciliation': 'not_run',
                             'attachment_payloads': 'not_read', 'database_writes': False}}


def write_inventory(report: dict, destination: Path, *, source: Path) -> None:
    """Publish a new private bundle; failed publication leaves no partial directory."""
    import shutil
    import tempfile

    destination = Path(destination).expanduser().absolute()
    original = Path(source).expanduser().resolve()
    resolved = destination.resolve()
    if resolved == original or (original.is_dir() and resolved.is_relative_to(original)):
        raise ValueError('output_inside_source')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('output_already_exists')
    # Parent must already exist; do not mutate an arbitrary original tree.
    stage = Path(tempfile.mkdtemp(prefix='.knowledge-inventory-', dir=destination.parent))
    reservation = None
    try:
        summary = {k: v for k, v in report.items() if k not in {'units', 'errors'}}
        for name, data in [('summary.json', json.dumps(summary, ensure_ascii=False, indent=2) + '\n'),
                           ('units.jsonl', ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in report['units'])),
                           ('errors.jsonl', ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in report['errors']))]:
            path = stage / name
            with path.open('x', encoding='utf-8') as handle:
                os.chmod(path, 0o600)
                handle.write(data)
        # os.rename would replace an empty directory; refuse even that overwrite.
        destination.mkdir(mode=0o700)
        reservation = destination.stat().st_ino
        stage.rename(destination)  # Atomic replacement of our reserved empty directory.
        reservation = None
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if reservation is not None:
            try:
                if destination.stat().st_ino == reservation:
                    destination.rmdir()  # Empty reservation only; never remove someone else's data.
            except OSError:
                pass
