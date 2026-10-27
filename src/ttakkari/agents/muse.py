"""Muse's official MSP/SDK bridge, normalized to the shared Run contract."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ttakkari.agents.base import LaunchSpec, Normalized, ParseState, abs_path, truncate
from ttakkari.config import get_settings
from ttakkari.model_catalog import MUSE_EFFORT


def _mcp_binding_error(name: str, args: Any, output: str, failed: bool) -> bool:
    """Recognize pre-dispatch binding errors, never arbitrary tool/business failures."""
    if not name.startswith(('mcp__', 'mcp.')):
        return False
    leaf = name.rsplit('__', 1)[-1].rsplit('.', 1)[-1]
    unknown = re.match(r'''(?i)^unknown tool\s*:?[\s`'"]+([^\s`'":]+)''', output)
    if failed and unknown and unknown.group(1) in {name, leaf}:
        return True
    return (args == {} and output.startswith(f'Error executing tool {leaf}:')
            and 'Field required' in output and 'input_value={}' in output)


class MuseAdapter:
    engine = 'muse'
    validates_resume_identity = True

    def __init__(self):
        self._resources: dict[int, Path] = {}

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        settings = get_settings()
        if not settings.muse_enabled:
            raise RuntimeError('Muse execution is disabled pending operational acceptance')
        if spec.model and spec.model not in settings.engine_models.get('muse', []):
            raise ValueError('Unsupported Muse coding model')
        runtime = Path(__file__).resolve().parents[3] / '.muse-runtime'
        if not (settings.muse_runtime_root / 'node_modules/@muse-code/sdk/package.json').is_file():
            raise RuntimeError('Muse SDK runtime missing; run npm ci --prefix .muse-runtime --ignore-scripts')
        config_home = Path(extra_env.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
        native_settings = config_home / 'muse/settings.json'
        if native_settings.is_file() and json.loads(native_settings.read_text()).get('managed_hooks_path'):
            raise RuntimeError('Existing Muse managed hooks require composition; refusing to replace them')
        self.cleanup(spec)
        private = Path(tempfile.mkdtemp(prefix='ttakkari-muse-'))
        self._resources[id(spec)] = private
        for key in ('META_API_KEY', 'MUSE_AUTH_PATH', 'MUSE_TRANSPORT_TRACE'):
            extra_env.pop(key, None)
        extra_env.update(MUSE_NO_AUTO_UPDATE='1', MUSE_LOGIN='0')
        role = 'agent'
        for srv in (spec.mcp_servers or {}).values():
            env = srv.get('env') if isinstance(srv, dict) else None
            if isinstance(env, dict) and env.get('TTK_ROLE') == 'orchestrator':
                role = 'orchestrator'
                break
        policy = {'write_roots': [str(p) for p in spec.write_roots],
                  'protected': [str(settings.home)], 'private_root': str(private),
                  'cwd': str(spec.cwd), 'mcp_servers': list(spec.mcp_servers),
                  'role': role}
        data = {'cwd': str(spec.cwd), 'prompt': spec.prompt, 'system_note': spec.system_note,
                'engine_session_id': spec.engine_session_id, 'model': spec.model or settings.worker_default_models['muse']['model'],
                'effort': MUSE_EFFORT, 'write_roots': policy['write_roots'],
                'mcp_servers': spec.mcp_servers, 'muse_bin': settings.muse_bin,
                'runtime_root': str(settings.muse_runtime_root),
                'policy_file': str(private / 'policy.json'),
                'hook_python': sys.executable,
                'hook_script': str(Path(__file__).resolve().parents[1] / 'security/muse_hook.py')}
        for name, value in [('policy.json', policy), ('spec.json', data)]:
            path = private / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream)
        return [settings.muse_node_bin, str(runtime / 'bridge.mjs'), str(private / 'spec.json')]

    def cleanup(self, spec: LaunchSpec) -> None:
        private = self._resources.pop(id(spec), None)
        if private is not None:
            shutil.rmtree(private)

    def parse_line(self, obj: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]:
        method, p = obj.get('method'), obj.get('params') or {}
        if method in {'usage/changed', 'muse/quota'}:
            from ttakkari.runs.usage import muse_usage_from_probe
            observation = muse_usage_from_probe({'account': {'state': 'accountLogin'}, 'usage': {'usage': p}})
            if observation.windows and (not state.account_usage or
                    observation.updated_at > (state.account_usage.get('updated_at') or 0)):
                state.account_usage = observation.as_dict()
                return [Normalized('account.usage', state.account_usage)]
            return []
        if method == 'muse/sessionReady':
            session_id = p.get('sessionId')
            if state.engine_session_id and state.engine_session_id != session_id:
                state.resume_mismatch = state.is_error = True
                state.error = 'Muse resume identity mismatch'
                return [Normalized('error', {'message': state.error})]
            state.engine_session_id = session_id
            return [Normalized('agent.session', {'engine_session_id': session_id, 'model': p.get('modelId')})]
        if method == 'muse/error':
            state.is_error = True
            state.error = str(p.get('message') or 'Muse failed')
            return [Normalized('error', {'message': state.error, 'kind': p.get('kind')})]
        if p.get('sessionId') and state.engine_session_id and p['sessionId'] != state.engine_session_id:
            return []
        if method in {'item/started', 'item/completed'}:
            item = p.get('item') or {}
            iid, rev, kind = item.get('itemId'), item.get('revision', 0), item.get('kind')
            revisions = state.adapter_data.setdefault('revisions', {})
            if iid in revisions and revisions[iid] >= rev:
                return []
            revisions[iid] = rev
            done = method == 'item/completed'
            if kind == 'agentMessage' and done:
                text = item.get('text') or ''
                state.last_message = text
                if item.get('messagePhase') != 'commentary' and not item.get('cutOff'):
                    state.result_text = text
                return [Normalized('message', {'text': text, 'phase': item.get('messagePhase')})]
            if kind == 'reasoning' and done:
                return [Normalized('thinking', {'text': truncate(item.get('text'))})]
            if kind == 'toolCall':
                out = []
                called = state.adapter_data.setdefault('called', set())
                name = item.get('tool', '')
                try:
                    args = json.loads(item.get('args') or '{}')
                except (ValueError, TypeError):
                    args = {'raw': truncate(item.get('args'))}
                if iid not in called:
                    called.add(iid)
                    out.append(Normalized('tool.call', {'call_id': iid, 'name': name, 'input': args}))
                if done:
                    failed = item.get('status') != 'completed'
                    output = item.get('visibleOutput') or item.get('failureReason') or ''
                    binding_error = _mcp_binding_error(name, args, output, failed)
                    failed = failed or binding_error
                    out.append(Normalized('tool.result', {'call_id': iid, 'is_error': failed, 'output': truncate(output)}))
                    completed = state.adapter_data.setdefault('completed_tools', set())
                    if iid not in completed and name.startswith(('mcp__', 'mcp.')):
                        completed.add(iid)
                        streak = state.adapter_data.get('mcp_binding_streak', 0)
                        streak = streak + 1 if binding_error else 0
                        state.adapter_data['mcp_binding_streak'] = streak
                        if streak >= 2 and not state.adapter_data.get('mcp_binding_failure'):
                            state.adapter_data['mcp_binding_failure'] = True
                            state.is_error = True
                            state.error = f'Muse MCP binding failed repeatedly for {name}; preserving progress for provider handoff'
                            out.append(Normalized('error', {'message': state.error, 'kind': 'mcpBindingFailure'}))
                    if not failed and name in {'write_file', 'edit_file'} and isinstance(args, dict) and args.get('path'):
                        state.touched_files.add(abs_path(args['path'], cwd))
                return out
            if kind in {'subagent', 'workflow', 'hookRun', 'compaction'}:
                return [Normalized('log', {'stream': 'system', 'text': f'Muse {kind}: {item.get("status")} {item.get("summary") or item.get("fallbackText") or ""}'})]
        elif method == 'turn/completed':
            state.adapter_data['terminal_seen'] = True
            if p.get('terminal') != 'completed':
                state.is_error = True
                state.error = (p.get('error') or {}).get('message') or p.get('reason') or f'Muse turn {p.get("terminal")}'
                quota = (p.get('error') or {}).get('quota')
                if isinstance(quota, dict):
                    state.limit_hit = True
                    state.limit_text = state.error
                    reset = quota.get('resetsAtMs')
                    if isinstance(reset, int) and reset > 0:
                        state.limit_resets_at = reset / 1000
            state.usage.update({k: v for k, v in (p.get('usage') or {}).items() if isinstance(v, (int, float))})
            return [Normalized('usage', {'cost_usd': None, **state.usage})]
        elif method == 'session/tokenUsage':
            state.usage['muse_session_cumulative'] = p.get('cumulative') or {}
        elif method == 'session/contextUsage':
            state.context_tokens, state.context_window = p.get('usedTokens'), p.get('windowTokens')
        elif method in {'session/statusChanged', 'turn/modelRetryScheduled', 'turn/retryScheduled'}:
            return [Normalized('log', {'stream': 'system', 'text': f'Muse {method}: {p.get("status") or p.get("errorKind") or "retry scheduled"}'})]
        return []

    def finalize_process(self, state: ParseState, *, return_code: int | None, stderr: str) -> None:
        if not state.adapter_data.get('terminal_seen') and not state.is_error:
            state.is_error = True
            state.error = stderr.strip() or f'Muse exited without turn terminal (exit {return_code})'
