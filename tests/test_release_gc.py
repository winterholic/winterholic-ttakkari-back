"""배포 릴리스 자동 정리: 쓰이는 릴리스는 절대 지우지 않고, 확신이 없으면 통째로 건너뛴다."""
import json
import os
import time
from pathlib import Path

from ttakkari import release_gc

NOW = 2_000_000_000.0
DAY = 86_400


def sha(n: int) -> str:
    return f"{n:040x}"


def make_deploy(tmp_path: Path, count: int = 20, base_age_days: int = 30) -> Path:
    """릴리스 count 개. 번호가 클수록 최근이다(수정 시각 기준)."""
    root = tmp_path / "deploy"
    (root / "releases").mkdir(parents=True)
    for n in range(count):
        d = root / "releases" / sha(n)
        d.mkdir()
        (d / "payload.bin").write_bytes(b"x" * 1000)
        age = (base_age_days - n) * DAY
        os.utime(d, (NOW - age, NOW - age))
    return root


def kept(root: Path) -> set[str]:
    return {p.name for p in (root / "releases").iterdir()}


def test_keeps_recent_and_deletes_the_rest(tmp_path):
    root = make_deploy(tmp_path)
    res = release_gc.prune(root, keep_recent=5, ps_text="", now=NOW)
    assert res.skipped is None and len(res.deleted) == 15 and res.freed_bytes >= 15 * 1000
    assert kept(root) == {sha(n) for n in range(15, 20)}  # 가장 최근 5개


def test_protects_running_process_state_files_services_launchd_and_active_jobs(tmp_path):
    root = make_deploy(tmp_path)
    (root / "current").write_text(sha(1) + "\n")
    (root / "previous").write_text(sha(2))
    (root / "supervisor.json").write_text(json.dumps({"release": f"/x/deploy/releases/{sha(3)}"}))
    (root / "generations-enabled.json").write_text(json.dumps({"bootstrap": sha(4)}))
    (root / "services").mkdir()
    (root / "services" / "gen.8791.plist").write_text(f"<string>/deploy/releases/{sha(5)}/.venv/bin/python</string>")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "net.winterholic.ttakkari.deployer.plist").write_text(f"deploy/releases/{sha(6)}")
    (agents / "com.other.app.plist").write_text(f"deploy/releases/{sha(7)}")  # ttakkari 가 아니면 보호 근거가 아니다
    (root / "jobs").mkdir()
    (root / "jobs" / "a.json").write_text(json.dumps({"status": "draining", "revision": sha(8)}))
    (root / "jobs" / "b.json").write_text(json.dumps({"status": "succeeded", "revision": sha(9)}))  # 끝난 작업
    ps = f"/usr/bin/python -m ttakkari.cli serve /Users/me/.ttakkari/deploy/releases/{sha(10)}/.venv/bin/python\nother"
    res = release_gc.prune(root, keep_recent=2, ps_text=ps, own_revision=sha(11), launch_agents=agents, now=NOW)
    survivors = kept(root)
    assert {sha(n) for n in range(1, 7)} | {sha(8), sha(10), sha(11)} <= survivors  # 모든 보호 근거
    assert sha(7) not in survivors and sha(9) not in survivors  # 근거가 아닌 것은 지워진다
    assert {sha(18), sha(19)} <= survivors  # 최근 2개
    assert not res.failed


def test_recent_by_age_guard_keeps_a_fresh_build(tmp_path):
    root = make_deploy(tmp_path, count=12)
    fresh = root / "releases" / sha(0)  # 가장 오래된 번호지만 방금 만든 빌드
    os.utime(fresh, (NOW - 60, NOW - 60))
    release_gc.prune(root, keep_recent=2, min_age_seconds=3600, ps_text="", now=NOW)
    assert sha(0) in kept(root)


def test_fail_closed_when_protection_sources_are_unreadable(tmp_path):
    root = make_deploy(tmp_path, count=12)
    res = release_gc.prune(root, keep_recent=2, ps_text="", now=NOW)  # 정상: 지운다
    assert res.deleted
    root2 = make_deploy(tmp_path / "b", count=12)
    (root2 / "jobs").mkdir()
    (root2 / "jobs" / "broken.json").write_text("{ not json")
    res2 = release_gc.prune(root2, keep_recent=2, ps_text="", now=NOW)
    assert res2.skipped and not res2.deleted and len(kept(root2)) == 12  # 작업 기록을 못 읽으면 아무것도 지우지 않는다
    root3 = make_deploy(tmp_path / "c", count=12)
    (root3 / "current").mkdir()  # 파일이어야 할 곳이 디렉터리: 읽기 오류
    res3 = release_gc.prune(root3, keep_recent=2, ps_text="", now=NOW)
    assert res3.skipped and len(kept(root3)) == 12


def test_ignores_non_release_entries_and_symlinks(tmp_path):
    root = make_deploy(tmp_path, count=12)
    (root / "releases" / "notes.txt").write_text("hi")
    (root / "releases" / "short").mkdir()
    link = root / "releases" / sha(99)
    link.symlink_to(root / "releases" / sha(0))
    release_gc.prune(root, keep_recent=2, ps_text="", now=NOW)
    names = kept(root)
    assert "notes.txt" in names and "short" in names and sha(99) in names  # SHA 디렉터리가 아닌 것과 링크는 건드리지 않는다


def test_dry_run_deletes_nothing(tmp_path):
    root = make_deploy(tmp_path, count=12)
    res = release_gc.prune(root, keep_recent=2, ps_text="", now=NOW, dry_run=True)
    assert len(res.deleted) == 10 and len(kept(root)) == 12


def test_missing_releases_dir_is_skipped(tmp_path):
    assert release_gc.prune(tmp_path / "nowhere", ps_text="").skipped


def test_job_is_registered_only_when_the_deploy_dir_exists(tmp_path, monkeypatch):
    from ttakkari import generation_service
    from ttakkari.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "home", tmp_path)
    monkeypatch.setattr(s, "release_gc_interval_seconds", 600)
    assert "release-gc" not in [name for name, _, _ in generation_service.maintenance_jobs(s)]
    (tmp_path / "deploy" / "releases").mkdir(parents=True)
    jobs = {name: interval for name, _, interval in generation_service.maintenance_jobs(s)}
    assert jobs["release-gc"] == 600.0
    monkeypatch.setattr(s, "release_gc_interval_seconds", 0)
    assert "release-gc" not in [name for name, _, _ in generation_service.maintenance_jobs(s)]


def test_real_ps_text_is_parsed(tmp_path):
    ps = f"x deploy/releases/{sha(1)}/.venv/bin/python y\nnoise deploy/releases/short/z\n"
    assert release_gc.running_releases(ps) == {sha(1)}
    assert time.time() > 0
