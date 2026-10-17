"""Muse PreToolUse insurance; native sandbox remains mandatory.

Unknown native tools fail closed until their argument contract is checked.
Policy is passed as a private file because Muse clears hook environments.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ttakkari.broker.policy import is_within
from ttakkari.security.guard import GuardConfig, evaluate

PATH_TOOLS = {'read_file': 'Read', 'write_file': 'Write', 'edit_file': 'Edit',
              'list_directory': 'Glob', 'search_files': 'Grep', 'Read': 'Read', 'Write': 'Write', 'Edit': 'Edit'}
SHELL_TOOLS = {'shell', 'bash', 'Bash', 'exec_command'}


def decision(data: dict, policy: dict) -> str | None:
    tool, args = data.get('tool_name'), data.get('tool_input')
    if not isinstance(tool, str) or not isinstance(args, dict):
        return 'Malformed Muse tool payload'
    cfg = GuardConfig(write_roots=[Path(p) for p in policy['write_roots']],
                      protected=[Path(p) for p in policy['protected']])
    cwd = policy['cwd']
    private = Path(policy['private_root']).resolve()
    if tool in SHELL_TOOLS:
        command = args.get('command') or args.get('cmd')
        if not isinstance(command, str):
            return 'Unrecognized Muse shell arguments'
        if str(private) in command:
            return 'Private Muse launch material access'
        return evaluate('Bash', {'command': command}, cwd, cfg)
    if tool in PATH_TOOLS:
        raw = args.get('path') or args.get('file_path')
        if not isinstance(raw, str):
            return 'Unrecognized Muse file arguments'
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(cwd) / path
        if is_within(path.resolve(), private):
            return 'Private Muse launch material access'
        return evaluate(PATH_TOOLS[tool], {'file_path': raw}, cwd, cfg)
    if tool == 'apply_patch':
        patch = args.get('patch') or args.get('input')
        if not isinstance(patch, str):
            return 'Unrecognized patch arguments'
        paths = re.findall(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$', patch, re.MULTILINE)
        if not paths:
            return 'No auditable file paths in patch'
        for raw in paths:
            reason = decision({'tool_name': 'write_file', 'tool_input': {'path': raw}}, policy)
            if reason:
                return reason
        return None
    if tool.startswith('mcp__'):
        parts = tool.split('__', 2)
        if len(parts) == 3 and parts[1] in policy['mcp_servers'] and parts[2] not in {'spawn_worker', 'message_worker'}:
            return None
    if tool in {'web_search', 'web_fetch', 'update_plan'}:
        return None
    return f'Muse tool is not yet admitted: {tool}'


def main() -> int:
    try:
        if len(sys.argv) != 3 or sys.argv[1] != '--policy':
            raise ValueError('Expected private policy file')
        policy = json.loads(Path(sys.argv[2]).read_text())
        reason = decision(json.load(sys.stdin), policy)
    except Exception as error:  # noqa: BLE001 - every guard failure must become a blocking exit 2
        # Exit 2 is blocking under Muse's documented hook contract.
        print(f'[ttakkari-guard] Muse guard failed: {type(error).__name__}', file=sys.stderr)
        return 2
    if reason:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
              'permissionDecision': 'deny', 'permissionDecisionReason': f'[ttakkari-guard] {reason}'}}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
