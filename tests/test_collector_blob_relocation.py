"""The owner may link the configured blob root to another volume; links anywhere else stay refused."""
import pytest

from ttakkari.collector import blobs
from ttakkari.config import get_settings

pytestmark = pytest.mark.security


def _use_home(monkeypatch, home):
    settings = get_settings()
    monkeypatch.setattr(type(settings), "home", property(lambda self: home), raising=False)


def test_relocated_blob_root_symlink_is_resolved_and_usable(tmp_path, monkeypatch):
    real = tmp_path / "volume" / "collector-blobs"
    real.mkdir(parents=True)
    home = tmp_path / "home"
    (home / "collector").mkdir(parents=True)
    (home / "collector" / "blobs").symlink_to(real)
    _use_home(monkeypatch, home)
    store = blobs.default_blob_store()
    assert store.root == real.resolve()
    handle = store.put(b"hello relocated blob")
    assert store.get(handle) == b"hello relocated blob"


def test_symlink_inside_the_store_is_still_refused(tmp_path, monkeypatch):
    real = tmp_path / "volume" / "collector-blobs"
    real.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (real / "aa").symlink_to(tmp_path / "elsewhere")
    home = tmp_path / "home"
    (home / "collector").mkdir(parents=True)
    (home / "collector" / "blobs").symlink_to(real)
    _use_home(monkeypatch, home)
    store = blobs.default_blob_store()
    with pytest.raises(OSError):
        store.usage_bytes() if hasattr(store, "usage_bytes") else store._reconcile_stages()
