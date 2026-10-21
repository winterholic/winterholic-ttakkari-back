"""Unit & integration tests for worktree isolation (ADR 0066 D1)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ttakkari.config import Settings
from ttakkari.delivery.provenance import verify_commit_provenance
from ttakkari.delivery.worktree import (
    cleanup_session_worktree,
    create_detached_worktree,
    create_session_worktree,
    is_code_change_request,
    run_git,
    should_isolate_worker,
    validate_writable_roots,
)
from ttakkari.delivery_models import ChangeSetStatus


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """실제 임시 Git 저장소 생성 및 초기 커밋."""
    repo = tmp_path / "origin_repo"
    repo.mkdir()
    run_git(["init", "-b", "main"], repo)
    run_git(["config", "user.name", "Tester"], repo)
    run_git(["config", "user.email", "tester@example.com"], repo)

    initial_file = repo / "README.md"
    initial_file.write_text("# Test Repo\n", encoding="utf-8")
    run_git(["add", "README.md"], repo)
    run_git(["commit", "-m", "Initial commit"], repo)

    return repo


def test_should_isolate_worker():
    # 비git 작업은 격리 제외
    assert not should_isolate_worker(is_git=False, is_write=True)
    # 읽기 전용 작업은 격리 제외
    assert not should_isolate_worker(is_git=True, is_write=False)
    # 게임 스튜디오 작업(ADR 0058)은 격리 제외
    assert not should_isolate_worker(is_git=True, is_write=True, is_game=True)
    # 검증 작업은 전용 detached 격리이므로 일반 worktree 제외
    assert not should_isolate_worker(is_git=True, is_write=True, is_verification=True)
    # 따까리 관리 git 저장소 쓰기 작업만 격리 대상
    assert should_isolate_worker(is_git=True, is_write=True)


def test_opt_in_write_intent_leaves_review_and_read_only_requests_unisolated():
    assert is_code_change_request("버그 수정", "인증 오류를 고쳐줘")
    assert is_code_change_request("Implement endpoint", "Add the API route and tests")
    assert not is_code_change_request("코드 리뷰", "변경을 읽고 문제만 보고해줘")
    assert not is_code_change_request("Investigate", "Find why this is slow; do not edit files")


def test_delivery_feature_flag_loads_from_settings(monkeypatch):
    monkeypatch.delenv("TTAKKARI_DELIVERY_PIPELINE_ENABLED", raising=False)
    assert Settings(_env_file=None).delivery_pipeline_enabled is False
    monkeypatch.setenv("TTAKKARI_DELIVERY_PIPELINE_ENABLED", "true")
    assert Settings(_env_file=None).delivery_pipeline_enabled is True


def test_validate_writable_roots(tmp_path: Path):
    existing = [tmp_path / "foo", tmp_path / "bar"]
    wt_path = tmp_path / "worktree_session"

    updated = validate_writable_roots(existing, wt_path)
    assert wt_path.resolve() in updated
    assert len(updated) == 3


def test_parallel_workers_do_not_contaminate(temp_git_repo: Path, tmp_path: Path):
    """두 worker가 같은 파일을 동시에 수정해도 서로 오염되지 않는 격리성 검증."""
    wt_root = tmp_path / "worktrees"
    session_a = uuid.uuid4()
    session_b = uuid.uuid4()

    # Session A worktree 생성
    wt_a = create_session_worktree(temp_git_repo, session_a, worktrees_root=wt_root)
    # Session B worktree 생성
    wt_b = create_session_worktree(temp_git_repo, session_b, worktrees_root=wt_root)

    assert wt_a.worktree_path.exists()
    assert wt_b.worktree_path.exists()
    assert wt_a.worktree_path != wt_b.worktree_path

    # Session A에서 shared.py 생성 및 커밋
    file_a = wt_a.worktree_path / "shared.py"
    file_a.write_text("x = 'from worker A'\n", encoding="utf-8")
    run_git(["add", "shared.py"], wt_a.worktree_path)
    run_git(["commit", "-m", "Commit from A"], wt_a.worktree_path)

    # Session B에서는 file_a가 보이지 않아야 함
    file_b = wt_b.worktree_path / "shared.py"
    assert not file_b.exists(), "Session A의 미통합 변경이 Session B에 유출되었습니다."

    # Session B에서 다른 내용으로 shared.py 작성
    file_b.write_text("x = 'from worker B'\n", encoding="utf-8")
    run_git(["add", "shared.py"], wt_b.worktree_path)
    run_git(["commit", "-m", "Commit from B"], wt_b.worktree_path)

    assert file_a.read_text() == "x = 'from worker A'\n"
    assert file_b.read_text() == "x = 'from worker B'\n"


def test_opt_in_worktree_hook_adds_session_provenance(temp_git_repo: Path, tmp_path: Path):
    session_id = uuid.uuid4()
    wt = create_session_worktree(temp_git_repo, session_id, worktrees_root=tmp_path / "worktrees")
    (wt.worktree_path / "hooked.txt").write_text("hooked\n", encoding="utf-8")
    run_git(["add", "hooked.txt"], wt.worktree_path)
    committed = run_git(["commit", "-m", "No manual trailer needed"], wt.worktree_path)
    assert committed.returncode == 0, committed.stderr
    head = run_git(["rev-parse", "HEAD"], wt.worktree_path).stdout.strip()
    assert verify_commit_provenance(temp_git_repo, head, session_id)


def test_dirty_worktree_preservation_on_cleanup(temp_git_repo: Path, tmp_path: Path):
    """미커밋/dirty 변경사항이 남아있는 경우 cleanup 시 삭제되지 않고 보존되는지 검증."""
    wt_root = tmp_path / "worktrees"
    session_id = uuid.uuid4()
    wt = create_session_worktree(temp_git_repo, session_id, worktrees_root=wt_root)

    dirty_file = wt.worktree_path / "uncommitted.txt"
    dirty_file.write_text("dirty content", encoding="utf-8")

    # cleanup 시도 (status=registered, unmerged)
    result = cleanup_session_worktree(wt, ChangeSetStatus.registered, force=False)

    assert not result.removed
    assert result.preserved
    assert "dirty_uncommitted_changes" in result.reason
    assert wt.worktree_path.exists(), "Dirty 파일이 있는 worktree가 삭제되었습니다!"


def test_unmerged_worktree_preservation(temp_git_repo: Path, tmp_path: Path):
    """커밋은 되었으나 아직 merge되지 않은 worktree는 보존되는지 검증."""
    wt_root = tmp_path / "worktrees"
    session_id = uuid.uuid4()
    wt = create_session_worktree(temp_git_repo, session_id, worktrees_root=wt_root)

    new_file = wt.worktree_path / "committed.txt"
    new_file.write_text("committed content", encoding="utf-8")
    run_git(["add", "committed.txt"], wt.worktree_path)
    run_git(["commit", "-m", "Work commit"], wt.worktree_path)

    # status=submitted 일 때 정리 시도
    result = cleanup_session_worktree(wt, ChangeSetStatus.submitted, force=False)

    assert not result.removed
    assert result.preserved
    assert "unmerged_change_set" in result.reason
    assert wt.worktree_path.exists()


def test_safe_cleanup_after_merge(temp_git_repo: Path, tmp_path: Path):
    """통합 완료(merged) 후에는 안전하게 worktree와 브랜치가 제거되는지 검증."""
    wt_root = tmp_path / "worktrees"
    session_id = uuid.uuid4()
    wt = create_session_worktree(temp_git_repo, session_id, worktrees_root=wt_root)

    # merged 상태로 정리 시도
    result = cleanup_session_worktree(wt, ChangeSetStatus.merged, force=False)

    assert result.removed
    assert not result.preserved
    assert not wt.worktree_path.exists()

    # 브랜치도 삭제되었는지 확인
    check_branch = run_git(["show-ref", "--verify", f"refs/heads/{wt.branch_name}"], temp_git_repo)
    assert check_branch.returncode != 0, "브랜치가 삭제되지 않았습니다."


def test_detached_worktree_creation(temp_git_repo: Path, tmp_path: Path):
    """검증용 detached worktree 생성 검증."""
    head_sha = run_git(["rev-parse", "HEAD"], temp_git_repo).stdout.strip()
    target_dir = tmp_path / "detached_eval"

    detached = create_detached_worktree(temp_git_repo, head_sha, target_dir)
    assert detached.exists()
    assert (detached / "README.md").exists()

    # detached HEAD 상태인지 확인
    branch_res = run_git(["branch", "--show-current"], detached)
    assert branch_res.stdout.strip() == "", "Detached 상태가 아닙니다."
