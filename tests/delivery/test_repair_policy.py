"""Unit tests for bounded repair loop policy (ADR 0066 D5)."""

from __future__ import annotations

import uuid

from ttakkari.delivery.repair_policy import (
    MAX_REPAIRS_PER_SIGNATURE,
    check_repair_budget,
    extract_failure_signature,
    format_repair_prompt,
    record_repair_attempt,
)
from ttakkari.delivery_models import ChangeSet


def test_extract_failure_signature():
    sig = extract_failure_signature(
        "tests/test_auth.py::test_login", "AssertionError", "Expected 200 got 401"
    )
    assert sig == "tests/test_auth.py::test_login::AssertionError"


def test_signature_repair_budget_limit():
    cs = ChangeSet(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        repo_path="/repo",
        branch_name="work/1",
        worktree_path="/wt",
        base_sha="abc",
        repair_attempts=0,
        signature_repair_attempts=0,
        failure_signature=None,
    )

    sig1 = "tests/test_route.py::test_inventory::KeyError"

    # 1회차 수리 허용
    check1 = check_repair_budget(cs, sig1)
    assert check1.allowed
    record_repair_attempt(cs, sig1)
    assert cs.signature_repair_attempts == 1
    assert cs.repair_attempts == 1

    # 2회차 동일 서명 수리 허용
    check2 = check_repair_budget(cs, sig1)
    assert check2.allowed
    record_repair_attempt(cs, sig1)
    assert cs.signature_repair_attempts == 2
    assert cs.repair_attempts == 2

    # 3회차 동일 서명 수리 거부 (상한 2회 초과)
    check3 = check_repair_budget(cs, sig1)
    assert not check3.allowed
    assert f"동일 실패 서명('{sig1}')에 대한 자율 수정 상한(2회)을 초과" in check3.reason


def test_total_change_repair_budget_limit():
    cs = ChangeSet(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        repo_path="/repo",
        branch_name="work/1",
        worktree_path="/wt",
        base_sha="abc",
        repair_attempts=0,
        signature_repair_attempts=0,
        failure_signature=None,
    )

    sig1 = "test_1::Err1"
    sig2 = "test_2::Err2"
    sig3 = "test_3::Err3"
    sig4 = "test_4::Err4"

    # 서로 다른 서명으로 1회씩 3회 시도
    record_repair_attempt(cs, sig1)
    record_repair_attempt(cs, sig2)
    record_repair_attempt(cs, sig3)
    assert cs.repair_attempts == 3

    # 4회차 시도 거부 (총 한도 3회 초과)
    check = check_repair_budget(cs, sig4)
    assert not check.allowed
    assert "ChangeSet 총 자율 수정 상한(3회)을 초과" in check.reason


def test_signature_limit_does_not_reset_when_another_failure_intervenes():
    cs = ChangeSet(
        id=uuid.uuid4(), session_id=uuid.uuid4(), repo_path="/repo", branch_name="work/1",
        worktree_path="/wt", base_sha="abc", repair_attempts=0,
        signature_repair_attempts=0, failure_signature=None,
    )
    first = "test_a::AssertionError"
    other = "test_b::RuntimeError"
    for signature in (first, first, other):
        assert check_repair_budget(cs, signature).allowed
        record_repair_attempt(cs, signature)
    denied = check_repair_budget(cs, first)
    assert not denied.allowed
    assert denied.signature_repair_count == MAX_REPAIRS_PER_SIGNATURE


def test_legacy_signature_counter_survives_migration_to_signature_map():
    signature = "test_legacy::AssertionError"
    cs = ChangeSet(
        id=uuid.uuid4(), session_id=uuid.uuid4(), repo_path="/repo", branch_name="work/1",
        worktree_path="/wt", base_sha="abc", repair_attempts=2,
        signature_repair_attempts=2, failure_signature=signature, signature_attempts={},
    )
    assert not check_repair_budget(cs, signature).allowed


def test_format_repair_prompt():
    cs = ChangeSet(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        repo_path="/repo",
        branch_name="work/1",
        worktree_path="/wt",
        base_sha="abc",
        repair_attempts=1,
    )

    prompt = format_repair_prompt(
        cs,
        signature="test_db::ConnectionError",
        failure_log="Timeout connecting to 5432",
        diff_violations=["no_skip_or_xfail"],
    )

    assert "test_db::ConnectionError" in prompt
    assert "Timeout connecting to 5432" in prompt
    assert "no_skip_or_xfail" in prompt
    assert "테스트 삭제, skip/xfail 추가, timeout만 상향" in prompt
