from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ttakkari.agents.base import (
    GUARD_MARKER,
    LaunchSpec,
    Normalized,
    ParseState,
    abs_path,
    truncate,
)
from ttakkari.config import get_settings

FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


class ClaudeAdapter:
    engine = "claude"

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        s = get_settings()
        hook_cmd = f"{sys.executable} -m ttakkari.security.claude_hook"
        settings = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook_cmd}]}]}}
        cmd = [
            s.claude_bin, "-p", spec.prompt,
            "--output-format", "stream-json", "--verbose",
            # 사용자 결정: 전부 자동 실행. 보험은 PreToolUse guard hook 으로 건다(ADR 0003).
            "--dangerously-skip-permissions",
            "--settings", json.dumps(settings),
            "--append-system-prompt", spec.system_note,
            "--add-dir", str(spec.outbox),
        ]
        if s.claude_setting_sources:
            cmd += ["--setting-sources", s.claude_setting_sources]
        if spec.engine_session_id:
            cmd += ["--resume", spec.engine_session_id]
        if spec.model:
            cmd += ["--model", spec.model]
        if spec.effort:
            cmd += ["--effort", spec.effort]
        return cmd

    def parse_line(self, obj: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]:
        t = obj.get("type")
        out: list[Normalized] = []
        if t == "system" and obj.get("subtype") == "init":
            state.engine_session_id = obj.get("session_id")
            out.append(Normalized("agent.session", {
                "engine_session_id": state.engine_session_id, "model": obj.get("model"),
            }))
        elif t == "assistant":
            for block in obj.get("message", {}).get("content", []):
                bt = block.get("type")
                if bt == "text" and block.get("text"):
                    state.last_message = block["text"]
                    out.append(Normalized("message", {"text": block["text"]}))
                elif bt == "thinking" and block.get("thinking"):
                    out.append(Normalized("thinking", {"text": truncate(block["thinking"])}))
                elif bt == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input") or {}
                    if name in FILE_WRITE_TOOLS:
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if isinstance(fp, str):
                            state.touched_files.add(abs_path(fp, cwd))
                    out.append(Normalized("tool.call", {
                        "call_id": block.get("id"), "name": name, "input": _slim_input(name, inp),
                    }))
        elif t == "user":
            content = obj.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        text = truncate(block.get("content"))
                        out.append(Normalized("tool.result", {
                            "call_id": block.get("tool_use_id"),
                            "is_error": bool(block.get("is_error")),
                            "output": text,
                        }))
                        if GUARD_MARKER in text:
                            out.append(Normalized("guard.blocked", {"call_id": block.get("tool_use_id"), "reason": text}))
        elif t == "result":
            state.result_text = obj.get("result")
            state.is_error = bool(obj.get("is_error")) or obj.get("subtype") not in (None, "success")
            if state.is_error:
                state.error = obj.get("result") or obj.get("subtype")
            state.cost_usd = obj.get("total_cost_usd")
            state.usage = {
                "duration_ms": obj.get("duration_ms"),
                "num_turns": obj.get("num_turns"),
                **{k: v for k, v in (obj.get("usage") or {}).items() if isinstance(v, (int, float))},
            }
            out.append(Normalized("usage", {"cost_usd": state.cost_usd, **state.usage}))
        return out


def _slim_input(name: str, inp: dict[str, Any]) -> dict[str, Any]:
    # 모바일로 보내는 이벤트라 파일 본문 전체를 싣지 않는다.
    if name == "Write":
        return {"file_path": inp.get("file_path"), "bytes": len(str(inp.get("content", "")))}
    if name in ("Edit", "MultiEdit"):
        return {"file_path": inp.get("file_path")}
    return {k: (truncate(v, 2000) if isinstance(v, str) else v) for k, v in inp.items()}
