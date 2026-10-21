from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ttakkari.collector import blobs as blobs_module
from ttakkari.collector import pipeline
from ttakkari.collector.blobs import FileBlobStore


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_quota_counts_durable_orphans_and_deduplicates_same_sha(tmp_path: Path):
    store = FileBlobStore(tmp_path, cap_bytes=8)
    first = b"orphan!"  # durable, even though no database row references it
    store.put(first)

    assert store.put(first) == f"cblob:sha256:{_sha(first)}"
    with pytest.raises(OSError, match="collector blob quota"):
        store.put(b"another")
    assert store.get(f"cblob:sha256:{_sha(first)}") == first


def test_same_size_corruption_is_repaired_atomically_by_put(tmp_path: Path):
    store = FileBlobStore(tmp_path, cap_bytes=32)
    original = b"immutable raw bytes"
    handle = store.put(original)
    path = store._path(_sha(original))
    path.write_bytes(b"X" * len(original))

    assert store.put(original) == handle
    assert store.get(handle) == original


def test_repair_readers_never_observe_partially_replaced_file(tmp_path: Path, monkeypatch):
    store = FileBlobStore(tmp_path, cap_bytes=64)
    original = b"atomic original"
    handle = store.put(original)
    path = store._path(_sha(original))
    path.write_bytes(b"X" * len(original))
    before_replace, continue_replace = threading.Event(), threading.Event()
    real_replace = blobs_module.os.replace

    def paused_replace(source, destination):
        if Path(destination) == path:
            before_replace.set()
            assert continue_replace.wait(timeout=5)
        return real_replace(source, destination)

    monkeypatch.setattr(blobs_module.os, "replace", paused_replace)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.put, original)
        assert before_replace.wait(timeout=5)
        assert path.read_bytes() == b"X" * len(original)
        continue_replace.set()
        assert future.result(timeout=5) == handle
    assert store.get(handle) == original


def test_put_then_precommit_failure_keeps_recoverable_raw_lineage(tmp_path: Path):
    store = FileBlobStore(tmp_path, cap_bytes=64)
    data = b"precommit source bytes"
    item_id = str(uuid.uuid4())
    lineage = {
        "item_id": item_id,
        "job_id": str(uuid.uuid4()),
        "tenant_id": "tenant-a",
        "scope": "personal",
        "source_key": "source-a",
        "url": "https://example.test/article",
    }
    try:
        handle = store.put(data, lineage=lineage)
        raise RuntimeError("injected DB transaction rollback after durable put")
    except RuntimeError:
        pass

    reopened = FileBlobStore(tmp_path, cap_bytes=64)
    record = reopened.lineage_records()[0]
    assert record == {**lineage, "blob_sha256": _sha(data)}
    assert reopened.get(handle) == data
    assert reopened.usage_bytes() == len(data)


def test_failed_final_replace_leaves_recoverable_stage_and_lineage(tmp_path: Path, monkeypatch):
    store = FileBlobStore(tmp_path, cap_bytes=64)
    data = b"rename boundary raw"
    item_id = str(uuid.uuid4())
    lineage = {
        "item_id": item_id,
        "job_id": str(uuid.uuid4()),
        "tenant_id": "tenant-a",
        "scope": "personal",
        "source_key": "source-a",
        "url": "https://example.test/rename",
    }
    final_path = store._path(_sha(data))
    real_replace = blobs_module.os.replace

    def fail_final_replace(source, destination):
        if Path(destination) == final_path:
            raise OSError("injected process interruption at final blob rename")
        return real_replace(source, destination)

    monkeypatch.setattr(blobs_module.os, "replace", fail_final_replace)
    with pytest.raises(OSError, match="injected process interruption"):
        store.put(data, lineage=lineage)

    monkeypatch.undo()
    reopened = FileBlobStore(tmp_path, cap_bytes=64)
    assert reopened.usage_bytes() == len(data)
    assert reopened.get(f"cblob:sha256:{_sha(data)}") == data
    assert reopened.lineage_records() == [{**lineage, "blob_sha256": _sha(data)}]


def test_stale_partial_stage_is_reconciled_before_quota_check(tmp_path: Path):
    store = FileBlobStore(tmp_path, cap_bytes=16)
    data = b"recovered raw"
    path = store._path(_sha(data))
    path.parent.mkdir(parents=True)
    (path.parent / f".stage-{_sha(data)}").write_bytes(data[:4])

    assert store.put(data) == f"cblob:sha256:{_sha(data)}"
    assert store.get(f"cblob:sha256:{_sha(data)}") == data
    assert not list(tmp_path.rglob(".stage-*"))


def test_complete_stage_from_crash_is_promoted_and_counted_once(tmp_path: Path):
    data = b"complete stage"
    path = FileBlobStore(tmp_path, cap_bytes=16)._path(_sha(data))
    path.parent.mkdir(parents=True)
    (path.parent / f".stage-{_sha(data)}").write_bytes(data)

    reopened = FileBlobStore(tmp_path, cap_bytes=16)
    assert reopened.usage_bytes() == len(data)
    assert reopened.get(f"cblob:sha256:{_sha(data)}") == data
    assert not list(tmp_path.rglob(".stage-*"))


def test_cross_process_different_blobs_cannot_both_cross_cap(tmp_path: Path):
    script = r"""
import sys
from pathlib import Path
from ttakkari.collector.blobs import FileBlobStore
root, payload = Path(sys.argv[1]), sys.argv[2].encode()
try:
    FileBlobStore(root, cap_bytes=9).put(payload)
    print("stored")
except OSError as exc:
    if "collector blob quota" not in str(exc):
        raise
    print("denied")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    with ThreadPoolExecutor(max_workers=2) as pool:
        procs = [
            pool.submit(
                subprocess.run,
                [sys.executable, "-c", script, str(tmp_path), value],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            for value in ("alpha", "bravo")
        ]
        results = [future.result() for future in procs]

    assert all(result.returncode == 0 for result in results), [r.stderr for r in results]
    assert sorted(r.stdout.strip() for r in results) == ["denied", "stored"]
    files = [p for p in tmp_path.rglob("*") if p.is_file() and not p.name.startswith(".")]
    assert sum(p.stat().st_size for p in files) <= 9


def test_process_death_after_put_keeps_orphan_and_releases_quota_lock(tmp_path: Path):
    item_id = str(uuid.uuid4())
    script = r"""
import os, sys
from pathlib import Path
from ttakkari.collector.blobs import FileBlobStore
root, item_id = Path(sys.argv[1]), sys.argv[2]
FileBlobStore(root, cap_bytes=8).put(b"orphan!!", lineage={
    "item_id": item_id, "job_id": "job", "tenant_id": "tenant", "scope": "personal",
    "source_key": "source", "url": "https://example.test/",
})
os._exit(73)  # abrupt death before the caller commits its DB page
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), item_id], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 73
    reopened = FileBlobStore(tmp_path, cap_bytes=8)
    assert reopened.usage_bytes() == 8
    assert reopened.get(f"cblob:sha256:{_sha(b'orphan!!')}") == b"orphan!!"
    assert reopened.lineage_records()[0]["item_id"] == item_id
    with pytest.raises(OSError, match="collector blob quota"):
        reopened.put(b"different")


def test_quota_boundary_is_inclusive_and_blob_paths_reject_symlinks(tmp_path: Path):
    store = FileBlobStore(tmp_path / "owned", cap_bytes=4)
    store.put(b"four")
    assert store.put(b"four") == f"cblob:sha256:{_sha(b'four')}"
    with pytest.raises(OSError, match="collector blob quota"):
        store.put(b"fifth")

    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    shard = store._path(_sha(b"other")).parent
    shard.parent.mkdir(parents=True)
    shard.symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(OSError):
        store.put(b"other")
    assert outside.read_bytes() == b"outside"


@pytest.mark.asyncio
async def test_pipeline_cap_precheck_uses_physical_root_bytes_not_db_rows(tmp_path: Path, monkeypatch):
    store = FileBlobStore(tmp_path, cap_bytes=16)
    store.put(b"orphan")
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(collector_blob_cap_bytes=1))
    monkeypatch.setattr(pipeline, "get_deps", lambda: SimpleNamespace(blobs=store))

    assert await pipeline._storage_full() is True


def test_quota_pause_commits_only_prepared_prefix_and_keeps_rejected_tail():
    entries = [pipeline._entry(f"https://example.test/{name}", 0) for name in ("first", "denied", "tail")]
    cursor = {
        "v": 1,
        "frontier": entries,
        "seen": [],
        "processed": 0,
        "discovered": 3,
        "counts": dict.fromkeys(pipeline.COUNT_KEYS, 0),
    }
    prefix = entries[:1]
    prepared = [pipeline.Prepared(prefix[0], uuid.uuid4(), prefix[0]["u"], "a" * 64)]
    item = SimpleNamespace(kind="document", status="accepted")

    advanced = pipeline._advance(cursor, prefix, prepared, [item], spec=None, req=None)

    assert advanced["frontier"] == entries[1:]
    assert advanced["processed"] == 1
    assert advanced["counts"]["accepted"] == 1
