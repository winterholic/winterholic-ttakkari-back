"""Adapter for the official opencode CLI (headless JSON stream protocol).

Runs `opencode run --format json` with GitHub Copilot authenticated models.
Normalizes session, message, tool, usage, and error events to Ttakkari Run events.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from ttakkari.agents.base import LaunchSpec, Normalized, ParseState, abs_path, truncate
from ttakkari.config import get_settings
from ttakkari.runs import profiles as profiles_mod

log = logging.getLogger(__name__)

DEFAULT_COPILOT_MODEL = "github-copilot/gpt-5.6-sol"
FILE_WRITE_TOOLS = {"write", "edit", "patch", "file_write", "write_to_file", "replace_file_content"}


class OpencodeAdapter:
    """Adapter for `opencode run --format json` headless execution."""

    engine = "opencode"

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        settings = get_settings()
        binary = getattr(settings, "opencode_bin", "opencode")

        model = spec.model or DEFAULT_COPILOT_MODEL
        if not model.startswith("github-copilot/"):
            # Ensure github-copilot provider prefix for Copilot models
            model = f"github-copilot/{model}"

        prompt = (
            f"<ttakkari_system_instructions>\n{spec.system_note}\n</ttakkari_system_instructions>\n\n"
            f"Workspace root: {spec.cwd.resolve()}\n"
            f"Additional writable roots: {', '.join(str(p.resolve()) for p in spec.write_roots)}\n\n"
            f"{spec.prompt}"
        )

        cmd = [
            binary, "run",
            "--format", "json",
            "--pure",
            "--auto",
            "--dir", str(spec.cwd.resolve()),
            "--model", model,
        ]

        if spec.engine_session_id:
            cmd += ["--session", spec.engine_session_id]

        if spec.effort and spec.effort in {"low", "medium", "high", "max"}:
            cmd += ["--variant", spec.effort]

        if spec.session_id and spec.mcp_servers:
            self._prepare_mcp(spec.session_id, spec.mcp_servers)

        cmd.append(prompt)
        return cmd

    def working_directory(self, spec: LaunchSpec) -> Path:
        if spec.session_id and spec.mcp_servers:
            return Path("/private/tmp/ttakkari-opencode") / spec.session_id
        return spec.cwd

    def _prepare_mcp(self, session_id: str, servers: dict[str, dict[str, Any]]) -> Path:
        try:
            uuid.UUID(session_id)
        except ValueError as exc:
            raise ValueError("OpenCode session requires a UUID workspace key") from exc

        base = Path("/private/tmp/ttakkari-opencode") / session_id
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if base.is_symlink():
            raise ValueError("OpenCode session directory cannot be a symlink")

        mcp_config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {}
        }
        for name, srv in servers.items():
            cmd_args = [srv.get("command", "")]
            if srv.get("args"):
                cmd_args.extend(srv["args"])
            mcp_config["mcp"][name] = {
                "type": "local",
                "command": cmd_args,
                "environment": srv.get("env", {}),
            }

        config_path = base / "opencode.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(config_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(mcp_config, stream, ensure_ascii=False, indent=2)
        return base

    def cleanup(self, spec: LaunchSpec) -> None:
        if not spec.session_id:
            return
        config = Path("/private/tmp/ttakkari-opencode") / spec.session_id / "opencode.json"
        try:
            config.unlink(missing_ok=True)
        except OSError:
            pass

    def finalize_process(self, state: ParseState, *, return_code: int | None, stderr: str) -> None:
        if return_code != 0 and not state.is_error:
            state.is_error = True
            state.error = stderr.strip()[-4000:] or f"OpenCode exited with return code {return_code}"
        elif return_code == 0 and not state.is_error and state.result_text is None:
            state.is_error = True
            state.error = stderr.strip()[-4000:] or "OpenCode exited without a final response"

    def parse_line(self, obj: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]:
        event_type = obj.get("type")
        session_id = obj.get("sessionID")

        if event_type == "step_start":
            if session_id:
                if state.engine_session_id and session_id != state.engine_session_id:
                    state.resume_mismatch = True
                    state.is_error = True
                    state.error = (
                        f"OpenCode could not resume the requested session {state.engine_session_id!r}; "
                        f"it opened {session_id!r} instead"
                    )
                    return [Normalized("error", {"message": state.error})]
                state.engine_session_id = session_id
                part = obj.get("part") or {}
                return [Normalized("agent.session", {
                    "engine_session_id": state.engine_session_id,
                    "model": part.get("modelID") or obj.get("model"),
                })]
            return []

        if event_type == "text":
            part = obj.get("part") or {}
            text = part.get("text", "")
            if text:
                state.last_message = (state.last_message or "") + text
                state.result_text = (state.result_text or "") + text
                return [Normalized("message", {"text": text})]
            return []

        if event_type == "reasoning":
            part = obj.get("part") or {}
            text = part.get("text", "")
            if text:
                # Include thinking in last_message commentary
                return [Normalized("message", {"text": f"[Thinking] {text}\n"})]
            return []

        if event_type == "tool_use":
            part = obj.get("part") or {}
            tool_name = part.get("tool") or "unknown"
            tool_state = part.get("state") or {}
            call_id = part.get("callID") or part.get("id") or f"oc:{tool_name}"
            raw_input = tool_state.get("input") or {}
            output = tool_state.get("output") or tool_state.get("error") or ""
            status = tool_state.get("status")
            is_error = status == "error" or bool(tool_state.get("error"))

            out: list[Normalized] = []
            if call_id not in state.pending_tool_calls:
                state.pending_tool_calls.add(call_id)
                out.append(Normalized("tool.call", {
                    "call_id": call_id,
                    "name": tool_name,
                    "input": _slim_input(tool_name, raw_input),
                }))

            if status in {"completed", "error"}:
                state.pending_tool_calls.discard(call_id)
                if tool_name in FILE_WRITE_TOOLS:
                    path = raw_input.get("path") or raw_input.get("filePath") or raw_input.get("file")
                    if isinstance(path, str):
                        state.touched_files.add(abs_path(path, cwd))

                trunc_output = truncate(output)
                out.append(Normalized("tool.result", {
                    "call_id": call_id,
                    "is_error": is_error,
                    "output": trunc_output,
                }))

                if is_error and "[ttakkari-guard]" in trunc_output:
                    out.append(Normalized("guard.blocked", {"call_id": call_id, "reason": trunc_output}))

            return out

        if event_type == "step_finish":
            part = obj.get("part") or {}
            tokens = part.get("tokens") or {}
            if isinstance(tokens, dict):
                state.usage.update({
                    "input_tokens": tokens.get("input", 0),
                    "output_tokens": tokens.get("output", 0),
                    "total_tokens": tokens.get("total", 0),
                })
            cost = part.get("cost")
            if isinstance(cost, (int, float)):
                state.cost_usd = float(cost)
            return [Normalized("usage", {"cost_usd": state.cost_usd, **state.usage})]

        if event_type == "error":
            err = obj.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            state.is_error = True
            state.error = msg or "OpenCode stream error"

            # Check if this error was a usage limit or rate limit
            if profiles_mod.is_usage_limit(state.error):
                state.limit_hit = True
                state.limit_text = state.error
                state.limit_resets_at = profiles_mod.reset_epoch_from(state.error)

            return [Normalized("error", {"message": state.error})]

        return []


def _slim_input(name: str, params: dict[str, Any]) -> dict[str, Any]:
    if name in FILE_WRITE_TOOLS:
        path = params.get("path") or params.get("filePath") or params.get("file")
        content = params.get("content") or params.get("text")
        return {"path": path, "bytes": len(content) if isinstance(content, str) else None}
    return {
        key: truncate(value, 2000) if isinstance(value, str) else value
        for key, value in params.items()
        if key.lower() not in {"content", "contents", "text"}
    }
