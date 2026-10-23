"""Non-regular filesystem entries are refused, never skipped, by every commitment computation.

Covers the sealed bundle (read_bundle, verified_bundle, seal) and the workspace manifests the evaluator
and the candidate-visible export rely on (dir_manifest, start tree, end tree). All entries are made in
synthetic temporary trees; the real bundle is only scanned read-only.
"""

import os
import shutil
import socket
import stat
import tempfile
from pathlib import Path

import pytest
from local_llm_eval import bundle, codes
from local_llm_eval.bundle_paths import SEALED_ROOT
from local_llm_eval.evaluator import evaluate_attempt
from local_llm_eval.manifest import NON_REGULAR_PREFIXES, dir_manifest
from local_llm_eval.simulate import gold_ends, materialize, reports_for
from local_llm_eval.tasks import REGISTRY
from local_llm_eval.tests.test_llme_bundle import SYNTHETIC, TASK_ID, make_bundle


def _fifo(path: Path) -> None:
    os.mkfifo(path, 0o600)


def _symlink_to_file(path: Path) -> None:
    target = path.parent / "real-target.txt"
    target.write_text("x")
    os.symlink(target, path)


def _symlink_to_missing(path: Path) -> None:
    os.symlink(path.parent / "does-not-exist", path)


def _symlink_to_directory(path: Path) -> None:
    os.symlink(path.parent, path)


def _symlink_to_outside(path: Path) -> None:
    os.symlink("/etc/hosts", path)


def _hardlink_inside(path: Path) -> None:
    os.link(path.parent / "a.py", path)


def _hardlink_outside(path: Path) -> None:
    outside = Path(tempfile.mkdtemp(prefix="llme-hl-")) / "outside.txt"
    outside.write_text("x")
    os.link(outside, path)


KINDS = {
    "symlink_to_file": _symlink_to_file,
    "symlink_dangling": _symlink_to_missing,
    "symlink_to_directory": _symlink_to_directory,
    "symlink_outside": _symlink_to_outside,
    "fifo": _fifo,
    "hardlink_inside": _hardlink_inside,
    "hardlink_outside": _hardlink_outside,
}


def _public_dir(root: Path) -> Path:
    return root / f"tasks/{TASK_ID}/public/workspace"


def _plant(root: Path, kind: str, where: str) -> None:
    folder = {
        "root": root,
        "workspace": _public_dir(root),
        "nested": _public_dir(root) / "deep" / "er",
    }[where]
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if kind == "hardlink_inside" and not (folder / "a.py").exists():
        (folder / "a.py").write_text("x = 1\n")
        (folder / "a.py").chmod(0o600)
    KINDS[kind](folder / "planted")


@pytest.mark.parametrize("where", ["root", "workspace", "nested"])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_bundle_refuses_every_non_regular_entry(tmp_path, kind, where):
    root, commitments_path = make_bundle(tmp_path)
    _plant(root, kind, where)
    with pytest.raises(bundle.BundleError) as caught:
        bundle.read_bundle(root)
    assert caught.value.code in (
        codes.INFRA_SEALED_COMMITMENT_MISMATCH,
        codes.INFRA_SEALED_PERMISSIONS,
    )
    with pytest.raises(bundle.BundleError):
        bundle.verified_bundle(root, commitments_path)
    with pytest.raises(bundle.BundleError):
        bundle.load_task(TASK_ID, root, commitments_path)


@pytest.mark.parametrize("kind", ["fifo", "symlink_to_file", "hardlink_outside"])
def test_seal_refuses_before_changing_any_mode(tmp_path, kind):
    root, commitments_path = make_bundle(tmp_path)
    _plant(root, kind, "root")
    victim = Path(os.path.realpath(root / "planted"))
    before = stat.S_IMODE(victim.lstat().st_mode)
    with pytest.raises(bundle.BundleError):
        bundle.seal(root, commitments_path, "t")
    assert stat.S_IMODE(victim.lstat().st_mode) == before


def test_a_fifo_never_blocks_the_scan_or_the_read(tmp_path):
    """The FIFO has no writer: opening it for reading without O_NONBLOCK would hang forever."""
    root, _ = make_bundle(tmp_path)
    _fifo(root / "queue")
    with pytest.raises(bundle.BundleError):
        bundle.read_bundle(root)
    descriptor_before = len(os.listdir("/dev/fd"))
    with pytest.raises(bundle.BundleError):
        bundle._read_regular(root / "queue", "queue")
    assert len(os.listdir("/dev/fd")) <= descriptor_before


def test_an_entry_swapped_after_the_scan_is_refused_on_open(tmp_path):
    root, _ = make_bundle(tmp_path)
    target = root / f"tasks/{TASK_ID}/gold.py"
    target.unlink()
    os.symlink("/etc/hosts", target)
    with pytest.raises(bundle.BundleError):
        bundle._read_regular(target, "gold.py")
    target.unlink()
    _fifo(target)
    with pytest.raises(bundle.BundleError):
        bundle._read_regular(target, "gold.py")


def test_a_bound_unix_socket_is_refused():
    # AF_UNIX paths are limited to ~100 bytes, so build this bundle under a short path
    short = Path(tempfile.mkdtemp(prefix="lb", dir="/tmp"))
    try:
        root, commitments_path = make_bundle(short)
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(root / "s"))
        except OSError as exc:  # pragma: no cover - platform dependent
            pytest.skip(f"cannot bind a socket here: {exc}")
        try:
            assert stat.S_ISSOCK((root / "s").lstat().st_mode)
            with pytest.raises(bundle.BundleError) as caught:
                bundle.verified_bundle(root, commitments_path)
            assert caught.value.code == codes.INFRA_SEALED_COMMITMENT_MISMATCH
        finally:
            server.close()
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_a_clean_synthetic_bundle_still_loads(tmp_path):
    root, commitments_path = make_bundle(tmp_path)
    assert len(bundle.verified_bundle(root, commitments_path)[0]) == len(SYNTHETIC)


def test_a_symlinked_sealed_root_is_refused(tmp_path):
    root, commitments_path = make_bundle(tmp_path)
    link = tmp_path / "link-to-bundle"
    os.symlink(root, link)
    with pytest.raises(bundle.BundleError) as caught:
        bundle.verified_bundle(link, commitments_path)
    assert caught.value.code == codes.INFRA_SEALED_MISSING


# ---- workspace manifests (start tree, end tree, candidate-visible export) --------------------


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_dir_manifest_marks_instead_of_skipping(tmp_path, kind):
    (tmp_path / "a.py").write_text("x = 1\n")
    KINDS[kind](tmp_path / "planted")
    marked = {
        p: d
        for p, d in dir_manifest(tmp_path).items()
        if d.startswith(NON_REGULAR_PREFIXES)
    }
    assert marked, (kind, dir_manifest(tmp_path))
    assert "planted" in marked or kind.startswith("hardlink")


def test_a_symlink_named_like_an_ignored_cache_dir_is_still_recorded(tmp_path):
    os.symlink("/etc", tmp_path / "__pycache__")
    assert dir_manifest(tmp_path)["__pycache__"].startswith("symlink:")


@pytest.mark.parametrize("kind", ["fifo", "symlink_to_file", "hardlink_outside"])
def test_special_entry_in_the_end_tree_is_a_forbidden_write(kind):
    t = REGISTRY["B4-SGL-D"]()
    with materialize(t, gold_ends(t), reports_for(t, gold_ends(t))) as attempt:
        KINDS[kind](Path(attempt.stages[0].end_dir) / "planted")
        ev = evaluate_attempt(t, attempt)
    assert ev.fail_code == "FAIL_FORBIDDEN_WRITE", kind


@pytest.mark.parametrize("kind", ["fifo", "symlink_to_directory", "hardlink_outside"])
def test_special_entry_in_the_start_tree_is_a_base_mismatch(kind):
    t = REGISTRY["B4-SGL-D"]()
    with materialize(t, gold_ends(t), reports_for(t, gold_ends(t))) as attempt:
        KINDS[kind](Path(attempt.stages[0].start_dir) / "planted")
        ev = evaluate_attempt(t, attempt)
    assert (ev.verdict, ev.fail_code) == (codes.INFRA, codes.INFRA_BASE_MISMATCH), kind


def test_export_public_tree_has_only_regular_single_link_files(tmp_path):
    root, commitments_path = make_bundle(tmp_path)
    out = tmp_path / "exported"
    bundle.export_public(TASK_ID, out, root, commitments_path)
    assert not any(
        v.startswith(NON_REGULAR_PREFIXES) for v in dir_manifest(out).values()
    )
    _fifo(out / "planted")
    assert any(v.startswith("special:") for v in dir_manifest(out).values())


# ---- the real bundle (read-only scan, counts only) -------------------------------------------


@pytest.mark.skipif(
    not SEALED_ROOT.exists(), reason="the sealed bundle is not on this host"
)
def test_the_real_bundle_has_no_non_regular_entries():
    entries = bundle.scan_entries(SEALED_ROOT)
    assert len(entries) > 0
    non_regular = 0
    for _, path in entries:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            non_regular += 1
    assert non_regular == 0
