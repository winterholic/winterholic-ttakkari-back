"""BlobStore must stay inside its root even if a directory is swapped for a symlink mid-operation.

The attack is injected immediately before every dir_fd based filesystem call of one put()/get()
(each call index x each directory depth), which covers the old "check path, then os.replace" window.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from ttakkari.kb_parsers import BlobStore

_HOOKED = ("open", "mkdir", "stat", "rename", "unlink")
_DATA = b"toctou-payload"
_DIGEST = hashlib.sha256(_DATA).hexdigest()


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    """Override session DB fixture from conftest for pure unit testing without DB connection."""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """Override per-test DB clean fixture from conftest."""
    yield


def _uses_dir_fd(kwargs: dict) -> bool:
    return any(
        kwargs.get(k) is not None for k in ("dir_fd", "src_dir_fd", "dst_dir_fd")
    )


class _FsInjector:
    """Counts dir_fd calls and runs ``attack`` right before call number ``fire_at``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, attack=None, fire_at=-1):
        self.calls = 0
        self.fired = False
        self._attack = attack
        self._fire_at = fire_at
        for name in _HOOKED:
            real = getattr(os, name)
            monkeypatch.setattr(os, name, self._wrap(real))

    def _wrap(self, real):
        def wrapper(*args, **kwargs):
            if _uses_dir_fd(kwargs):
                if self.calls == self._fire_at and self._attack and not self.fired:
                    self.fired = True
                    self._attack()
                self.calls += 1
            return real(*args, **kwargs)

        return wrapper


def _swap_for_symlink(target: Path, outside: Path) -> None:
    if target.is_symlink():
        return
    if target.exists():
        target.rename(target.with_name(target.name + ".moved"))
    target.symlink_to(outside, target_is_directory=True)


def _all_entries(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def _make_store(tmp_path: Path, *, dirs_exist: bool) -> tuple[BlobStore, Path, Path]:
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = BlobStore(root)
    if dirs_exist:
        (store.root_dir / store.relpath_for(_DIGEST)).parent.mkdir(parents=True)
    return store, store.root_dir, outside


@pytest.mark.parametrize("dirs_exist", [False, True])
def test_put_never_writes_outside_root_when_directory_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirs_exist: bool
):
    (tmp_path / "probe").mkdir()
    probe_store, _, _ = _make_store(tmp_path / "probe", dirs_exist=dirs_exist)
    counter = _FsInjector(monkeypatch)
    probe_store.put(_DATA)
    total_calls = counter.calls
    monkeypatch.undo()
    assert (
        total_calls >= 6
    )  # root/dir walk + tmp create + stat + rename were all dir_fd based

    rel_parts = BlobStore(tmp_path / "x").relpath_for(_DIGEST).parts[:-1]
    injected = 0
    for fire_at in range(total_calls):
        for depth in range(len(rel_parts) + 1):  # 0 = the store root itself
            case = tmp_path / f"case-{dirs_exist}-{fire_at}-{depth}"
            case.mkdir()
            store, root, outside = _make_store(case, dirs_exist=dirs_exist)
            target = root.joinpath(*rel_parts[:depth])
            with monkeypatch.context() as m:
                injector = _FsInjector(
                    m,
                    attack=lambda t=target, o=outside: _swap_for_symlink(t, o),
                    fire_at=fire_at,
                )
                try:
                    store.put(_DATA)
                except (OSError, ValueError):
                    pass  # failing closed is acceptable; escaping is not
            injected += injector.fired
            assert _all_entries(outside) == [], (
                f"escaped write: call={fire_at} depth={depth} dirs_exist={dirs_exist} "
                f"{_all_entries(outside)}"
            )
    assert injected > 0


def test_stream_put_and_second_put_survive_swap_without_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, root, outside = _make_store(tmp_path, dirs_exist=False)
    store.put(_DATA)
    # swap the fan-out directory for a symlink after the blob exists, then put the same data again
    with monkeypatch.context() as m:
        _FsInjector(
            m,
            attack=lambda: _swap_for_symlink(root / "sha256", outside),
            fire_at=1,
        )
        try:
            store.put(io.BytesIO(_DATA))
        except (OSError, ValueError):
            pass
    assert _all_entries(outside) == []


def test_planted_symlink_is_replaced_by_directory_not_followed(tmp_path: Path):
    store, root, outside = _make_store(tmp_path, dirs_exist=False)
    (root / "sha256").symlink_to(outside, target_is_directory=True)
    digest, _, path = store.put(_DATA)
    assert digest == _DIGEST
    assert _all_entries(outside) == []
    assert not (root / "sha256").is_symlink()
    assert store.get(digest) == _DATA
    assert store.verify(digest)
    assert path.is_relative_to(root)


def test_reads_never_follow_a_symlinked_directory(tmp_path: Path):
    store, root, outside = _make_store(tmp_path, dirs_exist=False)
    store.put(_DATA)
    decoy = outside / "sha256"
    decoy_leaf = decoy / _DIGEST[:2] / _DIGEST[2:4]
    decoy_leaf.mkdir(parents=True)
    (decoy_leaf / f"{_DIGEST}.blob").write_bytes(b"DECOY")
    (root / "sha256").rename(root / "sha256.moved")
    (root / "sha256").symlink_to(decoy, target_is_directory=True)

    assert not store.exists(_DIGEST)
    assert not store.verify(_DIGEST)
    with pytest.raises(FileNotFoundError):
        store.get(_DIGEST)
    # reads must not mutate the store (the old implementation unlinked the symlink in path_for)
    assert (root / "sha256").is_symlink()


def _store_with_blob_and_decoy(case: Path) -> tuple[BlobStore, Path, Path]:
    case.mkdir()
    store, root, outside = _make_store(case, dirs_exist=False)
    store.put(_DATA)
    decoy_leaf = outside / "sha256" / _DIGEST[:2] / _DIGEST[2:4]
    decoy_leaf.mkdir(parents=True)
    (decoy_leaf / f"{_DIGEST}.blob").write_bytes(b"DECOY")
    return store, root, outside


def test_get_race_never_returns_outside_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, _, _ = _store_with_blob_and_decoy(tmp_path / "probe")
    counter = _FsInjector(monkeypatch)
    store.get(_DIGEST)
    total_calls = counter.calls
    monkeypatch.undo()
    assert total_calls >= 4

    parts_all = store.relpath_for(_DIGEST).parts[:-1]
    for fire_at in range(total_calls):
        for depth in range(1, len(parts_all) + 1):
            case_store, root, outside = _store_with_blob_and_decoy(
                tmp_path / f"case-{fire_at}-{depth}"
            )
            target = root.joinpath(*parts_all[:depth])
            with monkeypatch.context() as m:
                _FsInjector(
                    m,
                    attack=lambda t=target, r=root, o=outside: _swap_for_symlink(
                        t, o / t.relative_to(r)
                    ),
                    fire_at=fire_at,
                )
                try:
                    got = case_store.get(_DIGEST)
                except FileNotFoundError:
                    got = None
            assert got in (None, _DATA), (
                f"followed symlink: call={fire_at} depth={depth}"
            )


def test_unsupported_platform_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(NotImplementedError, match="dir_fd"):
        BlobStore(tmp_path / "s")
