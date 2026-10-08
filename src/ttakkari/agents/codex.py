from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ttakkari.agents.base import LaunchSpec, Normalized, ParseState, abs_path, truncate
from ttakkari.config import get_settings

SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}


class CodexAdapter:
    engine = "codex"

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        s = get_settings()
        sandbox = spec.codex_sandbox if spec.codex_sandbox in SANDBOX_MODES else "workspace-write"
        writable = [str(p) for p in spec.write_roots]
        opts = [
            "--json", "--skip-git-repo-check",
            # 사용자 전역 config 가 danger-full-access 여도 워크스페이스 설정으로 덮는다. OS 샌드박스가 codex 의 보험이다.
            "-c", f'sandbox_mode="{sandbox}"',
            "-c", 'approval_policy="never"',
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", f"sandbox_workspace_write.writable_roots={json.dumps(writable)}",
            "-c", f"developer_instructions={json.dumps(spec.system_note)}",
        ]
        if spec.model:
            opts += ["-m", spec.model]
        if spec.effort:
            opts += ["-c", f'model_reasoning_effort="{spec.effort}"']
        if spec.engine_session_id:
            return [s.codex_bin, "exec", "resume", *opts, spec.engine_session_id, "--", spec.prompt]
        return [s.codex_bin, "exec", *opts, "-C", str(spec.cwd), "--", spec.prompt]

    def parse_line(self, obj: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]:
        t = obj.get("type")
        out: list[Normalized] = []
        if t == "thread.started":
            state.engine_session_id = obj.get("thread_id")
            out.append(Normalized("agent.session", {"engine_session_id": state.engine_session_id, "model": None}))
        elif t in ("item.started", "item.completed"):
            out.extend(self._item(obj.get("item") or {}, t == "item.completed", state, cwd))
        elif t == "turn.completed":
            usage = obj.get("usage") or {}
            state.usage = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
            out.append(Normalized("usage", {"cost_usd": None, **state.usage}))
        elif t == "turn.failed":
            state.is_error = True
            state.error = (obj.get("error") or {}).get("message") or "turn failed"
        elif t == "error":
            state.is_error = True
            state.error = obj.get("message") or "error"
            out.append(Normalized("error", {"message": state.error}))
        return out

    def _item(self, item: dict[str, Any], done: bool, state: ParseState, cwd: Path) -> list[Normalized]:
        it = item.get("type") or item.get("item_type")
        iid = item.get("id")
        if it == "agent_message":
            if not done:
                return []
            text = item.get("text") or ""
            state.last_message = text
            state.result_text = text
            return [Normalized("message", {"text": text})]
        if it == "reasoning":
            return [Normalized("thinking", {"text": truncate(item.get("text"))})] if done and item.get("text") else []
        if it == "command_execution":
            if not done:
                return [Normalized("tool.call", {"call_id": iid, "name": "shell", "input": {"command": item.get("command")}})]
            code = item.get("exit_code")
            return [Normalized("tool.result", {
                "call_id": iid, "is_error": code not in (0, None) or item.get("status") == "failed",
                "output": truncate(item.get("aggregated_output")), "exit_code": code,
            })]
        if it == "file_change":
            if not done:
                return []
            changes = item.get("changes") or []
            for c in changes:
                if isinstance(c, dict) and isinstance(c.get("path"), str) and c.get("kind") != "delete":
                    state.touched_files.add(abs_path(c["path"], cwd))
            return [
                Normalized("tool.call", {"call_id": iid, "name": "file_change", "input": {"changes": changes}}),
                Normalized("tool.result", {"call_id": iid, "is_error": item.get("status") == "failed", "output": ""}),
            ]
        if it == "mcp_tool_call":
            name = f"mcp:{item.get('server')}/{item.get('tool')}"
            if not done:
                return [Normalized("tool.call", {"call_id": iid, "name": name, "input": item.get("arguments") or {}})]
            return [Normalized("tool.result", {"call_id": iid, "is_error": item.get("status") == "failed",
                                               "output": truncate(item.get("result") or item.get("error"))})]
        if it == "web_search":
            return [] if done else [Normalized("tool.call", {"call_id": iid, "name": "web_search", "input": {"query": item.get("query")}})]
        if it == "todo_list":
            return [Normalized("plan", {"items": item.get("items") or []})]
        if it == "error":
            return [Normalized("error", {"message": item.get("message")})] if done else []
        return []
