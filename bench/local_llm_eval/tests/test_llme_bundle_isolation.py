"""The candidate process must not be able to read the sealed bundle (measured, not assumed).

Real OS denial is measured through the runner in holdout mode: the probe records the exception type of
a read of a sealed path. The real bundle is included when it exists on this host; a sealed-like directory
made in tmp_path is always included. The environment-denied / defect distinction follows the existing
deny tests: an engine sandbox that cannot apply a profile skips with its reason, a defect fails.
"""

from pathlib import Path

import pytest
from local_llm_eval import sandbox
from local_llm_eval.bundle_paths import SEALED_ROOT
from local_llm_eval.runner import run_check


def _require_isolation():
    status = sandbox.availability()
    if not status.available:
        assert not status.defect, "isolation defect: " + status.reason
        pytest.skip("OS deny unverified in this engine: " + status.reason)


def test_profile_never_names_the_sealed_root_and_refuses_overlapping_roots(tmp_path):
    runtime = sandbox.runtime_paths()
    profile = sandbox.profile_text(runtime, [tmp_path / "work"])
    assert str(SEALED_ROOT) not in profile and "bench-sealed" not in profile
    for root in (SEALED_ROOT, SEALED_ROOT.parent, SEALED_ROOT / "tasks"):
        with pytest.raises(ValueError):
            sandbox.profile_text(runtime, [root])


def test_runner_holdout_profile_excludes_the_sealed_root(tmp_path, monkeypatch):
    """The profile handed to the candidate process is built from the workspace roots only."""
    from local_llm_eval import runner
    from local_llm_eval.channel import TransportResult

    seen = {}
    monkeypatch.setattr(
        sandbox, "availability", lambda: sandbox.Availability(True, "mock readiness")
    )

    def capture(command, cwd, env, spec, timeout):
        seen["profile"] = Path(command[2]).read_text()
        seen["spec_keys"] = sorted(spec)
        return TransportResult("OBS", {"v": 1}, 0)

    monkeypatch.setattr(runner, "collect", capture)
    run_check(tmp_path, "profile", "rec('v', 1)", mode="holdout")
    assert "bench-sealed" not in seen["profile"]
    assert str(SEALED_ROOT) not in seen["profile"]
    assert "expected" not in seen["spec_keys"]


def test_deny_driver_covers_the_sealed_paths(tmp_path):
    _require_isolation()
    results = sandbox.run_deny_checks(tmp_path)
    reads = results["reads"]
    assert reads["sealed_like"] == "PermissionError", reads
    if SEALED_ROOT.exists():
        assert reads["sealed_bundle"] == "PermissionError", reads
        assert reads["sealed_bundle_gold"] == "PermissionError", reads
    assert all(value == "PermissionError" for value in reads.values()), reads


PROBE = """
import os
def read(path):
    try:
        with open(path, 'rb') as handle:
            handle.read(16)
        return 'ALLOWED'
    except OSError as exc:
        return type(exc).__name__
def listing(path):
    try:
        os.listdir(path)
        return 'ALLOWED'
    except OSError as exc:
        return type(exc).__name__
for name, path in TARGETS.items():
    rec(name + '_read', read(path))
with open('allowed.txt', 'w') as handle:
    handle.write('ok')
with open('allowed.txt') as handle:
    rec('positive', handle.read())
rec('root_listing', listing(ROOT))
rec('parent_listing', listing(PARENT))
"""


def test_candidate_side_process_cannot_read_or_list_the_sealed_paths(tmp_path):
    """End to end through run_check(mode='holdout'): PermissionError for every sealed target."""
    _require_isolation()
    work = tmp_path / "work"
    work.mkdir()
    like = tmp_path / "sealed-like"
    like.mkdir(mode=0o700)
    (like / "answer.txt").write_text("canary", encoding="utf-8")
    targets = {"like": str(like / "answer.txt")}
    root, parent = str(like), str(like.parent)
    if SEALED_ROOT.exists():
        targets["manifest"] = str(SEALED_ROOT / "manifest.sealed.json")
        golds = sorted(SEALED_ROOT.glob("tasks/*/gold.py"))
        if golds:
            targets["gold"] = str(golds[0])
        root, parent = str(SEALED_ROOT), str(SEALED_ROOT.parent)
    probe = f"TARGETS = {targets!r}\nROOT = {root!r}\nPARENT = {parent!r}\n" + PROBE
    run = run_check(work, "sealed-deny", probe, mode="holdout")
    assert run.status == "OBS", (run.status, run.detail, run.stderr_tail[-300:])
    assert run.facts["isolation"]["active"] is True
    assert run.obs["positive"] == "ok", (
        "the probe can use its own workspace, so a deny is a real deny"
    )
    for name in targets:
        assert run.obs[name + "_read"] == "PermissionError", run.obs
    assert run.obs["root_listing"] == "PermissionError", run.obs
    assert run.obs["parent_listing"] == "PermissionError", run.obs
