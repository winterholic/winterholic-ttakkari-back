"""macOS read-allowlist isolation. Unavailable development runs are explicitly flagged."""

import functools
import json
import os
import platform
import subprocess
import sys
import sysconfig
import threading
from dataclasses import dataclass
from pathlib import Path

SANDBOX = Path("/usr/bin/sandbox-exec")
_PROBE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Runtime:
    executable: Path
    stdlib: Path
    libraries: Path


def runtime_paths():
    # Resolve through the venv symlink. Never grant its repository prefix.
    exe = Path(sys.executable).resolve()
    stdlib = Path(sysconfig.get_path("stdlib")).resolve()
    libraries = Path(sys.base_prefix).resolve() / "lib"
    for path in (exe, stdlib, libraries):
        if any(
            part in {".venv", "site-packages", "development"} for part in path.parts
        ):
            raise ValueError(f"unsafe runtime inside source/venv: {path}")
    return Runtime(exe, stdlib, libraries)


def _paths(kind, paths):
    return " ".join(f"({kind} {json.dumps(str(Path(p).resolve()))})" for p in paths)


def profile_text(runtime, writable):
    # Metadata alone may reveal existence; file contents and directory listings
    # outside these roots remain denied. Do not allow HOME or the checkout tree.
    roots = [Path(p).resolve() for p in writable]
    if any(str(p) in {"/", "/Users", str(Path.home()), "/private/tmp"} for p in roots):
        raise ValueError("overbroad sandbox root")
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(deny network*)",
            "(deny process-fork)",
            "(allow process-exec " + _paths("literal", [runtime.executable]) + ")",
            "(allow sysctl-read)",
            "(allow file-read-metadata)",
            "(allow file-read* "
            + _paths(
                "subpath",
                [
                    runtime.stdlib,
                    runtime.libraries,
                    "/System/Library",
                    "/usr/lib",
                    "/usr/share",
                    "/var/db/timezone",
                ],
            )
            + " "
            + _paths(
                "literal",
                [
                    runtime.executable,
                    "/dev/null",
                    "/dev/random",
                    "/dev/urandom",
                    "/etc/localtime",
                ],
            )
            + ")",
            # Third-party packages are not required and must not smuggle source paths.
            "(deny file-read* "
            + _paths("subpath", [runtime.stdlib / "site-packages"])
            + ")",
            ("(allow file-read* file-write* " + _paths("subpath", roots) + ")")
            if roots
            else "(deny file-write*)",
        ]
    )


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str


def availability():
    # An operator may preserve a measured environment denial rather than retry it.
    # This only disables isolation; it can never enable it or unlock holdout mode.
    measured = os.environ.get("LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE")
    if measured:
        return Availability(False, measured)
    with _PROBE_LOCK:
        return _probe()


@functools.lru_cache(maxsize=1)
def _probe():
    if platform.system() != "Darwin" or not SANDBOX.is_file():
        return Availability(False, "sandbox-exec unavailable on this platform")
    try:
        runtime = runtime_paths()
        proc = subprocess.run(
            [
                str(SANDBOX),
                "-p",
                profile_text(runtime, []),
                str(runtime.executable),
                "-I",
                "-S",
                "-c",
                'print("llme-sandbox-ready")',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return Availability(False, str(exc))
    return Availability(
        proc.returncode == 0 and proc.stdout.strip() == "llme-sandbox-ready",
        proc.stderr.strip() or f"readiness exit {proc.returncode}",
    )


def command(bootstrap, profile_path):
    runtime = runtime_paths()
    return [
        str(SANDBOX),
        "-f",
        str(profile_path),
        str(runtime.executable),
        "-I",
        "-S",
        "-B",
        "-c",
        bootstrap,
    ]


def protected_sources():
    root = Path(__file__).resolve().parents[2]
    # Exact paths, not a guessed nonexistent evaluator path in another checkout.
    common = subprocess.check_output(
        [
            "git",
            "-C",
            os.environ.get("TTAKKARI_DEPLOY_SOURCE_ROOT", str(root)),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        text=True,
    ).strip()
    original = Path(common).parent
    targets = {
        "evaluator": root / "bench/local_llm_eval/evaluator.py",
        "gold": root / "bench/local_llm_eval/tasks/sgl.py",
        "probe_source": root / "bench/local_llm_eval/hidden_runner.py",
        "original_checkout": original / "pyproject.toml",
        "home_settings": Path.home() / ".zshrc",
    }
    for label, path in targets.items():
        if not path.is_file():
            raise FileNotFoundError(f"{label} deny target absent: {path}")
    return targets


def run_deny_checks(tmp_path):
    """Trusted positive/deny driver. No candidate attack implementation."""
    targets = protected_sources()
    outside = tmp_path / "private"
    outside.mkdir()
    probe = """import json, pathlib, socket, os, datetime, tempfile
request = json.load(__import__('sys').stdin)
result = {'positive':'ok', 'reads':{}}
def fork_once():
    pid = os.fork()
    if pid == 0: os._exit(0)
    os.waitpid(pid, 0)
for name, path in request['reads'].items():
    try:
        pathlib.Path(path).read_bytes()
        result['reads'][name] = 'ALLOWED'
    except OSError as exc: result['reads'][name] = type(exc).__name__
for name, fn in [('network',lambda:socket.socket().connect(('127.0.0.1',9))), ('fork',fork_once), ('outside_write',lambda:open(request['outside'],'w'))]:
    try:
        fn()
        result[name] = 'ALLOWED'
    except OSError as exc: result[name] = type(exc).__name__
with tempfile.TemporaryFile() as f: f.write(b'ok')
print(json.dumps(result))
"""
    profile = tmp_path / "deny.sb"
    writable = tmp_path / "allowed"
    writable.mkdir()
    profile.write_text(profile_text(runtime_paths(), [writable]))
    request = {
        "reads": {k: str(v) for k, v in targets.items()},
        "outside": str(outside / "file"),
    }
    proc = subprocess.run(
        command(probe, profile),
        input=json.dumps(request),
        cwd=writable,
        env={"PATH": "/usr/bin:/bin", "HOME": str(writable), "TMPDIR": str(writable)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def main():
    import tempfile
    from dataclasses import asdict

    status = availability()
    report = {
        "availability": asdict(status),
        "runtime": {key: str(value) for key, value in asdict(runtime_paths()).items()},
        "deny_targets": {key: str(value) for key, value in protected_sources().items()},
    }
    if status.available:
        with tempfile.TemporaryDirectory(prefix="llme-deny-") as tmp:
            report["deny_checks"] = run_deny_checks(Path(tmp))
        checked = report["deny_checks"]
        report["ok"] = (
            checked["positive"] == "ok"
            and all(v == "PermissionError" for v in checked["reads"].values())
            and all(
                checked[k] == "PermissionError"
                for k in ("network", "fork", "outside_write")
            )
        )
    else:
        report["ok"] = False
        report["unverified"] = ["OS read, write, network and fork denial"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
