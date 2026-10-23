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

from .bundle_paths import SEALED_ROOT

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
    # The sealed holdout bundle must never become readable: no root may be, contain or sit inside it.
    sealed = SEALED_ROOT.resolve()
    if any(p == sealed or sealed in p.parents or p in sealed.parents for p in roots):
        raise ValueError("sandbox root overlaps the sealed holdout bundle")
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(deny network*)",
            "(deny process-fork)",
            "(allow process-exec " + _paths("literal", [runtime.executable]) + ")",
            "(allow sysctl-read)",
            "(allow file-read-metadata)",
            # Measured on macOS 27 (Darwin 27.0.0): without data-read of the root
            # directory itself even /bin/echo aborts at process start (exit 134).
            # The directory entries of "/" become listable; nothing below it is.
            # The exact caller inside dyld/libSystem was not traced (unverified).
            '(allow file-read-data (literal "/"))',
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
    # True when isolation failed for a reason other than an environment denial
    # (profile/runtime defect, abnormal readiness exit). Gates must not skip these.
    defect: bool = False


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
        return Availability(False, str(exc), defect=True)
    ready = proc.returncode == 0 and proc.stdout.strip() == "llme-sandbox-ready"
    reason = proc.stderr.strip() or f"readiness exit {proc.returncode}"
    # Applying a profile from inside another sandbox fails in sandbox_apply
    # (exit 71, EPERM). Anything else that is not ready is our own defect.
    denied = proc.returncode == 71 and "sandbox_apply" in proc.stderr
    return Availability(ready, reason, defect=not ready and not denied)


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
        "system_config": Path("/etc/hosts"),
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
for name, fn in [('dev_fd_path',lambda:open('/dev/fd/0','rb')), ('network',lambda:socket.socket().connect(('127.0.0.1',9))), ('fork',fork_once), ('outside_write',lambda:open(request['outside'],'w')), ('home_listing',lambda:os.listdir(request['home'])), ('tmp_listing',lambda:os.listdir('/private/tmp'))]:
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
    # A sealed-like directory made here (owner-only, outside the allowed root) is always checked, so
    # the deny holds on hosts without the real bundle. The real manifest is added when it exists.
    sealed_like = tmp_path / "sealed-like"
    sealed_like.mkdir(mode=0o700)
    (sealed_like / "canary.txt").write_text("canary", encoding="utf-8")
    reads = {k: str(v) for k, v in targets.items()}
    reads["sealed_like"] = str(sealed_like / "canary.txt")
    real_manifest = SEALED_ROOT / "manifest.sealed.json"
    if real_manifest.is_file():
        reads["sealed_bundle"] = str(real_manifest)
    for gold in sorted(SEALED_ROOT.glob("tasks/*/gold.py"))[:1]:
        reads["sealed_bundle_gold"] = str(gold)
    request = {
        "reads": reads,
        "outside": str(outside / "file"),
        "home": str(Path.home()),
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
                for k in (
                    "dev_fd_path",
                    "network",
                    "fork",
                    "outside_write",
                    "home_listing",
                    "tmp_listing",
                )
            )
        )
    else:
        report["ok"] = False
        report["unverified"] = ["OS read, write, network and fork denial"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 0: verified. 2: unavailable because the environment denies sandbox_apply or
    # an operator recorded a measured denial (not a failure). 1: our defect, or a
    # deny check that did not deny; callers must not treat this as "unverified".
    if report["ok"]:
        return 0
    return 1 if status.available or status.defect else 2


if __name__ == "__main__":
    raise SystemExit(main())
