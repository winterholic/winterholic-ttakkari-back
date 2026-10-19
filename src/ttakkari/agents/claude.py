from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
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

    def __init__(self) -> None:
        self._resources: dict[int, Path] = {}

    def _write_mcp_config(self, spec: LaunchSpec) -> Path:
        """MCP 서버 명세(세션 토큰 포함)를 0600 파일로 쓰고 경로를 돌려준다.

        SEC2-AGT-01: 토큰이 argv(ps)에 노출되지 않게 JSON 문자열 전달을 파일 전달로 바꿨다.
        """
        self.cleanup(spec)
        private = Path(tempfile.mkdtemp(prefix="ttakkari-claude-"))
        self._resources[id(spec)] = private
        servers = {name: {"type": "stdio", **srv} for name, srv in spec.mcp_servers.items()}
        path = private / "mcp_config.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"mcpServers": servers}, stream)
        return path

    def cleanup(self, spec: LaunchSpec) -> None:
        private = self._resources.pop(id(spec), None)
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)

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
        if spec.mcp_servers:
            cmd += ["--mcp-config", str(self._write_mcp_config(spec))]
        sources = spec.setting_sources or s.claude_setting_sources
        if sources:
            cmd += ["--setting-sources", sources]
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
            limited = obj.get("error") == "rate_limit"
            if limited:
                state.limit_hit = True
            # 서브에이전트(parent_tool_use_id)의 요청은 메인 대화 컨텍스트가 아니다.
            if not obj.get("parent_tool_use_id"):
                _note_context(obj.get("message") or {}, state)
            for block in obj.get("message", {}).get("content", []):
                bt = block.get("type")
                if bt == "text" and block.get("text"):
                    state.last_message = block["text"]
                    if limited:
                        state.limit_text = block["text"]
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
        elif t == "rate_limit_event":
            info = obj.get("rate_limit_info")
            if isinstance(info, dict):
                state.rate_limits = info
                if info.get("status") == "rejected":
                    state.limit_hit = True
                    resets = info.get("resetsAt")
                    if isinstance(resets, (int, float)) and resets > 0:
                        state.limit_resets_at = float(resets)
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
            mu = (obj.get("modelUsage") or {}).get(state.context_model or "") or {}
            if isinstance(mu.get("contextWindow"), int):
                state.context_window = mu["contextWindow"]
            out.append(Normalized("usage", {"cost_usd": state.cost_usd, **state.usage}))
        return out


def _note_context(message: dict[str, Any], state: ParseState) -> None:
    u = message.get("usage")
    if not isinstance(u, dict):
        return
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    total = sum(v for k in keys if isinstance(v := u.get(k), int))
    if total:
        state.context_tokens = total
        state.context_model = message.get("model") or state.context_model


def _slim_input(name: str, inp: dict[str, Any]) -> dict[str, Any]:
    # 모바일로 보내는 이벤트라 파일 본문 전체를 싣지 않는다.
    if name == "Write":
        return {"file_path": inp.get("file_path"), "bytes": len(str(inp.get("content", "")))}
    if name in ("Edit", "MultiEdit"):
        return {"file_path": inp.get("file_path")}
    return {k: (truncate(v, 2000) if isinstance(v, str) else v) for k, v in inp.items()}
