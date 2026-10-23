"""Parent side of the hidden-check runner: spawn, handshake validation, process-group kill."""

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

RUNNER_PATH = Path(__file__).with_name("hidden_runner.py")
HANDSHAKE = "HIDDEN_DONE"
OUTPUT_CAP = 20000


@dataclass
class CheckRun:
    status: str  # OK | FAIL | EXIT | TAMPER | stub_exit0 | forged | timeout | crash
    detail: str = ""
    result: dict = field(default_factory=dict)
    wall_s: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""


def _minimal_env(home: str) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "HOME": home}


def run_check(cwd: Path, check_id: str, code: str, timeout: float = 20.0) -> CheckRun:
    nonce = secrets.token_hex(8)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="llme-run-") as scratch:
        spec_path = Path(scratch) / "spec.json"
        spec_path.write_text(
            json.dumps(
                {"nonce": nonce, "check_id": check_id, "code": code, "cwd": str(cwd)}
            )
        )
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(RUNNER_PATH), str(spec_path)],
            cwd=cwd,
            env=_minimal_env(scratch),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            timed_out = True
    wall = time.monotonic() - started
    out, err = out[-OUTPUT_CAP:], err[-OUTPUT_CAP:]
    base = {"wall_s": wall, "stdout_tail": out[-400:], "stderr_tail": err[-400:]}
    if timed_out:
        return CheckRun("timeout", f"check exceeded {timeout}s", **base)
    valid, forged = None, False
    for line in out.splitlines():
        if not line.startswith(HANDSHAKE + "\t"):
            continue
        parts = line.split("\t", 4)
        if len(parts) == 5 and parts[1] == nonce and parts[2] == check_id:
            valid = parts
        else:
            forged = True
    if forged:
        return CheckRun("forged", "handshake line with wrong nonce/check id", **base)
    if valid is None:
        if proc.returncode == 0:
            return CheckRun("stub_exit0", "exit 0 without handshake", **base)
        return CheckRun("crash", f"exit {proc.returncode} without handshake", **base)
    payload = json.loads(valid[4])
    return CheckRun(
        valid[3], payload.get("detail", ""), payload.get("result", {}), **base
    )
