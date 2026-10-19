"""엔진 Seatbelt 프로파일. DB 없이 돈다(`pytest --noconftest tests/test_seatbelt.py`)."""
from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ttakkari.security import seatbelt
from ttakkari.security.seatbelt import (
    SeatbeltPolicy,
    maybe_wrap,
    policy_for,
    render_profile,
)


def _policy(tmp_path: Path, **kw) -> SeatbeltPolicy:
    ttk = tmp_path / ".ttakkari"
    return policy_for(home=tmp_path, ttakkari_home=ttk, outbox=ttk / "data/outbox/s1",
                      write_roots=[tmp_path / "ws"],
                      workspace=tmp_path / "ws",
                      env={"CLAUDE_CONFIG_DIR": str(ttk / "accounts/a"),
                           "TTAKKARI_GUARD_LOG": str(ttk / "data/guard.log")}, engine="claude", **kw)


def test_profile_blocks_db_ports_and_sockets(tmp_path):
    text = render_profile(_policy(tmp_path))
    for port in seatbelt.DB_PORTS:
        assert f'(remote ip "*:{port}")' in text
    assert r"\.s\.PGSQL\." in text
    # 워크스페이스 안 임시 테스트 클러스터 소켓만 다시 연다.
    assert f'(subpath "{os.path.realpath(tmp_path / "ws/.ttakkari-db-sockets")}")' in text


def test_secret_denies_come_after_every_allow(tmp_path):
    text = render_profile(_policy(tmp_path))
    secret_deny = text.index(";; 비밀 경로")
    ttk_allow = text.index("(allow file-read-data file-write*\n")
    assert ttk_allow < secret_deny
    # 비밀 거부 뒤에 오는 허용은 .env 예시 파일 하나뿐이다.
    tail = text[secret_deny:]
    assert tail.count("(allow ") == 1 and "example" in tail


def test_only_paths_inside_ttakkari_home_are_reopened(tmp_path):
    # write_roots 가 홈 전체여도 ~/.ssh 를 다시 열면 안 된다.
    policy = _policy(tmp_path)
    policy.allowed.append(tmp_path)
    text = render_profile(policy)
    real = os.path.realpath(tmp_path)
    assert f'(subpath "{real}")\n' not in text.split(";; 비밀 경로")[0].split(";; ttakkari 홈")[1]
    assert f'(subpath "{real}/.ttakkari/accounts/a")' in text


def test_keychain_deny_is_opt_in(tmp_path):
    assert "Library/Keychains" not in render_profile(_policy(tmp_path))
    assert "Library/Keychains" in render_profile(_policy(tmp_path, deny_keychain=True))


@pytest.mark.parametrize("bad", ['/tmp/a"b', "/tmp/a\\b", "relative/path", "/tmp/a\nb"])
def test_rejects_paths_that_could_break_sbpl(tmp_path, bad):
    policy = _policy(tmp_path)
    policy.socket_roots = [Path(bad)]
    with pytest.raises(ValueError):
        render_profile(policy)


def _wrap(engine, enabled, tmp_path):
    return maybe_wrap(engine, ["engine", "-p", "x"], enabled_engines=enabled, home=tmp_path,
                      ttakkari_home=tmp_path / ".ttakkari", outbox=tmp_path / ".ttakkari/data/outbox/s",
                      write_roots=[tmp_path / "ws"], env={})


def test_flag_off_leaves_command_untouched(tmp_path):
    assert _wrap("claude", [], tmp_path) == ["engine", "-p", "x"]


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec 는 macOS 전용")
def test_flag_on_wraps_with_sandbox_exec(tmp_path):
    cmd = _wrap("claude", ["claude"], tmp_path)
    assert cmd[:2] == [seatbelt.SANDBOX_EXEC, "-p"] and cmd[3:] == ["engine", "-p", "x"]


@pytest.mark.parametrize("engine", ["codex", "antigravity", "qwen-local", "muse"])
def test_engines_with_own_seatbelt_fail_closed(tmp_path, engine):
    # 중첩 Seatbelt 는 안쪽 sandbox_apply 가 실패한다. 조용히 샌드박스 없이 띄우지 않는다.
    with pytest.raises(RuntimeError):
        _wrap(engine, [engine], tmp_path)


PROBE = r"""
import json, os, socket, sys
def op(f):
    try:
        f(); return "ok"
    except OSError as e:
        return "denied:" + str(e.errno)
def unix(p):
    s = socket.socket(socket.AF_UNIX); s.settimeout(2)
    try: s.connect(p)
    finally: s.close()
def tcp(port):
    s = socket.socket(); s.settimeout(2)
    try: s.connect(("127.0.0.1", port))
    finally: s.close()
def read(p):
    fd = os.open(p, os.O_RDONLY); os.close(fd)
def write(p):
    with open(p, "w") as f: f.write("x")
h = sys.argv[1]
print(" ".join([
    op(lambda: read(h + "/.ssh/id_ed25519")),
    op(lambda: read(h + "/.ttakkari/config.env")),
    op(lambda: read(h + "/.ttakkari/accounts/b/auth.json")),
    op(lambda: read(h + "/.ttakkari/accounts/a/auth.json")),
    op(lambda: read(h + "/ws/.env")),
    op(lambda: read(h + "/ws/.env.example")),
    op(lambda: write(h + "/ws/edit.txt")),
    op(lambda: write(h + "/.ttakkari/data/outbox/s1/out.md")),
    op(lambda: unix(h + "/outside/.s.PGSQL.5432")),
    op(lambda: unix(h + "/ws/.ttakkari-db-sockets/.s.PGSQL.5432")),
    op(lambda: tcp(int(sys.argv[2]))),
]))
print(json.dumps({p: op(lambda p=p: read(h + "/" + p)) for p in json.loads(sys.argv[3])}))
runtime_file = sys.argv[4] + "/.venv/pyvenv.cfg"
print(json.dumps({"read": op(lambda: read(runtime_file)), "write": op(lambda: write(runtime_file))}))
"""

CREDENTIAL_DECOYS = [
    ".gemini/oauth_creds.json", ".gemini/config/new-token", ".codex/auth.json", ".codex/auth.json.bak",
    ".config/gh/hosts.yml", ".aws/credentials", ".kube/config", ".docker/config.json",
    ".docker/contexts/tls/key.pem", ".netrc", ".git-credentials", ".npmrc", ".pypirc",
    ".config/gcloud/application_default_credentials.json", ".azure/accessTokens.json", ".gnupg/private-keys-v1.d/key",
    ".ttakkari/config.env.bak", ".ttakkari/initial-password.txt", ".ttakkari/vapid.json",
    ".ttakkari/accounts/b/.env.example", ".gemini/.env.example",
    ".ttakkari/deploy/releases/" + "b" * 40 + "/.venv/pyvenv.cfg",
]


@pytest.mark.skipif(sys.platform != "darwin" or not Path(seatbelt.SANDBOX_EXEC).exists(),
                    reason="sandbox-exec 실측은 macOS 전용")
def test_kernel_enforces_profile():
    if reason := os.environ.get("TTAKKARI_TEST_SEATBELT_UNAVAILABLE"):
        pytest.skip(reason)
    # pytest tmp_path 는 AF_UNIX 경로 한도(104바이트)를 넘기 쉬워 /tmp 아래 짧은 경로를 쓴다.
    home = Path(os.path.realpath(tempfile.mkdtemp(prefix="sb", dir="/tmp")))
    try:
        _enforce(home)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _enforce(home: Path):
    ttk = home / ".ttakkari"
    for rel in [".ssh/id_ed25519", ".ttakkari/config.env", ".ttakkari/accounts/b/auth.json",
                ".ttakkari/accounts/a/auth.json", "ws/.env", "ws/.env.example", *CREDENTIAL_DECOYS]:
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        try:
            (home / rel).write_text("decoy")
        except PermissionError:
            pytest.skip("이미 Seatbelt 안이라 비밀 경로 미끼를 만들 수 없다")
    (ttk / "data/outbox/s1").mkdir(parents=True)
    runtime_root = ttk / "deploy/releases" / ("a" * 40)
    (runtime_root / ".venv").mkdir(parents=True)
    (runtime_root / ".venv/pyvenv.cfg").write_text("decoy")
    (home / "outside").mkdir()
    servers = []
    (home / "ws/.ttakkari-db-sockets").mkdir()
    for path in [home / "outside/.s.PGSQL.5432", home / "ws/.ttakkari-db-sockets/.s.PGSQL.5432"]:
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(path))
        s.listen(1)
        servers.append(s)
    tcp_server = socket.socket()
    tcp_server.bind(("127.0.0.1", 0))
    tcp_server.listen(1)
    servers.append(tcp_server)
    # 실제 DB 포트를 열 수 없으니 임시 포트를 DB 포트 목록에 넣은 프로파일로 같은 규칙을 검사한다.
    port = tcp_server.getsockname()[1]
    policy = policy_for(home=home, ttakkari_home=ttk, outbox=ttk / "data/outbox/s1",
                        write_roots=[home, Path("/tmp"), home / "ws", ttk / "accounts", ttk / "accounts/b", ttk / "data", ttk / "config.env"],
                        workspace=home / "ws",
                        env={"CLAUDE_CONFIG_DIR": str(ttk / "accounts/a"), "CODEX_HOME": str(ttk / "accounts/b"),
                             "XDG_CONFIG_HOME": str(ttk / "accounts/b")}, engine="claude")
    policy.runtime_root = runtime_root
    profile = render_profile(policy).replace('(remote ip "*:5432")', f'(remote ip "*:{port}")')
    try:
        proc = subprocess.run([seatbelt.SANDBOX_EXEC, "-p", profile, sys.executable, "-c", PROBE, str(home), str(port),
                               json.dumps(CREDENTIAL_DECOYS), str(runtime_root)],
                              capture_output=True, text=True, timeout=30, check=False)
    finally:
        for s in servers:
            s.close()
    if "sandbox_apply" in proc.stderr:
        pytest.skip(f"이미 샌드박스 안이라 실측 불가: {proc.stderr.strip()}")
    assert proc.returncode == 0, proc.stderr
    access, credential_access, runtime_access = proc.stdout.splitlines()
    assert access.replace(f"denied:{errno.EPERM}", "denied").split() == [
        "denied", "denied", "denied", "ok", "denied", "ok", "ok", "ok", "denied", "ok", "denied",
    ]
    assert json.loads(credential_access) == dict.fromkeys(CREDENTIAL_DECOYS, f"denied:{errno.EPERM}")
    assert json.loads(runtime_access) == {"read": "ok", "write": f"denied:{errno.EPERM}"}


def test_default_claude_account_keeps_its_own_credentials(tmp_path):
    common = {"home": tmp_path, "ttakkari_home": tmp_path / ".ttakkari",
              "outbox": tmp_path / ".ttakkari/data/outbox/s1",
              "write_roots": [tmp_path / "ws"]}
    creds = seatbelt.CLAUDE_DEFAULT_CREDENTIALS
    assert creds not in render_profile(policy_for(env={}, engine="claude", **common))
    assert creds in render_profile(policy_for(env={"CLAUDE_CONFIG_DIR": "/x/acc"}, engine="claude", **common))
    assert creds in render_profile(policy_for(env={}, engine="muse", **common))


@pytest.mark.parametrize("rel", [
    ".gemini", ".codex", ".config/gh", ".aws", ".kube", ".docker",
    ".config/gcloud", ".azure", ".gnupg",
])
def test_entire_credential_store_is_denied(tmp_path, rel):
    text = render_profile(_policy(tmp_path))
    denies = text.split(";; 비밀 경로")[1]
    assert f'(subpath "{os.path.realpath(tmp_path / rel)}")' in denies


@pytest.mark.parametrize("rel", [".netrc", ".git-credentials", ".npmrc", ".pypirc"])
def test_standalone_credential_file_is_denied(tmp_path, rel):
    assert f'(literal "{os.path.realpath(tmp_path / rel)}")' in render_profile(_policy(tmp_path))


def test_only_claude_account_env_can_reopen_an_account(tmp_path):
    ttk = tmp_path / ".ttakkari"
    policy = policy_for(home=tmp_path, ttakkari_home=ttk, outbox=ttk / "data/outbox/s1",
                        write_roots=[ttk / "accounts", ttk / "accounts/b", ttk / "data", ttk / "backups"],
                        engine="claude", env={"CLAUDE_CONFIG_DIR": str(ttk / "accounts/a"),
                                              "CODEX_HOME": str(ttk / "accounts/b"),
                                              "XDG_CONFIG_HOME": str(ttk / "accounts/c"),
                                              "XDG_DATA_HOME": str(ttk / "accounts/d"),
                                              "XDG_CACHE_HOME": str(ttk / "accounts/e")})
    text = render_profile(policy).split(";; 비밀 경로")[0]
    allow = text.split("(allow file-read-data file-write*\n")[1]
    real = os.path.realpath(ttk)
    assert f'(subpath "{real}/accounts/a")' in allow
    for rel in ["accounts", "accounts/b", "accounts/c", "accounts/d", "accounts/e", "data", "backups"]:
        assert f'(subpath "{real}/{rel}")' not in allow


def test_transfer_dirs_and_guard_log_keep_narrow_access(tmp_path):
    ttk = tmp_path / ".ttakkari"
    policy = policy_for(home=tmp_path, ttakkari_home=ttk, outbox=ttk / "data/outbox/s1",
                        write_roots=[ttk / "data/uploads", ttk / "inbox", ttk / "config.env"],
                        env={"TTAKKARI_GUARD_LOG": str(ttk / "data/guard.log")}, engine="claude")
    text = render_profile(policy)
    allow = text.split("(allow file-read-data file-write*\n")[1].split(";; 비밀 경로")[0]
    real = os.path.realpath(ttk)
    for rel in ["data/uploads", "inbox", "data/outbox/s1"]:
        assert f'(subpath "{real}/{rel}")' in allow
    assert f'(literal "{real}/data/guard.log")' in allow
    assert f'(subpath "{real}/config.env")' not in allow


@pytest.mark.parametrize("rel", [".", ".ttakkari", ".ttakkari/accounts", ".codex", ".config/gh"])
def test_overbroad_or_foreign_credential_config_dir_is_rejected(tmp_path, rel):
    with pytest.raises(ValueError):
        policy_for(home=tmp_path, ttakkari_home=tmp_path / ".ttakkari",
                   outbox=tmp_path / ".ttakkari/data/outbox/s1", write_roots=[],
                   env={"CLAUDE_CONFIG_DIR": str(tmp_path / rel)}, engine="claude")


def test_explicit_default_claude_config_keeps_credentials(tmp_path):
    policy = policy_for(home=tmp_path, ttakkari_home=tmp_path / ".ttakkari",
                        outbox=tmp_path / ".ttakkari/data/outbox/s1", write_roots=[],
                        env={"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude")}, engine="claude")
    assert seatbelt.CLAUDE_DEFAULT_CREDENTIALS not in render_profile(policy)


def test_symlinked_credential_store_target_is_denied(tmp_path):
    target = tmp_path / "credentials-elsewhere"
    target.mkdir()
    (tmp_path / ".gemini").symlink_to(target, target_is_directory=True)
    assert f'(subpath "{target.resolve()}")' in render_profile(_policy(tmp_path))


def test_env_examples_cannot_reopen_credential_directories(tmp_path):
    text = render_profile(_policy(tmp_path))
    example_allow = text[text.rindex("(allow "):]
    for rel in [".ttakkari", ".gemini", ".codex", ".aws"]:
        assert f'(require-not (subpath "{os.path.realpath(tmp_path / rel)}"))' in example_allow


def test_env_examples_cannot_reopen_symlinked_credential_file(tmp_path):
    example = tmp_path / "project/.env.example"
    example.parent.mkdir()
    example.write_text("decoy")
    (tmp_path / ".netrc").symlink_to(example)
    text = render_profile(_policy(tmp_path))
    assert f'(require-not (literal "{example.resolve()}"))' in text[text.rindex("(allow "):]


def test_deployed_runtime_is_readable_without_reopening_other_ttakkari_data(tmp_path, monkeypatch):
    ttk = tmp_path / ".ttakkari"
    release = ttk / "deploy/releases" / ("a" * 40)
    monkeypatch.setattr(seatbelt, "__file__", str(release / "src/ttakkari/security/seatbelt.py"))
    text = render_profile(_policy(tmp_path))
    # Deployed Python needs this release's modules and venv for hooks and MCP.
    rule = f'(allow file-read-data (subpath "{release.resolve()}"))'
    assert rule in text
    assert text.index(";; ttakkari 홈") < text.index(rule) < text.index(";; 비밀 경로")
    assert f'(subpath "{(ttk / "deploy").resolve()}")' not in text
    assert f'(subpath "{(ttk / "accounts/b").resolve()}")' not in text
    assert f'(allow file-read-data file-write* (subpath "{release.resolve()}"))' not in text


def test_runtime_exception_rejects_a_non_release_inside_ttakkari(tmp_path, monkeypatch):
    foreign = tmp_path / ".ttakkari/accounts/b"
    monkeypatch.setattr(seatbelt, "__file__", str(foreign / "src/ttakkari/security/seatbelt.py"))
    with pytest.raises(ValueError, match="runtime"):
        _policy(tmp_path)


def test_generic_write_roots_cannot_reopen_production_db_sockets(tmp_path):
    ttk = tmp_path / ".ttakkari"
    policy = policy_for(home=tmp_path, ttakkari_home=ttk, outbox=ttk / "data/outbox/s1",
                        write_roots=[Path("/tmp"), Path("/"), tmp_path], env={}, engine="claude")
    assert policy.socket_roots == []


def test_test_db_socket_exception_is_a_dedicated_workspace_directory(tmp_path):
    ttk = tmp_path / ".ttakkari"
    workspace = tmp_path / "ws"
    policy = policy_for(home=tmp_path, ttakkari_home=ttk, outbox=ttk / "data/outbox/s1",
                        write_roots=[Path("/tmp")], workspace=workspace, env={}, engine="claude")
    assert policy.socket_roots == [workspace / ".ttakkari-db-sockets"]


def test_test_db_socket_directory_cannot_escape_workspace_by_symlink(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".ttakkari-db-sockets").symlink_to("/tmp", target_is_directory=True)
    with pytest.raises(ValueError, match="socket"):
        policy_for(home=tmp_path, ttakkari_home=tmp_path / ".ttakkari",
                   outbox=tmp_path / ".ttakkari/data/outbox/s1", write_roots=[],
                   workspace=workspace, env={}, engine="claude")
