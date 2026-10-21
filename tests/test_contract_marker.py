"""ADR 0066 D3a: 계약형 레지스트리 일관성 묶음(contract 마커) 인벤토리 검증.

도구 카탈로그·라우트/MCP 분류표·세대 계약↔마이그레이션·보안 게이트 목록 검사를
pytest 마커(contract)로 묶고, 마크가 빠지거나 인벤토리에서 빠지면 실패한다.
손 목록 스크립트는 마크를 못 따라가서 같은 SHA에서 거짓 통과한 적이 있다(LOG-145).
검사 관례는 tests/test_security_gate_inventory.py 와 같다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[1]

CONTRACT_TEST_FILES = (
    "tests/test_context.py",
    "tests/test_contract_marker.py",
    "tests/test_generation_deployment.py",
    "tests/test_migration_single_head.py",
    "tests/test_route_inventory.py",
    "tests/test_security_gate.py",
    "tests/test_security_gate_inventory.py",
)


def _has_contract_mark(tree: ast.Module) -> bool:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
            )
            and "contract" in ast.unparse(node.value)
        ):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            "mark.contract" in ast.unparse(d) for d in node.decorator_list
        ):
            return True
    return False


def test_all_contract_files_exist_and_non_empty():
    for rel_path in CONTRACT_TEST_FILES:
        file_path = REPO_ROOT / rel_path
        assert file_path.is_file(), f"계약 테스트 파일 부재: {rel_path}"
        assert file_path.stat().st_size > 0, f"계약 테스트 파일 빈 파일: {rel_path}"


def test_all_contract_files_declare_contract_mark():
    for rel_path in CONTRACT_TEST_FILES:
        file_path = REPO_ROOT / rel_path
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        assert _has_contract_mark(tree), f"{rel_path} 에 contract 마크가 없습니다."


def test_no_contract_tests_omitted_from_inventory():
    """계약 파일명 줄기(inventory, generation_deployment 등)를 가진 파일이 인벤토리에서 빠지면 실패한다."""
    contract_patterns = (
        "route_inventory",
        "generation_deployment",
        "security_gate",
        "migration_single_head",
        "contract_marker",
        "test_context.py",
    )
    missing: list[str] = []
    for test_path in (REPO_ROOT / "tests").glob("test_*.py"):
        rel = test_path.relative_to(REPO_ROOT).as_posix()
        if (
            any(pat in test_path.name for pat in contract_patterns)
            and rel not in CONTRACT_TEST_FILES
        ):
            missing.append(rel)
        tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
        if _has_contract_mark(tree) and rel not in CONTRACT_TEST_FILES:
            missing.append(rel)
    assert not missing, (
        f"계약 테스트가 CONTRACT_TEST_FILES 에 등록되지 않았습니다: {sorted(set(missing))}"
    )
