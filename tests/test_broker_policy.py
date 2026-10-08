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
    [".env", ".env.local", "id_ed25519", "server.pem", ".ssh/config", ".aws/credentials", ".config/gcloud/application_default_credentials.json"],
)
def test_is_sensitive_recognizes_sensitive_names_and_directories(path: str) -> None:
    assert is_sensitive(Path(path))


def test_is_sensitive_matches_case_insensitively() -> None:
    assert is_sensitive(Path("PROJECT/.ENV.PROD"))
    assert is_sensitive(Path("PROJECT/.SSH/id_rsa"))


def test_unicode_lookalike_is_not_treated_as_ascii_env_name() -> None:
    assert not is_sensitive(Path(".еnv"))  # Cyrillic small letter ie
