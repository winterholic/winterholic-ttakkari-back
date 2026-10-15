from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from ttakkari.agents.base import LaunchSpec, Normalized, ParseState, abs_path, truncate
from ttakkari.config import get_settings

FILE_WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content", "notebook_edit"}


class AntigravityAdapter:
    """Adapter for the installed official agy headless CLI (NDJSON stream protocol)."""

    engine = "antigravity"

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        settings = get_settings()
        run_id = extra_env.get("TTAKKARI_RUN_ID", "standalone")
        log_file = Path("/private/tmp") / f"ttakkari-agy-{run_id}.log"
        if spec.session_id and spec.mcp_servers:
            self._session_project(spec.session_id, spec.mcp_servers)
        prompt = (
            f"<ttakkari_system_instructions>\n{spec.system_note}\n</ttakkari_system_instructions>\n\n"
            f"Workspace root: {spec.cwd.resolve()}\n"
            f"Additional writable roots: {', '.join(str(p.resolve()) for p in spec.write_roots)}\n\n"
            f"{spec.prompt}"
        )
        cmd = [
            settings.antigravity_bin, "-p", prompt,
            "--output-format", "stream-json",
            "--print-timeout", f"{settings.run_timeout_seconds}s",
            "--log-file", str(log_file),
            "--mode", "accept-edits",
            "--sandbox",
        ]
        if spec.engine_session_id:
            cmd += ["--conversation", spec.engine_session_id]
        if spec.model:
            cmd += ["--model", spec.model]
        if spec.effort:
            cmd += ["--effort", spec.effort]
        for path in dict.fromkeys([*spec.write_roots, spec.outbox]):
            if path.resolve() != spec.cwd.resolve():
                cmd += ["--add-dir", str(path)]
        return cmd

    def working_directory(self, spec: LaunchSpec) -> Path:
        if spec.session_id and spec.mcp_servers:
            return Path("/private/tmp/ttakkari-antigravity") / spec.session_id
        return spec.cwd

    def _session_project(self, session_id: str, servers: dict[str, dict[str, Any]]) -> Path:
        try:
            uuid.UUID(session_id)
        except ValueError as exc:
            raise ValueError("Antigravity session requires a UUID workspace key") from exc
        base = Path("/private/tmp/ttakkari-antigravity") / session_id
        base.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if base.is_symlink():
            raise ValueError("Antigravity session directory cannot be a symlink")
        base.mkdir(mode=0o700, exist_ok=True)
        agents = base / ".agents"
        if agents.is_symlink():
            raise ValueError("Antigravity session MCP directory cannot be a symlink")
        agents.mkdir(mode=0o700, exist_ok=True)
        config = {
            "mcpServers": {
                name: {
                    key: value for key, value in server.items()
                    if key in {"command", "args", "env", "cwd", "serverUrl", "headers", "disabledTools"}
                }
                for name, server in servers.items()
            }
        }
        config_path = agents / "mcp_config.json"
        if config_path.is_symlink():
            raise ValueError("Antigravity session MCP config cannot be a symlink")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(config_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
        return base

    def cleanup(self, spec: LaunchSpec) -> None:
        if not spec.session_id:
            return
        config = Path("/private/tmp/ttakkari-antigravity") / spec.session_id / ".agents" / "mcp_config.json"
        try:
            config.unlink(missing_ok=True)
        except OSError:
            pass

    def finalize_process(self, state: ParseState, *, return_code: int | None, stderr: str) -> None:
        """Treat a silent zero-exit CLI as a failed run and preserve its diagnostic."""
        if return_code == 0 and not state.is_error and state.result_text is None:
            state.is_error = True
            state.error = stderr.strip()[-8000:] or "Antigravity exited without a final response"

    def parse_line(self, obj: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]:
        event = obj.get("event")
        if event == "init":
            conversation_id = obj.get("conversation_id")
            if state.engine_session_id and conversation_id != state.engine_session_id:
                state.resume_mismatch = True
                state.is_error = True
                state.error = (
                    "Antigravity could not resume the requested conversation "
                    f"{state.engine_session_id!r}; it opened {conversation_id!r} instead"
                )
                return [Normalized("error", {"message": state.error})]
            state.engine_session_id = conversation_id
            return [Normalized("agent.session", {
                "engine_session_id": state.engine_session_id,
                "model": (obj.get("init") or {}).get("model"),
            })]
        if event == "step_update":
            return self._step(obj.get("step_update") or {}, state, cwd)
        if event == "result":
            result = obj.get("result") or {}
            if not state.resume_mismatch:
                state.engine_session_id = result.get("conversation_id") or state.engine_session_id
            state.result_text = result.get("response")
            state.usage = {
                key: value for key, value in (result.get("usage") or {}).items()
                if isinstance(value, (int, float))
            }
            status = result.get("status")
            state.is_error = status != "SUCCESS" or not state.result_text
            if state.is_error:
                state.error = (result.get("error") or result.get("response")
                               or ("Antigravity returned an empty final response" if status == "SUCCESS"
                                   else f"Antigravity status: {status}"))
            out = [Normalized("usage", {"cost_usd": None, **state.usage})]
            if state.is_error:
                out.append(Normalized("error", {"message": state.error}))
            return out
        if event == "error":
            state.is_error = True
            state.error = obj.get("message") or "Antigravity stream error"
            return [Normalized("error", {"message": state.error})]
        return []

    def _step(self, step: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]:
        kind = step.get("step_type")
        out: list[Normalized] = []
        if kind == "agent_response" and step.get("text_delta"):
            text = step["text_delta"]
            state.last_message = (state.last_message or "") + text
            out.append(Normalized("message", {"text": text}))
        if kind != "tool":
            if isinstance(step.get("usage"), dict):
                state.usage.update({k: v for k, v in step["usage"].items() if isinstance(v, (int, float))})
            return out

        info = step.get("tool_info") or {}
        name = info.get("name") or step.get("tool_name") or "unknown"
        params = info.get("parameters") or {}
        call_id = f"agy:{step.get('step_index', 'unknown')}"
        if step.get("state") == "ACTIVE":
            state.pending_tool_calls.add(call_id)
            return [Normalized("tool.call", {"call_id": call_id, "name": name, "input": _slim_input(name, params)})]

        if name in FILE_WRITE_TOOLS:
            raw_path = _field(params, "FilePath", "TargetFile", "filePath", "path", "notebookPath")
            if isinstance(raw_path, str):
                state.touched_files.add(abs_path(raw_path, cwd))
        error = info.get("error")
        is_error = bool(error) or bool(step.get("is_error"))
        error_text = error.get("message") if isinstance(error, dict) else error
        output = truncate(info.get("output") or error_text or "")
        if call_id not in state.pending_tool_calls:
            out.append(Normalized("tool.call", {"call_id": call_id, "name": name, "input": _slim_input(name, params)}))
        state.pending_tool_calls.discard(call_id)
        out.append(Normalized("tool.result", {"call_id": call_id, "is_error": is_error, "output": output}))
        if is_error and "[ttakkari-guard]" in output:
            out.append(Normalized("guard.blocked", {"call_id": call_id, "reason": output}))
        return out


def _field(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _slim_input(name: str, params: dict[str, Any]) -> dict[str, Any]:
    if name in FILE_WRITE_TOOLS:
        path = _field(params, "FilePath", "TargetFile", "filePath", "path", "notebookPath")
        content = _field(params, "Content", "content", "newContent")
        return {"path": path, "bytes": len(content) if isinstance(content, str) else None}
    if name in {"view_file", "read_file", "sed_file"}:
        return {"path": _field(params, "FilePath", "TargetFile", "filePath", "path")}
    return {key: truncate(value, 2000) if isinstance(value, str) else value
            for key, value in params.items() if key.lower() not in {"content", "contents", "text"}}
