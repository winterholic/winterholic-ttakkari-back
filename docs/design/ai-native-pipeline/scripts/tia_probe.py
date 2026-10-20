"""정적 import 그래프 기반 영향 테스트 선택(TIA)의 선택률과 과거 실패 재현율을 잰다.

사용: python tia_probe.py <repo_root> [<commit> <failing_test_file> ...]
- 테스트 모듈별로 ttakkari 모듈 전이 폐포를 구한다(conftest 경유 포함/제외 두 가지).
- 커밋을 주면 그 커밋의 변경 파일로 선택되는 테스트 수와, 실제 실패 테스트가 선택되는지(놓침 여부)를 보고한다.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path


def module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root / "src").with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports(path: Path, pkg: str) -> set[str]:
    out = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.startswith("ttakkari")}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.rsplit(".", node.level - 1)[0] if node.level > 1 else pkg
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            if mod.startswith("ttakkari"):
                out.add(mod)
                out |= {f"{mod}.{a.name}" for a in node.names}
    return out


def build(root: Path):
    src = {module_name(root, p): p for p in (root / "src").rglob("*.py")}
    graph = {}
    for name, path in src.items():
        pkg = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        graph[name] = {m for m in imports(path, pkg) if m in src}
    return src, graph


def closure(starts: set[str], graph: dict[str, set[str]]) -> set[str]:
    seen, stack = set(), list(starts)
    while stack:
        m = stack.pop()
        if m in seen or m not in graph:
            continue
        seen.add(m)
        # 부모 패키지 __init__ 도 import 시 실행된다.
        parts = m.split(".")
        stack.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        stack.extend(graph[m])
    return seen


def main(root: Path, pairs: list[str]) -> dict:
    src, graph = build(root)
    tests = sorted(p for d in ("tests", "unit_tests") for p in (root / d).glob("test_*.py"))
    conftest = closure(imports(root / "tests/conftest.py", "tests"), graph)
    per_test = {}
    for t in tests:
        own = closure(imports(t, t.parent.name), graph)
        with_conf = own | conftest if t.parent.name == "tests" else own
        per_test[str(t.relative_to(root))] = (own, with_conf)
    rev = {}
    for name in src:
        rev[name] = (sum(name in o for o, _ in per_test.values()), sum(name in w for _, w in per_test.values()))
    report = {
        "src_modules": len(src), "test_modules": len(tests),
        "conftest_closure_modules": len(conftest),
        "median_tests_selected_per_src_module_own_imports": sorted(a for a, _ in rev.values())[len(rev) // 2],
        "median_tests_selected_per_src_module_with_conftest": sorted(b for _, b in rev.values())[len(rev) // 2],
        "history": [],
    }
    for i in range(0, len(pairs), 2):
        commit, failing = pairs[i], pairs[i + 1]
        files = subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
                                        cwd=root, text=True).split()
        changed = {module_name(root, root / f) for f in files if f.startswith("src/") and f.endswith(".py")}
        changed_tests = {f for f in files if f.startswith(("tests/", "unit_tests/"))}
        sel_own = {t for t, (o, _) in per_test.items() if o & changed} | changed_tests
        sel_conf = {t for t, (_, w) in per_test.items() if w & changed} | changed_tests
        report["history"].append({"commit": commit, "changed_src": sorted(changed), "failing": failing,
                                  "own_imports": {"selected": len(sel_own), "caught": failing in sel_own},
                                  "with_conftest": {"selected": len(sel_conf), "caught": failing in sel_conf}})
    return report


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1]).resolve(), sys.argv[2:]), ensure_ascii=False, indent=1))
