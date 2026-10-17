"""Adapter for the fixed local qwen-worker protocol.

It intentionally does not receive Ttakkari MCP configuration, provider profiles,
or credentials.  The harness owns its sandbox and local model lifecycle.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ttakkari.agents.base import LaunchSpec, Normalized, ParseState

MODEL = "Qwen3.8-Flash-Next-Q2_K"
READY_CONTRACT = "qwen-local-readiness-v1"


class QwenLocalAdapter:
    engine = "qwen-local"

    def __init__(self, *, worker_path: Path, enabled: bool = False, turns: int = 6):
        self.worker_path = Path(worker_path).expanduser()
        self.enabled = enabled
        if not 1 <= turns <= 60:
            raise ValueError("turns must be between 1 and 60")
        self.turns = turns
        self._task_files: dict[str, Path] = {}

    def readiness(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        if not self.worker_path.is_file() or not os.access(self.worker_path, os.X_OK):
            return False, "worker_executable_missing"
        # doctor is a fixed product entrypoint. It must explicitly attest the
        # Seatbelt, host monitor, and fixture contract before any model request.
        import subprocess

        try:
            proc = subprocess.run(
                [str(self.worker_path), "doctor"], cwd=str(self.worker_path.parent.parent),
                env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home()), "LANG": "en_US.UTF-8"},
                stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=8, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, "readiness_check_failed"
        try:
            report = json.loads(proc.stdout)
        except (TypeError, json.JSONDecodeError):
            return False, "readiness_report_invalid"
        checks = report.get("checks") if isinstance(report, dict) else None
        if (not isinstance(report, dict) or proc.returncode != 0 or report.get("contract") != READY_CONTRACT or
                not isinstance(checks, dict) or any(checks.get(k) is not True for k in ("seatbelt", "hostmon", "fixture"))):
            return False, "readiness_contract_unverified"
        return True, "ready"

    def build_command(self, spec: LaunchSpec, extra_env: dict[str, str]) -> list[str]:
        if not self.enabled:
            raise RuntimeError("qwen-local is disabled")
        spec.outbox.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=".qwen-task-", suffix=".txt", dir=spec.outbox)
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as task_file:
                task_file.write(spec.system_note + "\n\n" + spec.prompt)
            self._task_files[str(spec.session_id or spec.outbox)] = path
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        argv = [str(self.worker_path), "run", "--workspace", str(spec.cwd), "--task-file", str(path),
                "--turns", str(self.turns)]
        if spec.effort in {"low", "medium", "high"}:
            argv.extend(["--effort", spec.effort])
        if spec.engine_session_id:
            argv.extend(["--resume", spec.engine_session_id])
        return argv

    def cleanup(self, spec: LaunchSpec) -> None:
        key = str(spec.session_id or spec.outbox)
        path = self._task_files.pop(key, None)
        if path is not None:
            path.unlink(missing_ok=True)

    @staticmethod
    def child_env() -> dict[str, str]:
        allowed = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "USER", "LOGNAME"}
        return {key: value for key, value in os.environ.items() if key in allowed or key.startswith("LC_")}

    def parse_line(self, obj: dict[str, Any], state: ParseState, cwd: Path) -> list[Normalized]:
        if state.protocol_final_seen:
            state.is_error = True
            state.error = "qwen-worker wrote more than one stdout JSON result"
            return [Normalized("log", {"stream": "system", "text": state.error})]
        state.protocol_final_seen = True
        if obj.get("schema") != 1:
            state.is_error = True
            state.error = "qwen-worker result schema is missing or unsupported"
            return [Normalized("log", {"stream": "system", "text": state.error})]
        status, exit_code = obj.get("status"), obj.get("exit_code")
        text = obj.get("result_text")
        if not isinstance(status, str) or not isinstance(exit_code, int) or isinstance(exit_code, bool):
            state.is_error = True
            state.error = "qwen-worker result is missing status or process exit code"
            return [Normalized("log", {"stream": "system", "text": state.error})]
        state.engine_session_id = obj.get("session_id") if isinstance(obj.get("session_id"), str) else None
        if isinstance(text, str) and text:
            state.result_text = text
        if status != "success" or exit_code != 0 or not state.result_text:
            detail = obj.get("error")
            message = detail.get("message") if isinstance(detail, dict) else None
            kind = detail.get("kind") if isinstance(detail, dict) else None
            state.is_error = True
            category = f" [{kind}]" if isinstance(kind, str) else ""
            state.error = f"qwen-local {status}{category}: {message or 'missing final result or nonzero exit'}"
        files = obj.get("files")
        if isinstance(files, dict):
            for key in ("added", "modified"):
                rows = files.get(key)
                if isinstance(rows, list):
                    for raw in rows:
                        if isinstance(raw, str):
                            p = Path(raw)
                            state.touched_files.add(str(p if p.is_absolute() else cwd / p))
        payload = {"text": state.result_text or "", "engine": self.engine, "model": MODEL,
                   "status": status, "exit_code": exit_code}
        metrics = obj.get("metrics")
        if isinstance(metrics, dict):
            payload["local_resources"] = metrics.get("host_peaks")
            payload["metrics"] = metrics
        return [Normalized("message", payload)] if state.result_text else [Normalized("log", {"stream": "system", "text": state.error or "qwen-worker returned no final result"})]

    def parse_stdout_line(self, text: str, state: ParseState, cwd: Path) -> list[Normalized]:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            state.is_error = True
            state.error = "qwen-worker stdout must contain one JSON result"
            return [Normalized("log", {"stream": "stdout", "text": text[:4000]})]
        if not isinstance(obj, dict):
            state.is_error = True
            state.error = "qwen-worker stdout JSON result must be an object"
            return [Normalized("log", {"stream": "stdout", "text": text[:4000]})]
        return self.parse_line(obj, state, cwd)

    def finalize_process(self, state: ParseState, *, return_code: int | None, stderr: str) -> None:
        if state.result_text is None and not state.is_error:
            state.is_error = True
            state.error = "qwen-worker exited without one final JSON result"
        if return_code not in (0, None):
            state.is_error = True
            state.error = state.error or f"qwen-worker process exited {return_code}"
