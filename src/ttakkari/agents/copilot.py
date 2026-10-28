"""Adapter for the official GitHub Copilot CLI (headless JSON stream protocol).

Runs `copilot -p <prompt> --output-format json --allow-all-tools` with official GitHub Copilot models.
Normalizes session, message, tool, usage, and error events to Ttakkari Run events.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ttakkari.agents.base import LaunchSpec, Normalized, ParseState, truncate
from ttakkari.config import get_settings
from ttakkari.oplog.scrub import scrub
from ttakkari.runs import profiles as profiles_mod

log = logging.getLogger(__name__)

DEFAULT_COPILOT_MODEL = "auto"
FILE_WRITE_TOOLS = {"write", "edit", "patch", "file_write", "write_to_file", "replace_file_content"}


def _get_session_dir(session_id: str) -> Path:
    dev_tmp = Path("/Users/winterholic/development/.tmp")
    root = dev_tmp if dev_tmp.is_dir() else Path(tempfile.gettempdir())
    return root / "ttakkari-copilot" / session_id


class CopilotAdapter:
    """Adapter for official GitHub Copilot CLI headless execution."""

    engine = "copilot"

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        settings = get_settings()
        binary = getattr(settings, "copilot_bin", "/opt/homebrew/bin/copilot")

        model = spec.model or DEFAULT_COPILOT_MODEL

        prompt = (
            f"<ttakkari_system_instructions>\n{spec.system_note}\n</ttakkari_system_instructions>\n\n"
            f"Workspace root: {spec.cwd.resolve()}\n"
            f"Additional writable roots: {', '.join(str(p.resolve()) for p in spec.write_roots)}\n\n"
            f"{spec.prompt}"
        )

        cmd = [
            binary,
            "-p", prompt,
            "--output-format", "json",
            "--allow-all-tools",
            "--no-ask-user",
            "--no-auto-update",
            "--stream", "on",
            "--model", model,
        ]

        if spec.cwd:
            cmd.extend(["-C", str(spec.cwd.resolve())])

        if spec.engine_session_id:
            cmd.extend(["--session-id", spec.engine_session_id])

        if model != "auto" and spec.effort and spec.effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            cmd.extend(["--reasoning-effort", spec.effort])

        if spec.session_id and spec.mcp_servers:
            mcp_file = self._prepare_mcp(spec.session_id, spec.mcp_servers)
            cmd.extend(["--additional-mcp-config", f"@{mcp_file}"])

        return cmd

    def working_directory(self, spec: LaunchSpec) -> Path:
        return spec.cwd

    def _prepare_mcp(self, session_id: str, servers: dict[str, dict[str, Any]]) -> Path:
        try:
            uuid.UUID(session_id)
        except ValueError as exc:
            raise ValueError("Copilot session requires a UUID workspace key") from exc

        base = _get_session_dir(session_id)
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if base.is_symlink():
            raise ValueError("Copilot session directory cannot be a symlink")

        mcp_config = {
            "mcpServers": {}
        }
        for name, srv in servers.items():
            cmd = srv.get("command", "")
            args = srv.get("args") or []
            mcp_config["mcpServers"][name] = {
                "command": cmd,
                "args": args,
                "env": srv.get("env", {}),
            }

        config_path = base / "mcp-config.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(config_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(mcp_config, stream, ensure_ascii=False, indent=2)
        return config_path

    def cleanup(self, spec: LaunchSpec) -> None:
        if not spec.session_id:
            return
        config = _get_session_dir(spec.session_id) / "mcp-config.json"
        try:
            config.unlink(missing_ok=True)
        except OSError:
            pass

    def finalize_process(self, state: ParseState, *, return_code: int | None, stderr: str) -> None:
        if return_code != 0 and not state.is_error:
            state.is_error = True
            state.error = scrub(stderr.strip())[-4000:] or f"Copilot exited with return code {return_code}"
        elif return_code == 0 and not state.is_error and (not state.protocol_final_seen or not (state.result_text or "").strip()):
            state.is_error = True
            state.error = scrub(stderr.strip())[-4000:] or "Copilot exited without a final response"

    def parse_line(self, obj: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]:
        event_type = obj.get("type")
        data = obj.get("data") or {}

        if event_type == "session.usage_info":
            tokens, window = data.get("currentTokens"), data.get("tokenLimit")
            if type(tokens) is int and tokens >= 0 and type(window) is int and window > 0:
                state.context_tokens, state.context_window = tokens, window
                return [Normalized("usage", {"context_tokens": tokens, "context_window": window,
                                              "context_percent": round(tokens * 100 / window)})]
            return []

        # 1. Session resolution and auto routing
        if event_type == "session.auto_mode_resolved":
            chosen_model = data.get("chosenModel")
            if chosen_model:
                state.context_model = chosen_model
                state.adapter_data["model"] = chosen_model

        # 2. Text delta
        if event_type == "assistant.message_delta":
            delta = data.get("deltaContent", "")
            if delta:
                state.last_message = (state.last_message or "") + delta
                state.result_text = (state.result_text or "") + delta
                return [Normalized("message", {"text": delta})]
            return []

        # 3. Complete assistant message
        if event_type == "assistant.message":
            content = data.get("content", "")
            tool_requests = data.get("toolRequests") or []
            out: list[Normalized] = []

            if content and not state.result_text:
                state.last_message = (state.last_message or "") + content
                state.result_text = (state.result_text or "") + content
                out.append(Normalized("message", {"text": content}))

            for req in tool_requests:
                call_id = req.get("toolCallId") or f"call_{len(state.pending_tool_calls)}"
                name = req.get("name") or "unknown"
                raw_input = req.get("arguments") or {}
                if call_id not in state.pending_tool_calls:
                    state.pending_tool_calls.add(call_id)
                    out.append(Normalized("tool.call", {
                        "call_id": call_id,
                        "name": name,
                        "input": _slim_input(name, raw_input),
                    }))
            return out

        # 4. Tool execution start
        if event_type == "tool.execution_start":
            call_id = data.get("toolCallId")
            name = data.get("toolName") or "unknown"
            raw_input = data.get("arguments") or {}
            if call_id and call_id not in state.pending_tool_calls:
                state.pending_tool_calls.add(call_id)
                return [Normalized("tool.call", {
                    "call_id": call_id,
                    "name": name,
                    "input": _slim_input(name, raw_input),
                })]
            return []

        # 5. Tool execution complete
        if event_type == "tool.execution_complete":
            call_id = data.get("toolCallId")
            success = bool(data.get("success", True))
            res_obj = data.get("result") or {}
            output = res_obj.get("content") or res_obj.get("detailedContent") or ""
            err_obj = data.get("error")
            is_error = not success or bool(err_obj)

            if err_obj and not output:
                output = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)

            if call_id:
                state.pending_tool_calls.discard(call_id)

            trunc_output = truncate(output)
            out = [Normalized("tool.result", {
                "call_id": call_id or "unknown",
                "is_error": is_error,
                "output": trunc_output,
            })]

            if is_error and "[ttakkari-guard]" in trunc_output:
                out.append(Normalized("guard.blocked", {"call_id": call_id, "reason": trunc_output}))

            return out

        # 6. Final result
        if event_type == "result":
            state.protocol_final_seen = True
            session_id = obj.get("sessionId")
            exit_code = obj.get("exitCode", 0)
            usage = obj.get("usage") or {}

            if session_id:
                if state.engine_session_id and session_id != state.engine_session_id:
                    state.resume_mismatch = True
                    state.is_error = True
                    state.error = (
                        f"Copilot could not resume the requested session {state.engine_session_id!r}; "
                        f"it opened {session_id!r} instead"
                    )
                    return [Normalized("error", {"message": state.error})]
                state.engine_session_id = session_id

            if exit_code != 0:
                state.is_error = True

            out = []
            if state.engine_session_id:
                out.append(Normalized("agent.session", {
                    "engine_session_id": state.engine_session_id,
                    "model": state.context_model or state.adapter_data.get("model", DEFAULT_COPILOT_MODEL),
                }))

            if usage:
                tokens = usage.get("tokenDetails") or {}
                input_tokens = tokens.get("input", {}).get("tokenCount", 0)
                output_tokens = tokens.get("output", {}).get("tokenCount", 0)
                state.usage.update({
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "premium_requests": usage.get("premiumRequests", 0),
                })
                out.append(Normalized("usage", {"cost_usd": 0.0, **state.usage}))

            return out

        # 7. Error events
        if event_type == "error":
            err = obj.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            state.is_error = True
            state.error = msg or "Copilot stream error"

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
