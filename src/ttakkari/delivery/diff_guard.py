"""Mechanical diff inspection for forbidden changes (ADR 0066 D5).

통합기가 diff를 기계적으로 검사하여 다음 금지 사항을 차단한다:
1. 테스트 함수/케이스 삭제 (deleted_test)
2. pytest skip / xfail 무단 추가 (added_skip_xfail)
3. timeout 값만 상향 (timeout_bump)
4. 예외 삼키기 (swallowed_exception: except: pass, except Exception: pass)
5. 근거/설명 없는 테스트 기대값 변경 (unexplained_assertion_change)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DiffViolation:
    rule: str
    detail: str
    line: str | None = None
    file_path: str | None = None


@dataclass(frozen=True)
class DiffGuardResult:
    is_valid: bool
    violations: list[DiffViolation] = field(default_factory=list)


# 정규식 패턴들
RE_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
RE_DELETED_TEST = re.compile(r"^-\s*def (test_[a-zA-Z0-9_]+)\(")
RE_ADDED_SKIP = re.compile(r"^\+\s*@pytest\.mark\.(skip|xfail)")
RE_ADDED_SKIP_CALL = re.compile(r"^\+\s*pytest\.(skip|xfail)\(")
RE_SWALLOWED_EXC = re.compile(
    r"^\+\s*except\s*([A-Za-z0-9_.]*)\s*:\s*(?:#.*)?\n\+\s*pass\b"
)
RE_SWALLOWED_INLINE = re.compile(r"^\+\s*except.*:\s*pass\b")
RE_TIMEOUT_ONLY = re.compile(r"^\+\s*@pytest\.mark\.timeout\(")
RE_ASSERT_CHANGED = re.compile(r"^[-+]\s*assert\b")


def check_forbidden_diff(
    diff_text: str,
    commit_message: str = "",
) -> DiffGuardResult:
    """diff_text를 기계적으로 분석하여 ADR 0066 D5 위반 사항을 검출."""
    violations: list[DiffViolation] = []
    current_file: str | None = None

    lines = diff_text.splitlines(keepends=True)
    i = 0
    num_lines = len(lines)

    has_explanation = bool(commit_message and len(commit_message.strip()) > 15)
    assert_modified_in_file: dict[str, int] = {}

    while i < num_lines:
        line = lines[i]
        stripped = line.rstrip("\r\n")

        # 파일 경로 추적
        header_match = RE_DIFF_HEADER.match(stripped)
        if header_match:
            current_file = header_match.group(2)
            i += 1
            continue

        is_test_file = current_file and (
            "test" in current_file.lower() or current_file.endswith("_test.py")
        )

        # 1. 테스트 함수 삭제 검출
        if is_test_file:
            del_match = RE_DELETED_TEST.match(stripped)
            if del_match:
                test_name = del_match.group(1)
                violations.append(
                    DiffViolation(
                        rule="no_test_deletion",
                        detail=f"테스트 함수 '{test_name}' 삭제가 금지되어 있습니다.",
                        line=stripped,
                        file_path=current_file,
                    )
                )

        # 2. 무단 skip / xfail 추가 검출
        if RE_ADDED_SKIP.match(stripped) or RE_ADDED_SKIP_CALL.match(stripped):
            violations.append(
                DiffViolation(
                    rule="no_skip_or_xfail",
                    detail="임의로 pytest skip 또는 xfail 마커/호출을 추가하는 것이 금지되어 있습니다.",
                    line=stripped,
                    file_path=current_file,
                )
            )

        # 3. timeout 상향 검출
        if RE_TIMEOUT_ONLY.match(stripped):
            violations.append(
                DiffViolation(
                    rule="no_timeout_bump",
                    detail="테스트 timeout 마커를 임의로 추가하거나 상향하는 것이 금지되어 있습니다.",
                    line=stripped,
                    file_path=current_file,
                )
            )

        # 4. 예외 삼키기 (인라인 또는 2줄 pass)
        if RE_SWALLOWED_INLINE.match(stripped):
            violations.append(
                DiffViolation(
                    rule="no_swallowed_exceptions",
                    detail="except ...: pass 로 예외를 무단으로 삼키는 것이 금지되어 있습니다.",
                    line=stripped,
                    file_path=current_file,
                )
            )
        elif stripped.startswith("+") and "except" in stripped and ":" in stripped:
            # 다음 라인이 + pass 인지 검사
            if i + 1 < num_lines:
                next_line = lines[i + 1].strip()
                if next_line in ("+ pass", "+pass"):
                    violations.append(
                        DiffViolation(
                            rule="no_swallowed_exceptions",
                            detail="except ...: pass 블록으로 예외를 무단으로 삼키는 것이 금지되어 있습니다.",
                            line=stripped + " " + next_line,
                            file_path=current_file,
                        )
                    )

        # 5. assert 변경 추적
        if is_test_file and RE_ASSERT_CHANGED.match(stripped):
            assert_modified_in_file[current_file] = (
                assert_modified_in_file.get(current_file, 0) + 1
            )

        i += 1

    # 설명 없는 테스트 기대값 변경 검출
    if assert_modified_in_file and not has_explanation:
        for fpath, cnt in assert_modified_in_file.items():
            violations.append(
                DiffViolation(
                    rule="no_unexplained_assertion_change",
                    detail=(
                        f"파일 '{fpath}'에서 {cnt}개의 assert 기대값이 변경되었으나, "
                        "커밋 메시지에 동작 변경에 대한 설명이 없습니다."
                    ),
                    file_path=fpath,
                )
            )

    return DiffGuardResult(
        is_valid=len(violations) == 0,
        violations=violations,
    )
