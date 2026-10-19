from pathlib import Path

import pytest

from ttakkari.broker.policy import (
    BrokerError,
    is_sensitive,
    resolve_in_root,
    validate_absolute_in_roots,
    validate_workspace_root,
)


def test_resolve_in_root_rejects_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")

    with pytest.raises(BrokerError):
        resolve_in_root(root, "../outside.txt")


@pytest.mark.parametrize("rel", ["/etc/passwd", "sub/../../outside", "sub\\..\\..\\outside", "bad\x00name"])
def test_resolve_in_root_rejects_unsafe_relative_paths(tmp_path: Path, rel: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(BrokerError):
        resolve_in_root(root, rel, must_exist=False)


def test_resolve_in_root_allows_normalized_in_root_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "nested" / "file.txt"
    target.parent.mkdir()
    target.write_text("ok")

    assert resolve_in_root(root, "nested//./file.txt").path == target.resolve()
    assert resolve_in_root(root, r"nested\file.txt").path == target.resolve()


def test_resolve_in_root_rejects_symlink_chain_escaping_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "link2").symlink_to(outside, target_is_directory=True)
    (root / "link1").symlink_to(root / "link2", target_is_directory=True)

    with pytest.raises(BrokerError):
        resolve_in_root(root, "link1/secret.txt")


def test_validate_absolute_in_roots_resolves_symlink_to_real_target(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "file.txt"
    target.write_text("ok")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(target)

    assert validate_absolute_in_roots(alias, [root]).path == target.resolve()


def test_validate_workspace_root_requires_allowed_real_directory(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    workspace = allowed / "project"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    assert validate_workspace_root(str(workspace), [allowed]) == workspace.resolve()
    with pytest.raises(BrokerError):
        validate_workspace_root(str(outside), [allowed])


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", "id_ed25519", "server.pem", ".ssh/config", ".aws/credentials", ".config/gcloud/application_default_credentials.json",
     "home/.codex/auth.json", "home/.claude/.credentials.json"],
)
def test_is_sensitive_recognizes_sensitive_names_and_directories(path: str) -> None:
    assert is_sensitive(Path(path))


def test_is_sensitive_matches_case_insensitively() -> None:
    assert is_sensitive(Path("PROJECT/.ENV.PROD"))
    assert is_sensitive(Path("PROJECT/.SSH/id_rsa"))


def test_unicode_lookalike_is_not_treated_as_ascii_env_name() -> None:
    assert not is_sensitive(Path(".еnv"))  # Cyrillic small letter ie


def test_harness_folders_are_not_sensitive_except_login_tokens() -> None:
    # 하네스 폴더는 권한 폴더로 열 수 있어야 한다. 토큰 파일만 막는다.
    for ok in ("home/.claude", "home/.claude/settings.json", "home/.claude/skills/x/SKILL.md", "home/.codex/config.toml",
               "proj/auth.json"):
        assert not is_sensitive(Path(ok)), ok


# S-6b·S-6c: "*secret*" 부분일치를 실제 비밀 파일명으로 좁혔다.
@pytest.mark.parametrize(
    "path",
    ["secrets.json", "secret.json", "secrets.yaml", "secrets.yml", "secrets.toml", "secrets.env", "secrets.txt",
     ".secrets", "config/secrets"],
)
def test_is_sensitive_recognizes_real_secret_filenames(path: str) -> None:
    assert is_sensitive(Path(path))


@pytest.mark.parametrize(
    "path",
    ["src/secret-v2-bridge.ts", "SecretManager.ts", "my-secret-notes.md", "docs/secrets-overview.md"],
)
def test_is_sensitive_allows_secret_substring_in_ordinary_names(path: str) -> None:
    assert not is_sensitive(Path(path))


# 우회 3: 앞 8KiB만 읽으면 인증서 뒤 개인키를 놓친다. 전체를 검사한다.
def test_is_sensitive_pem_finds_private_key_past_8kib(tmp_path: Path) -> None:
    cert = "-----BEGIN CERTIFICATE-----\n" + "A" * 64 + "\n-----END CERTIFICATE-----\n"
    key = "-----BEGIN RSA PRIVATE KEY-----\n" + "B" * 64 + "\n-----END RSA PRIVATE KEY-----\n"
    bundle = tmp_path / "chain.pem"
    bundle.write_text(cert + "#" * (8192 - len(cert) + 128) + key)
    raw = bundle.read_bytes()
    assert len(raw) > 8192
    assert raw.find(b"PRIVATE KEY") > 8192
    assert is_sensitive(bundle)
    pure = tmp_path / "large-cert.pem"
    pure.write_text(cert + "#" * (9000 - len(cert)))
    assert not is_sensitive(pure)


# 우회 4: 실제 비밀 파일명 변형은 민감, 일반 소스·문서는 비민감.
@pytest.mark.parametrize(
    "path",
    ["my-secrets.json", "prod-secrets.yml", "application-secrets.yaml", "config/my-secrets.env"],
)
def test_is_sensitive_recognizes_secret_filename_variants(path: str) -> None:
    assert is_sensitive(Path(path))


@pytest.mark.parametrize(
    "path",
    ["src/secret-v2-bridge.ts", "SecretManager.ts", "my-secret-notes.md"],
)
def test_is_sensitive_allows_ordinary_names_with_secret_word(path: str) -> None:
    assert not is_sensitive(Path(path))


# S-2·S-7: .pem은 내용으로 가린다. 순수 인증서만 비민감.
def test_is_sensitive_pem_depends_on_content(tmp_path: Path) -> None:
    cert = tmp_path / "server-cert.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    key = tmp_path / "server-key.pem"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----\n")
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
                      "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n")
    assert not is_sensitive(cert)
    assert is_sensitive(key)
    assert is_sensitive(bundle)
    assert is_sensitive(tmp_path / "missing.pem")
    assert is_sensitive(Path("server.pem"))
