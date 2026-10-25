from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from ttakkari.agents.base import LaunchSpec, Normalized, ParseState, abs_path, truncate
from ttakkari.config import get_settings
from ttakkari.model_catalog import supports_effort

FILE_WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content", "notebook_edit"}
DIAGRAM_SKILLS = ("diagram-design", "ttakkari-diagram")
DIAGRAM_MANIFEST = "ttakkari-diagram-skills.json"
log = logging.getLogger(__name__)


def _diagram_skill_sources(write_roots: list[Path]) -> dict[str, Path]:
    """Use only the shared skill root already authorized by this workspace.

    Never discover through an account HOME or grant a new filesystem root.
    Both siblings are necessary for the thin brand skill's relative references.
    """
    for root in write_roots:
        if root.name != ".claude":
            continue
        sources = {name: root / "skills" / name for name in DIAGRAM_SKILLS}
        for source in sources.values():
            if not source.resolve().is_relative_to(root.resolve()):
                raise ValueError("Antigravity diagram skill escapes authorized shared root")
            if not (source / "SKILL.md").resolve().is_relative_to(root.resolve()):
                raise ValueError("Antigravity diagram skill file escapes authorized shared root")
        if all((source / "SKILL.md").is_file() for source in sources.values()):
            return {name: source.resolve() for name, source in sources.items()}
        log.info("Antigravity diagram skills skipped: shared dependency pair is incomplete")
    return {}


def _clear_diagram_skills(agents: Path) -> None:
    manifest = agents / DIAGRAM_MANIFEST
    skills = agents / "skills"
    if agents.is_symlink() or skills.is_symlink() or manifest.is_symlink():
        raise ValueError("Antigravity diagram skill metadata cannot be a symlink")
    if not manifest.exists():
        return
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(entries, dict) or not set(entries).issubset(DIAGRAM_SKILLS):
        raise ValueError("Invalid Antigravity diagram skill manifest")
    for name, target in entries.items():
        link = skills / name
        # Delete only links owned by this bridge, never source assets or replacements.
        if isinstance(target, str) and link.is_symlink() and str(link.readlink()) == target:
            link.unlink()
    manifest.unlink()


def _sync_diagram_skills(agents: Path, sources: dict[str, Path]) -> None:
    _clear_diagram_skills(agents)
    if not sources:
        return
    skills = agents / "skills"
    skills.mkdir(mode=0o700, exist_ok=True)
    for name in sources:
        if (skills / name).exists() or (skills / name).is_symlink():
            raise ValueError(f"Antigravity diagram skill conflicts with existing entry: {name}")
    manifest = agents / DIAGRAM_MANIFEST
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(manifest, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({name: str(source) for name, source in sources.items()}, stream)
    try:
        for name, source in sources.items():
            (skills / name).symlink_to(source, target_is_directory=True)
    except OSError:
        _clear_diagram_skills(agents)
        raise


class AntigravityAdapter:
    """Adapter for the installed official agy headless CLI (NDJSON stream protocol)."""

    engine = "antigravity"

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        settings = get_settings()
        run_id = extra_env.get("TTAKKARI_RUN_ID", "standalone")
        log_file = Path("/private/tmp") / f"ttakkari-agy-{run_id}.log"
        if spec.session_id and spec.mcp_servers:
            self._session_project(spec.session_id, spec.mcp_servers,
                                  _diagram_skill_sources(spec.write_roots))
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
            "--dangerously-skip-permissions",
            "--sandbox",
        ]
        if spec.engine_session_id:
            cmd += ["--conversation", spec.engine_session_id]
        if spec.model:
            cmd += ["--model", spec.model]
        if spec.effort and supports_effort(self.engine, spec.model):
            cmd += ["--effort", spec.effort]
        for path in dict.fromkeys([*spec.write_roots, spec.outbox]):
            if path.resolve() != spec.cwd.resolve():
                cmd += ["--add-dir", str(path)]
        return cmd

    def working_directory(self, spec: LaunchSpec) -> Path:
        if spec.session_id and spec.mcp_servers:
            return Path("/private/tmp/ttakkari-antigravity") / spec.session_id
        return spec.cwd

    def _session_project(self, session_id: str, servers: dict[str, dict[str, Any]],
                         skill_sources: dict[str, Path] | None = None) -> Path:
        try:
            uuid.UUID(session_id)
        except ValueError as exc:
            raise ValueError("Antigravity session requires a UUID workspace key") from exc
        base = Path("/private/tmp/ttakkari-antigravity") / session_id
        if base.parent.is_symlink():
            raise ValueError("Antigravity session parent cannot be a symlink")
        base.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if base.is_symlink():
            raise ValueError("Antigravity session directory cannot be a symlink")
        base.mkdir(mode=0o700, exist_ok=True)
        agents = base / ".agents"
        if agents.is_symlink():
            raise ValueError("Antigravity session MCP directory cannot be a symlink")
        agents.mkdir(mode=0o700, exist_ok=True)
        _sync_diagram_skills(agents, skill_sources or {})
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
        try:
            uuid.UUID(spec.session_id)
            base = Path("/private/tmp/ttakkari-antigravity") / spec.session_id
            config = base / ".agents" / "mcp_config.json"
            if base.parent.is_symlink() or base.is_symlink() or config.parent.is_symlink():
                raise ValueError("Antigravity cleanup directory cannot be a symlink")
            _clear_diagram_skills(config.parent)
            config.unlink(missing_ok=True)
        except (OSError, ValueError):
            log.warning("Antigravity session cleanup could not remove its private metadata", exc_info=True)

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
