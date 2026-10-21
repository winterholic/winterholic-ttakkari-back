"""Bounded repair loop policy and failure signature tracking (ADR 0066 D5).

자동 수정 루프 상한:
- 같은 실패 서명(테스트 id + 예외 유형)당 최대 2회 (MAX_REPAIRS_PER_SIGNATURE = 2)
- ChangeSet당 총 수리 시도 최대 3회 (MAX_REPAIRS_PER_CHANGE = 3)
상한 초과 시 큐에서 격리(rejected)하고 사용자/상위 세션에 알린다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ttakkari.delivery_models import ChangeSet

MAX_REPAIRS_PER_SIGNATURE = 2
MAX_REPAIRS_PER_CHANGE = 3


@dataclass(frozen=True)
class RepairBudgetCheck:
    allowed: bool
    reason: str | None
    signature: str
    change_repair_count: int
    signature_repair_count: int


def extract_failure_signature(
    test_id: str,
    exception_type: str,
    message: str = "",
) -> str:
    """테스트 ID와 예외 유형으로부터 정규화된 실패 서명 추출.

    예: "tests/test_foo.py::test_bar::AssertionError"
    """
    clean_test = test_id.strip()
    clean_exc = exception_type.strip()
    return f"{clean_test}::{clean_exc}"


def check_repair_budget(
    change_set: ChangeSet,
    new_signature: str,
) -> RepairBudgetCheck:
    """현재 ChangeSet의 수리 시도 횟수를 평가하여 추가 수정 가능 여부 판단."""
    current_total = change_set.repair_attempts
    signature_counts = change_set.signature_attempts or {}
    legacy_count = (
        change_set.signature_repair_attempts
        if change_set.failure_signature == new_signature
        else 0
    )
    current_sig_count = int(signature_counts.get(new_signature, legacy_count))
    next_sig_count = current_sig_count + 1

    next_total = current_total + 1

    # 1. ChangeSet 전체 수리 한도 검사
    if next_total > MAX_REPAIRS_PER_CHANGE:
        return RepairBudgetCheck(
            allowed=False,
            reason=f"ChangeSet 총 자율 수정 상한({MAX_REPAIRS_PER_CHANGE}회)을 초과했습니다.",
            signature=new_signature,
            change_repair_count=current_total,
            signature_repair_count=current_sig_count,
        )

    # 2. 동일 서명 수리 한도 검사
    if next_sig_count > MAX_REPAIRS_PER_SIGNATURE:
        return RepairBudgetCheck(
            allowed=False,
            reason=(
                f"동일 실패 서명('{new_signature}')에 대한 자율 수정 상한"
                f"({MAX_REPAIRS_PER_SIGNATURE}회)을 초과했습니다."
            ),
            signature=new_signature,
            change_repair_count=current_total,
            signature_repair_count=current_sig_count,
        )

    return RepairBudgetCheck(
        allowed=True,
        reason=None,
        signature=new_signature,
        change_repair_count=next_total,
        signature_repair_count=next_sig_count,
    )


def record_repair_attempt(
    change_set: ChangeSet,
    signature: str,
) -> None:
    """수리 시도 카운터와 실패 서명 갱신."""
    signature_counts = dict(change_set.signature_attempts or {})
    signature_counts[signature] = int(signature_counts.get(signature, 0)) + 1
    change_set.signature_attempts = signature_counts
    change_set.failure_signature = signature
    change_set.signature_repair_attempts = signature_counts[signature]

    change_set.repair_attempts += 1


def format_repair_prompt(
    change_set: ChangeSet,
    signature: str,
    failure_log: str,
    diff_violations: list[str] | None = None,
) -> str:
    """원 세션 또는 후속 작업 세션에 전달할 구조화된 수정 안내 메시지 생성."""
    lines = [
        f"[자동 검증 실패 피드백 - ChangeSet {change_set.id}]",
        f"실패 서명: {signature}",
        f"수정 시도 횟수: {change_set.repair_attempts}/{MAX_REPAIRS_PER_CHANGE}",
    ]

    if diff_violations:
        lines.append("\n[금지된 변경 사항 위반 목록]")
        for v in diff_violations:
            lines.append(f"- {v}")

    if failure_log:
        lines.append(f"\n[검증 실패 로그]\n{failure_log.strip()[:2000]}")

    lines.append(
        "\n주의사항: 테스트 삭제, skip/xfail 추가, timeout만 상향, 설명 없는 기대값 변경은 "
        "기계적으로 거부됩니다. 원인을 정확히 분석하여 수정 후 재제출하세요."
    )
    return "\n".join(lines)
