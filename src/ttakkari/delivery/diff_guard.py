"""Mechanical diff inspection for forbidden changes (ADR 0066 D5).

통합기가 diff를 기계적으로 검사하여 다음 금지 사항을 차단한다:
1. 테스트 함수/케이스 삭제 (deleted_test)
2. pytest skip / xfail 무단 추가 (added_skip_xfail)
3. timeout 값만 상향 (timeout_bump)
4. 예외 삼키기 (swallowed_exception: except: pass, except Exception: pass)
5. 근거/설명 없는 테스트 기대값 변경 (unexplained_assertion_change)
"""

from __future__ import annotations

import ast
import re
from collections import Counter
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
    *,
    test_source_pairs: list[tuple[str, str, str]] | None = None,
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
        elif (
            stripped.startswith("+")
            and "except" in stripped
            and ":" in stripped
            and i + 1 < num_lines
            and re.fullmatch(r"\+\s*pass(?:\s*#.*)?", lines[i + 1].strip())
        ):
            next_line = lines[i + 1].strip()
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

    for file_path, before, after in test_source_pairs or []:
        if not ("test" in file_path.lower() or file_path.endswith("_test.py")):
            continue
        try:
            before_tree = ast.parse(before, filename=f"{file_path}:base") if before else ast.Module(body=[], type_ignores=[])
            after_tree = ast.parse(after, filename=f"{file_path}:head") if after else ast.Module(body=[], type_ignores=[])
        except SyntaxError as exc:
            violations.append(
                DiffViolation("test_source_parse", f"변경 테스트 소스를 AST로 검증하지 못했습니다: {exc.msg}", file_path=file_path)
            )
            continue
        before_tests = Counter(
            node.name
            for node in ast.walk(before_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
        after_tests = Counter(
            node.name
            for node in ast.walk(after_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
        if before_tests - after_tests:
            violations.append(
                DiffViolation("no_test_deletion", f"AST 검사에서 테스트 함수가 삭제되었습니다: {dict(before_tests - after_tests)}", file_path=file_path)
            )
        old_skips = _pytest_skip_nodes(before_tree)
        new_skips = _pytest_skip_nodes(after_tree)
        if new_skips - old_skips:
            violations.append(
                DiffViolation("no_skip_or_xfail", "AST 검사에서 pytest skip/xfail 호출 또는 마커가 추가되었습니다.", file_path=file_path)
            )

    return DiffGuardResult(
        is_valid=len(violations) == 0,
        violations=violations,
    )


def _pytest_skip_nodes(tree: ast.Module) -> Counter[str]:
    pytest_names = {"pytest"}
    mark_names: set[str] = set()
    imported_skip_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    pytest_names.add(alias.asname or "pytest")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            for alias in node.names:
                imported = alias.asname or alias.name
                if alias.name == "mark":
                    mark_names.add(imported)
                if alias.name in {"skip", "skipif", "xfail"}:
                    imported_skip_names.add(imported)

    def names_in_chain(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, ast.Attribute):
            return names_in_chain(node.value)
        if isinstance(node, ast.Call):
            return names_in_chain(node.func)
        return set()

    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        candidate: ast.AST | None = None
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in imported_skip_names:
                candidate = node
            elif isinstance(node.func, ast.Attribute) and node.func.attr in {"skip", "skipif", "xfail"}:
                roots = names_in_chain(node.func)
                if roots & pytest_names or roots & mark_names:
                    candidate = node
        elif isinstance(node, ast.Attribute) and node.attr in {"skip", "skipif", "xfail"}:
            roots = names_in_chain(node)
            if roots & pytest_names or roots & mark_names:
                candidate = node
        if candidate is not None:
            found[ast.dump(candidate, include_attributes=False)] += 1
    return found
