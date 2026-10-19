from __future__ import annotations

import json
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any

from ttakkari.agents.base import LaunchSpec, Normalized, ParseState, abs_path, truncate
from ttakkari.agents.browser_mcp import BROWSER_TOOLS, SERVER_NAME, server_spec
from ttakkari.config import get_settings
from ttakkari.orchestrator.tool_policy import AUTO_APPROVED_TOOLS

SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
ROLLOUT_TAIL_BYTES = 4 * 1024 * 1024


def read_context(codex_home: Path, thread_id: str) -> tuple[int, int] | None:
    """(마지막 요청 토큰, 모델 컨텍스트 한도). exec --json 은 턴 합계만 주므로 세션 rollout 의 token_count 를 읽는다."""
    files = sorted((codex_home / "sessions").glob(f"*/*/*/rollout-*{thread_id}.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    try:
        with files[-1].open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - ROLLOUT_TAIL_BYTES))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            info = (json.loads(line).get("payload") or {}).get("info") or {}
        except ValueError:
            continue
        last, window = info.get("last_token_usage") or {}, info.get("model_context_window")
        if isinstance(last.get("total_tokens"), int) and isinstance(window, int) and window > 0:
            return last["total_tokens"], window
    return None


class CodexAdapter:
    engine = "codex"

    def __init__(self) -> None:
        self._resources: dict[int, Path] = {}

    def _write_profile(self, spec: LaunchSpec, extra_env: dict[str, str],
                       servers: dict[str, dict[str, Any]]) -> str:
        """MCP 서버 명세(세션 토큰 포함)를 $CODEX_HOME 프로파일 TOML(0600)로 쓰고 프로파일명을 돌려준다.

        SEC2-AGT-01: `-c mcp_servers.*.env=...` argv 전달을 파일 전달로 바꿨다.
        `-c` 값은 TOML 로 파싱되므로 같은 스키마를 파일에 적는다.
        """
        self.cleanup(spec)
        run_id = extra_env.get("TTAKKARI_RUN_ID") or spec.session_id or uuid.uuid4().hex
        name = f"ttakkari-run-{run_id}"
        codex_home = Path(extra_env.get("CODEX_HOME", Path.home() / ".codex"))
        codex_home.mkdir(parents=True, exist_ok=True)
        path = codex_home / f"{name}.config.toml"
        lines = []
        for srv_name, srv in servers.items():
            q = json.dumps(srv_name)
            lines.append(f"[mcp_servers.{q}]")
            lines.append(f"command = {json.dumps(srv['command'])}")
            lines.append(f"args = {json.dumps(list(srv.get('args', [])))}")
            if srv.get("cwd") is not None:
                lines.append(f"cwd = {json.dumps(srv['cwd'])}")
            if srv.get("enabled_tools") is not None:
                lines.append(f"enabled_tools = {json.dumps(list(srv['enabled_tools'] or []))}")
            env = srv.get("env") or {}
            if env:
                lines.append(f"[mcp_servers.{q}.env]")
                for key in sorted(env):
                    lines.append(f"{json.dumps(key)} = {json.dumps(env[key])}")
            for tool in sorted(srv.get("approvals") or ()):
                lines.append(f"[mcp_servers.{q}.tools.{json.dumps(tool)}]")
                lines.append("approval_mode = \"approve\"")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._resources[id(spec)] = path
        return name

    def cleanup(self, spec: LaunchSpec) -> None:
        path = self._resources.pop(id(spec), None)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        s = get_settings()
        sandbox = spec.codex_sandbox if spec.codex_sandbox in SANDBOX_MODES else "workspace-write"
        writable = [str(p) for p in spec.write_roots]
        note = spec.system_note
        servers = dict(spec.mcp_servers)
        browser = None
        if sys.platform == "darwin" and sandbox != "danger-full-access":
            browser = server_spec(spec.outbox)
            if browser:
                servers[SERVER_NAME] = browser
                note += (
                    f"\nFor UI verification, use the configured {SERVER_NAME} MCP browser tools first. "
                    "The browser is headless with an isolated profile; existing login sessions are not shared. "
                    "Take screenshots without a filename to save automatically under this Run's outbox/browser. "
                    "Close the browser when finished. If a browser tool fails to launch, stop browser attempts "
                    "and report the error; do not fall back to a shell browser launch.\n"
                )
            preflight = shlex.join([sys.executable, str(Path(__file__).with_name("browser_preflight.py"))])
            note += (
                "\nBrowser launch on macOS: Before directly launching Chrome/Chromium/Playwright, "
                f"run this read-only preflight INSIDE your command tool: {preflight}\n"
                "If it returns blocked, unknown, fails, or cannot be read, do not launch a local browser. "
                "Do not retry with another Chrome binary, headless mode, or --no-sandbox; these do not "
                "remove the command sandbox's OS IPC restriction and may produce macOS crash dialogs. "
                "Use an already configured, approved browser tool if available; otherwise report browser "
                "verification as blocked and continue independent checks. Do not bypass the sandbox "
                "or change security settings. permissions_allowed only checks prerequisite IPC permissions, "
                "not browser health; on a launch permission error, stop without repeating it. "
                "This is an instruction-level precaution, not an enforced command interceptor.\n"
            )
        opts = [
            "--json", "--skip-git-repo-check",
            # 사용자 전역 config 가 danger-full-access 여도 워크스페이스 설정으로 덮는다. OS 샌드박스가 codex 의 보험이다.
            "-c", f'sandbox_mode="{sandbox}"',
            "-c", 'approval_policy="never"',
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", f"sandbox_workspace_write.writable_roots={json.dumps(writable)}",
            "-c", f"developer_instructions={json.dumps(note)}",
        ]
        profile_servers: dict[str, dict[str, Any]] = {}
        for name, srv in servers.items():
            entry = {"command": srv["command"], "args": list(srv.get("args", [])),
                     "env": dict(srv.get("env", {}))}
            approvals: set[str] = set()
            # 무인 실행의 never 는 승인 요청을 자동 허용하지 않고 거절한다.
            # 사용자 지시를 실행하는 자체 앱 도구를 명시적으로 허용한다. 쓰기의 readOnlyHint는 바꾸지 않는다.
            if name == "ttakkari":
                approvals |= set(AUTO_APPROVED_TOOLS)
            if name == SERVER_NAME and browser is not None:
                entry["cwd"] = srv["cwd"]
                entry["enabled_tools"] = sorted(BROWSER_TOOLS)
                approvals |= set(BROWSER_TOOLS)
            entry["approvals"] = approvals
            profile_servers[name] = entry
        if profile_servers:
            # SEC2-AGT-01: 서버 env(세션 토큰)는 argv 가 아닌 프로파일 파일로 전달한다.
            opts += ["-p", self._write_profile(spec, extra_env, profile_servers)]
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
