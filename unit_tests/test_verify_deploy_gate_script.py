from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "verify_deploy_gate.sh"
SLOTS = ROOT / "scripts" / "pytest_slots.py"
ZERO = "0" * 40


def fake_uv(tmp_path: Path, *, pytest_exit: int, pytest_output: str = "1 passed") -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "pytest-calls.log"
    uv = bindir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "mkdir -p .venv/bin\n"
        "cat > .venv/bin/python <<'PY'\n"
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "if '-m' in sys.argv and 'pytest' in sys.argv:\n"
        "    record = {'args': sys.argv[1:], 'migration_test_present': os.path.isfile('tests/test_migration_single_head.py')}\n"
        "    with open(os.environ['GATE_CALL_LOG'], 'a') as f: f.write(json.dumps(record) + '\\n')\n"
        "    print(os.environ['GATE_PYTEST_OUTPUT'])\n"
        "    raise SystemExit(int(os.environ['GATE_PYTEST_EXIT']))\n"
        "raise SystemExit(0)\n"
        "PY\n"
        "chmod +x .venv/bin/python\n"
        "cat > .venv/bin/ruff <<'RUFF'\n#!/bin/sh\nexit 0\nRUFF\n"
        "chmod +x .venv/bin/ruff\n"
    )
    uv.chmod(0o755)
    return bindir, calls


def run_gate(tmp_path: Path, *args: str, pytest_exit: int, pytest_output: str = "1 passed",
             stdin: str = "") -> subprocess.CompletedProcess[str]:
    bindir, calls = fake_uv(tmp_path, pytest_exit=pytest_exit, pytest_output=pytest_output)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GATE_CALL_LOG": str(calls),
           "GATE_PYTEST_EXIT": str(pytest_exit), "GATE_PYTEST_OUTPUT": pytest_output,
           "TTK_PYTEST_SLOT_DIR": str(tmp_path / "slot-locks")}
    return subprocess.run([str(GATE), *args], input=stdin, text=True, capture_output=True, env=env, timeout=30, check=False)


def test_full_mode_rejects_zero_collected_tests(tmp_path):
    result = run_gate(tmp_path, "--full", pytest_exit=5, pytest_output="no tests ran")

    assert result.returncode != 0
    assert "전체 pytest" in result.stdout
    assert "사전 검증 실패" in result.stdout


def test_pre_push_checks_every_eligible_ref(tmp_path):
    main_sha = "25b5f9806656067ceb594d6d3217b37fba28d795"
    master_sha = "184b5c5aebb596f196681feaed055f8ce0370b95"
    stdin = (
        f"refs/heads/main {main_sha} refs/heads/main {ZERO}\n"
        f"refs/heads/master {master_sha} refs/heads/master {ZERO}\n"
    )
    result = run_gate(tmp_path, "--pre-push", "origin", "unused", pytest_exit=0, stdin=stdin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("대상 커밋:") == 2


def test_pre_push_rejects_when_any_eligible_ref_fails(tmp_path):
    main_sha = "25b5f9806656067ceb594d6d3217b37fba28d795"
    master_sha = "184b5c5aebb596f196681feaed055f8ce0370b95"
    stdin = (
        f"refs/heads/main {main_sha} refs/heads/main {ZERO}\n"
        f"refs/heads/master {master_sha} refs/heads/master {ZERO}\n"
    )
    result = run_gate(tmp_path, "--pre-push", "origin", "unused", pytest_exit=1, stdin=stdin)

    assert result.returncode != 0
    assert result.stdout.count("검사 ref refs/heads/") == 2


def test_installed_hook_invokes_gate_automatically_and_preserves_existing_hook(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    git_dir = repo / ".git"
    scripts = repo / "scripts"
    scripts.mkdir()
    marker = tmp_path / "hook-invoked"
    gate = scripts / "verify_deploy_gate.sh"
    gate.write_text(f"#!/bin/sh\nprintf 'invoked:%s\\n' \"$1\" >> '{marker}'\n")
    gate.chmod(0o755)
    env = {**os.environ, "GIT_DIR": str(git_dir)}

    installed = subprocess.run([str(GATE), "--install-hook"], capture_output=True, text=True, env=env, check=False)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    hook = git_dir / "hooks" / "pre-push"
    invoked = subprocess.run([str(hook), "origin", "unused"], cwd=repo, input="", text=True,
                             capture_output=True, env=env, check=False)
    assert invoked.returncode == 0, invoked.stdout + invoked.stderr
    assert marker.read_text().strip() == "invoked:--pre-push"

    hook.write_text("#!/bin/sh\necho user-hook\n")
    hook.chmod(0o755)
    rejected = subprocess.run([str(GATE), "--install-hook"], capture_output=True, text=True, env=env, check=False)
    assert rejected.returncode != 0
    assert hook.read_text() == "#!/bin/sh\necho user-hook\n"


def test_changed_test_selection_runs_inside_shared_slot(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    copied_gate = scripts / "verify_deploy_gate.sh"
    copied_gate.write_bytes(GATE.read_bytes())
    copied_gate.chmod(0o755)
    (scripts / "pytest_slots.py").write_bytes(SLOTS.read_bytes())
    (repo / "tests").mkdir()
    (repo / "tests" / "test_contract_marker.py").write_text("# committed contract suite\n")
    (repo / "unit_tests").mkdir()
    (repo / "unit_tests" / "test_base.py").write_text("# base test\n")
    (repo / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "unit_tests" / "test_changed.py").write_text("# changed test\n")
    git("add", "-A")
    git("commit", "-qm", "add changed test")
    candidate = git("rev-parse", "HEAD")

    fake_root = tmp_path / "fake"
    fake_root.mkdir()
    bindir, calls = fake_uv(fake_root, pytest_exit=0)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GATE_CALL_LOG": str(calls),
           "GATE_PYTEST_EXIT": "0", "GATE_PYTEST_OUTPUT": "1 passed",
           "TTK_PYTEST_SLOT_DIR": str(tmp_path / "slot-locks")}
    result = subprocess.run([str(scripts / "verify_deploy_gate.sh"), "--commit", candidate, "--base", base],
                            cwd=repo, text=True, capture_output=True, env=env, timeout=30, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "slot-locks" / "slot-0.lock").is_file()
    assert "체커: revision" in result.stdout and "SHA-256" in result.stdout
    calls_seen = [json.loads(line) for line in calls.read_text().splitlines()]
    changed_calls = [call for call in calls_seen if "unit_tests/test_changed.py" in call["args"]]
    assert len(changed_calls) == 1
    assert all(not call["migration_test_present"] for call in calls_seen)



def preflight_env(tmp_path: Path, *, shmmni: int, segments: int) -> dict[str, str]:
    bindir = tmp_path / "pf-bin"
    bindir.mkdir()
    pgbin = tmp_path / "pg-bin"
    pgbin.mkdir()
    for path, body in {
        bindir / "uv": "#!/bin/sh\nexit 0\n",
        bindir / "sysctl": f"#!/bin/sh\necho {shmmni}\n",
        bindir / "ipcs": "#!/bin/sh\necho 'Shared Memory:'\n" + "".join(
            f"echo 'm {i} 0x0 --rw------- u g'\n" for i in range(segments)),
        pgbin / "initdb": "#!/bin/sh\nexit 0\n",
        pgbin / "pg_ctl": "#!/bin/sh\nexit 0\n",
    }.items():
        path.write_text(body)
        path.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "TTAKKARI_TEST_PG_BIN": str(pgbin)}


def run_preflight(tmp_path: Path, **kw: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(GATE), "--preflight"], text=True, capture_output=True,
                          env=preflight_env(tmp_path, **kw), timeout=30, check=False)


def test_preflight_fails_when_sysv_shared_memory_is_exhausted(tmp_path):
    if os.uname().sysname != "Darwin":
        return
    result = run_preflight(tmp_path, shmmni=32, segments=32)

    assert result.returncode == 1
    assert "소진: 사용 32 / 한도 32" in result.stdout
    assert "preflight 실패" in result.stdout


def test_preflight_passes_with_headroom_and_creates_nothing(tmp_path):
    if os.uname().sysname != "Darwin":
        return
    before = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("ttk-gate-*"))
    result = run_preflight(tmp_path, shmmni=32, segments=10)

    assert result.returncode == 0, result.stdout
    assert "사용 10 / 한도 32" in result.stdout
    assert set(Path(os.environ.get("TMPDIR", "/tmp")).glob("ttk-gate-*")) == before


@pytest.mark.parametrize(("unavailable", "message", "body"), [
    ("sysctl", "FAIL SysV 공유메모리 한도를 확인할 수 없음", "#!/bin/sh\nexit 1\n"),
    ("ipcs", "FAIL SysV 공유메모리 사용량을 확인할 수 없음", "#!/bin/sh\nexit 1\n"),
    ("ipcs", "FAIL SysV 공유메모리 사용량을 확인할 수 없음",
     "#!/bin/sh\necho 'ipcs: sysctlbyname: Operation not permitted' >&2\necho 'Shared Memory:'\nexit 0\n"),
])
def test_preflight_fails_closed_when_sysv_state_is_unavailable(tmp_path, unavailable, message, body):
    if os.uname().sysname != "Darwin":
        return
    env = preflight_env(tmp_path, shmmni=32, segments=1)
    command = Path(env["PATH"].split(":", 1)[0]) / unavailable
    command.write_text(body)
    result = subprocess.run([str(GATE), "--preflight"], text=True, capture_output=True,
                            env=env, timeout=30, check=False)

    assert result.returncode == 1
    assert message in result.stdout
    assert "preflight 실패" in result.stdout
