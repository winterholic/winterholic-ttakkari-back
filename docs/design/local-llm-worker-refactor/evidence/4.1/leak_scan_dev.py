"""Model-free static leak scan for the dev fixtures reused in WBS 4.1 (automatic extraction, not exhaustive).

Usage: python3 leak_scan_dev.py <copied bench/2026-10-18 dir> <stage1 dir>

Collects literals (strings of 4+ chars, ints of 3+ digits) from hidden-check code strings and checks
where they also appear: in the model-facing prompt, in non-test workspace sources, or only in public
tests. A literal in the prompt or in non-test source is a leak finding. A literal that also appears in a
public test is reported as `public_test_overlap` (allowed only when the prompt/docstring states the rule).
"""

import ast
import contextlib
import io
import json
import os
import sys

BENCH = os.path.abspath(sys.argv[1])
STAGE1 = os.path.abspath(sys.argv[2])
sys.path.insert(0, BENCH)
sys.path.insert(0, os.path.join(BENCH, "evalbase"))

with contextlib.redirect_stdout(io.StringIO()):
    import tasks

DEV = {
    "B4-SGL-D": "t1-bug-median",
    "B4-MUL-D": "s3-fix-inventory",
    "B4-REF-D": "t2-refactor-dedupe",
    "B4-DBG-D": "t4-long-file",
    "B4-TST-D": "t3-write-tests",
}


def code_strings(func):
    found = []
    stack = [func.__code__]
    while stack:
        code = stack.pop()
        for const in code.co_consts:
            if isinstance(const, str) and len(const) > 20:
                found.append(const)
            elif hasattr(const, "co_consts"):
                stack.append(const)
    return found


def literals(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and len(node.value) >= 4:
                out.add(node.value)
            elif (
                isinstance(node.value, int)
                and not isinstance(node.value, bool)
                and abs(node.value) >= 100
            ):
                out.add(str(node.value))
    return out


def classify(hidden_literals, prompt, files):
    report = {"prompt": [], "nontest_source": [], "public_test_overlap": []}
    for lit in sorted(hidden_literals):
        if lit in prompt:
            report["prompt"].append(lit)
        for rel, content in files.items():
            if lit in content:
                key = (
                    "public_test_overlap"
                    if rel.startswith("tests/")
                    else "nontest_source"
                )
                report[key].append(f"{lit} @ {rel}")
    return report


results = {}
for spec_id, tid in DEV.items():
    task = tasks.TASKS[tid]
    lits = set()
    for func in (task["hidden"], task.get("verifier")):
        if func is None:
            continue
        for text in code_strings(func):
            lits |= literals(text)
    results[spec_id] = {
        "task": tid,
        "n_hidden_literals": len(lits),
        **classify(lits, task["prompt"], task["files"]),
    }

# stage1 shop fixture: literals from HIDDEN_CHECK in verify_fixture.py
with open(os.path.join(STAGE1, "verify_fixture.py")) as fh:
    tree = ast.parse(fh.read())
hidden_src = ""
for node in tree.body:
    if (
        isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", "") == "HIDDEN_CHECK"
    ):
        hidden_src = node.value.value
lits = literals(hidden_src)
files = {}
template = os.path.join(STAGE1, "fixture-template")
for root, dirs, names in os.walk(template):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
    for name in names:
        path = os.path.join(root, name)
        with open(path) as fh:
            files[os.path.relpath(path, template)] = fh.read()
with open(os.path.join(STAGE1, "task.txt")) as fh:
    prompt = fh.read()
results["B4-REC-D"] = {
    "task": "stage1-shop",
    "n_hidden_literals": len(lits),
    **classify(lits, prompt, files),
}

print(json.dumps(results, ensure_ascii=False, indent=1))
