"""Parent side of the probe runner: spawn the candidate-side process, collect observations and facts.

The parent never imports candidate code. Judging happens in the evaluator process from the returned
observations plus facts the parent observed on disk itself (which the candidate cannot rewrite after exit).
"""

import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import sandbox
from .channel import collect
from .integrity import signals, violations

RUNNER_PATH = Path(__file__).with_name("hidden_runner.py")
# Pin trusted executable bytes before any candidate starts. Later on-disk rewrites
# cannot change another probe in this evaluator invocation.
_BOUNDARY_PATH = Path(__file__).with_name("integrity.py")
_BOUNDARY_SOURCE = _BOUNDARY_PATH.read_text(encoding="utf-8")
_RUNNER_SOURCE = RUNNER_PATH.read_text(encoding="utf-8")
BOOTSTRAP = (
    "import sys, types\n"
    "_boundary = types.ModuleType('_llme_boundary')\n"
    "sys.modules['_llme_boundary'] = _boundary\n"
    f"exec(compile({_BOUNDARY_SOURCE!r}, {str(_BOUNDARY_PATH)!r}, 'exec'), _boundary.__dict__)\n"
    "_channel = types.ModuleType('_llme_channel')\n"
    "sys.modules['_llme_channel'] = _channel\n"
    f"exec(compile({Path(__file__).with_name('channel.py').read_text(encoding='utf-8')!r}, '<transport>', 'exec'), _channel.__dict__)\n"
    f"exec(compile({_RUNNER_SOURCE!r}, {str(RUNNER_PATH)!r}, 'exec'))\n"
)
OBS_PREFIX = "OBS\t"
OUTPUT_CAP = 200000
IGNORED = {"__pycache__"}


@dataclass
class CheckRun:
    # OBS | integrity | stub_exit0 | multi_obs | bad_obs | crash | timeout
    status: str
    flags: list[str] = field(default_factory=list)
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
    *,
    mode: str = "dev",
) -> CheckRun:
    if mode not in {"dev", "holdout"}:
        raise ValueError("unknown evaluation mode")
    started = time.monotonic()
    isolated = sandbox.availability()
    flags = ["development_answers_public"] if mode == "dev" else []
    if not isolated.available:
        flags.append("sandbox_unavailable")
        if mode == "holdout":
            return CheckRun(
                "isolation_required",
                flags=flags,
                detail=isolated.reason,
                facts={
                    "launched": False,
                    "isolation": {"active": False, "reason": isolated.reason},
                },
            )
    cwd_before = listing(cwd)
    sources = {}
    for rel in cwd_before:
        if not rel.endswith(".py") or rel in {"sitecustomize.py", "usercustomize.py"}:
            continue
        path = cwd / rel
        source = path.read_text(encoding="utf-8")
        flags.extend(flag for flag in signals(source) if flag not in flags)
        bad = violations(source)
        if bad:
            return CheckRun("integrity", flags=flags, detail=f"{rel}: {bad[0]}")
        parts = rel[:-3].split("/")
        package = parts[-1] == "__init__"
        name = ".".join(parts[:-1] if package else parts)
        if (
            name in {"__main__", "integrity", "_llme_boundary"}
            or name.split(".")[0] in sys.stdlib_module_names
        ):
            return CheckRun(
                "integrity", flags=flags, detail=f"reserved module name {name}"
            )
        sources[name] = (source, str(path), package)
    with tempfile.TemporaryDirectory(prefix="llme-run-") as scratch:
        dirs = {}
        for name in dir_names:
            path = Path(scratch) / f"dir-{name}"
            path.mkdir()
            dirs[name] = str(path)
        home = Path(scratch) / "home"
        temporary = Path(scratch) / "tmp"
        home.mkdir()
        temporary.mkdir()
        spec = {
            "check_id": check_id,
            "code": probe,
            "cwd": str(cwd),
            "dirs": dirs,
            "sources": sources,
        }
        env = _minimal_env(str(home))
        env["TMPDIR"] = str(temporary)
        runtime = sandbox.runtime_paths()
        command = [str(runtime.executable), "-I", "-S", "-B", "-c", BOOTSTRAP]
        if isolated.available:
            profile = Path(scratch) / "isolation.sb"
            profile.write_text(
                sandbox.profile_text(runtime, [cwd, home, temporary, *dirs.values()])
            )
            command = sandbox.command(BOOTSTRAP, profile)
        result = collect(command, cwd, env, spec, timeout)
        launch_failed = isolated.available and result.stderr_tail.startswith(
            "sandbox-exec:"
        )
        if launch_failed:
            flags.append("sandbox_launch_failed")
        facts = {
            "cwd_before": cwd_before,
            "cwd_after": listing(cwd),
            "dirs": {name: listing(Path(p)) for name, p in dirs.items()},
            "dir_files": {
                name: {
                    rel: (Path(p) / rel).read_text(encoding="utf-8", errors="replace")
                    for rel in listing(Path(p))
                    if (Path(p) / rel).resolve().is_relative_to(Path(p).resolve())
                    and not (Path(p) / rel).is_symlink()
                    and (Path(p) / rel).stat().st_size < 200000
                }
                for name, p in dirs.items()
            },
            "returncode": result.returncode,
            "launched": True,
            "isolation": {
                "active": isolated.available and not launch_failed,
                "reason": isolated.reason,
                "runtime": str(runtime.executable),
            },
        }
    return CheckRun(
        result.status,
        flags=flags,
        obs=result.obs,
        facts=facts,
        detail=result.detail,
        wall_s=time.monotonic() - started,
        stdout_tail=result.stdout_tail,
        stderr_tail=result.stderr_tail,
    )
