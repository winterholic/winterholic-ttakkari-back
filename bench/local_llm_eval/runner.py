"""Parent side of the probe runner: spawn the candidate-side process, collect observations and facts.

The parent never imports candidate code. Judging happens in the evaluator process from the returned
observations plus facts the parent observed on disk itself (which the candidate cannot rewrite after exit).
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

RUNNER_PATH = Path(__file__).with_name("hidden_runner.py")
OBS_PREFIX = "OBS\t"
OUTPUT_CAP = 200000
IGNORED = {"__pycache__"}


@dataclass
class CheckRun:
    # OBS | stub_exit0 | multi_obs | bad_obs | crash | timeout
    status: str
    obs: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    detail: str = ""
    wall_s: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""


def _minimal_env(home: str) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "HOME": home}


def listing(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED)
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(root)
            if rel.suffix != ".pyc":
                out.append(rel.as_posix())
    return sorted(out)


def run_check(
    cwd: Path,
    check_id: str,
    probe: str,
    timeout: float = 20.0,
    dir_names: tuple[str, ...] = (),
) -> CheckRun:
    started = time.monotonic()
    cwd_before = listing(cwd)
    with tempfile.TemporaryDirectory(prefix="llme-run-") as scratch:
        dirs = {}
        for name in dir_names:
            path = Path(scratch) / f"dir-{name}"
            path.mkdir()
            dirs[name] = str(path)
        spec_path = Path(scratch) / "spec.json"
        spec_path.write_text(
            json.dumps(
                {"check_id": check_id, "code": probe, "cwd": str(cwd), "dirs": dirs}
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
        facts = {
            "cwd_before": cwd_before,
            "cwd_after": listing(cwd),
            "dirs": {name: listing(Path(p)) for name, p in dirs.items()},
            "dir_files": {
                name: {
                    rel: (Path(p) / rel).read_text(encoding="utf-8", errors="replace")
                    for rel in listing(Path(p))
                    if (Path(p) / rel).stat().st_size < 200000
                }
                for name, p in dirs.items()
            },
            "returncode": proc.returncode,
        }
    out, err = out[-OUTPUT_CAP:], err[-OUTPUT_CAP:]
    base = {
        "facts": facts,
        "wall_s": time.monotonic() - started,
        "stdout_tail": out[-400:],
        "stderr_tail": err[-400:],
    }
    if timed_out:
        return CheckRun("timeout", detail=f"check exceeded {timeout}s", **base)
    lines = [line for line in out.splitlines() if line.startswith(OBS_PREFIX)]
    if not lines:
        if proc.returncode == 0:
            return CheckRun(
                "stub_exit0", detail="exit 0 without an observation line", **base
            )
        return CheckRun(
            "crash", detail=f"exit {proc.returncode} without observations", **base
        )
    if len(lines) > 1:
        return CheckRun("multi_obs", detail=f"{len(lines)} observation lines", **base)
    try:
        obs = json.loads(lines[0][len(OBS_PREFIX) :])
    except ValueError:
        return CheckRun("bad_obs", detail="observation line is not JSON", **base)
    if not isinstance(obs, dict):
        return CheckRun("bad_obs", detail="observation is not an object", **base)
    return CheckRun("OBS", obs=obs, **base)
