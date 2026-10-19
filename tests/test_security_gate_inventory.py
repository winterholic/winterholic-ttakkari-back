"""SEC2-GATE-01: 보안 회귀 테스트 묶음 인벤토리 검증.

보안 테스트 묶음(SECURITY_TEST_FILES)의 완전성과 marker 일치를 상시 검증하여,
새 보안 테스트가 추가되었을 때 게이트 묶음에서 누락되는 것을 방지한다.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ttakkari.security.gate import SECURITY_TEST_FILES

pytestmark = [pytest.mark.security]

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_all_security_files_exist_and_non_empty():
    for rel_path in SECURITY_TEST_FILES:
        file_path = REPO_ROOT / rel_path
        assert file_path.is_file(), f"보안 테스트 파일 부재: {rel_path}"
        assert file_path.stat().st_size > 0, f"보안 테스트 파일 빈 파일: {rel_path}"


def test_all_security_files_declare_security_mark():
    for rel_path in SECURITY_TEST_FILES:
        file_path = REPO_ROOT / rel_path
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))

        has_marker = False
        for node in tree.body:
            # 1) pytestmark = [pytest.mark.security] or pytestmark = pytest.mark.security
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "pytestmark":
                        # Check value contains 'security'
                        value_str = ast.unparse(node.value)
                        if "security" in value_str:
                            has_marker = True
            # 2) function decorator @pytest.mark.security
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    deco_str = ast.unparse(deco)
                    if "pytest.mark.security" in deco_str or "mark.security" in deco_str:
                        has_marker = True

        assert has_marker, f"{rel_path} 에 @pytest.mark.security 또는 pytestmark = [pytest.mark.security] 가 없습니다."


def test_no_security_tests_omitted_from_inventory():
    """보안 핵심 식별자(SEC2-, authz03, login_throttle 등)를 가진 파일이 인벤토리에서 누락되지 않았는지 검사."""
    security_patterns = (
        "authz03",
        "login_throttle",
        "agent_token_security",
        "agent_scope_auth",
        "operational_settings",
        "backup_restore",
        "backup_ops",
        "backup_status",
        "claude_hook_fail_closed",
        "muse_guard",
        "csrf_defense",
        "game_serving_security",
        "password_session_revocation",
        "runs_events_masking",
        "oplog_scrub",
        "antigravity_account_isolation",
        "antigravity_managed_profiles",
        "github_hardening",
    )

    all_test_files: list[Path] = []
    for directory in (REPO_ROOT / "tests", REPO_ROOT / "unit_tests"):
        if directory.exists():
            all_test_files.extend(directory.glob("test_*.py"))

    missing: list[str] = []
    for test_path in all_test_files:
        rel = test_path.relative_to(REPO_ROOT).as_posix()
        name = test_path.name
        if any(pat in name for pat in security_patterns) and rel not in SECURITY_TEST_FILES:
            missing.append(rel)

    assert not missing, f"보안 테스트가 SECURITY_TEST_FILES 에 등록되지 않았습니다: {missing}"
