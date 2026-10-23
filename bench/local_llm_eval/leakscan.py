"""Static answer-leak scan for a task (spec 4.1 section 5.4-3, extended in 4.2).

Two directions, both lexical (a lexical scan cannot prove there is no leak):
1. hidden-only literals: strings (4+ chars) and ints (100+) found in the judge functions (expected values) that appear in
   the model-facing prompt or in non-test workspace sources;
2. gold-only tokens: strings/identifiers/ints introduced by the reference solution (gold vs pristine) that
   appear in the prompt or in the pristine workspace.
A token the prompt itself states is spec-derived (spec 4.1 principle 4) and only counted. A token that is
NOT in the prompt but sits in a non-test workspace source is a finding unless it is generic (language,
stdlib, WSGI vocabulary: GENERIC) or allowlisted per task with a reason. Native checks are not scanned.
"""

import ast
import inspect
import re
import textwrap
from dataclasses import dataclass, field

from .model import Task

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{5,}")

# Vocabulary of the language / standard library / WSGI that any workspace source may contain.
GENERIC = frozenset(
    {
        "import",
        "return",
        "replace",
        "rstrip",
        "ValueError",
        "OSError",
        "unittest",
        "status",
        "headers",
        "total",
        "rows",
        "utf-8",
        "PATH_INFO",
        "REQUEST_METHOD",
        "test",
        "tests",
        "body",
        "code",
        "error",
        "field",
        "items",
        "limit",
        "offset",
        "notes",
        "title",
        "amount",
        "invoice",
        "common",
        "pricing",
    }
)

EXPLORATION_REASON = (
    "exploration target names exist in the workspace by construction; the answer is the selection and the "
    "definition lines, not a token"
)
TASK_ALLOW: dict[str, dict[str, str]] = {
    "B4-EXP-D": {
        name: EXPLORATION_REASON
        for name in ("_cents", "line_cents", "processing_fee", "tax_cents", "to_cents")
    },
}


@dataclass
class ScanResult:
    task_id: str
    hidden_literals: int = 0
    gold_tokens: int = 0
    findings: list[dict] = field(default_factory=list)
    allowed: list[dict] = field(default_factory=list)
    spec_stated: int = 0


def _label_ids(tree: ast.AST) -> set[int]:
    """Constants that name observation keys or label an assertion message (not expected values)."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("val", "raised", "unittest_ok"):
                skip |= {id(a) for a in node.args if isinstance(a, ast.Constant)}
            elif node.func.id == "same" and len(node.args) >= 3:
                skip.add(id(node.args[2]))
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            skip.add(id(node.slice))
        elif isinstance(node, ast.JoinedStr):
            skip |= {id(v) for v in node.values if isinstance(v, ast.Constant)}
        elif isinstance(node, ast.Assert) and isinstance(node.msg, ast.Constant):
            skip.add(id(node.msg))
    return skip


def _literals(source: str, judge: bool = False) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    skip = _label_ids(tree) if judge else set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in skip:
            value = node.value
            if isinstance(value, str) and len(value) >= 4:
                out.add(value)
            elif (
                isinstance(value, int)
                and not isinstance(value, bool)
                and abs(value) >= 100
            ):
                out.add(str(value))
    return out


def _tokens(text: str) -> set[str]:
    out = set(IDENT.findall(text))
    for lit in _literals(text):
        out.add(lit)
    return out


def hidden_literals(task: Task) -> set[str]:
    out: set[str] = set()
    for _, check in task.stage_checks():
        if check.judge is not None:
            try:
                out |= _literals(
                    textwrap.dedent(inspect.getsource(check.judge)), judge=True
                )
            except (OSError, TypeError):
                continue
    return out


def gold_tokens(task: Task) -> set[str]:
    """Tokens that appear in gold files but not in the pristine file of the same path."""
    out: set[str] = set()
    for end in task.gold(task.base_files):
        for path, content in end.items():
            before = _tokens(task.base_files.get(path, ""))
            out |= _tokens(content) - before
    return out


def scan(task: Task, allow: dict[str, str] | None = None) -> ScanResult:
    allow = allow if allow is not None else TASK_ALLOW.get(task.id, {})
    prompts = "\n".join([task.prompt] + [s.prompt for s in task.stages])
    sources = {p: c for p, c in task.base_files.items() if not p.startswith("tests/")}
    result = ScanResult(task.id)
    hidden = hidden_literals(task)
    gold = gold_tokens(task)
    result.hidden_literals, result.gold_tokens = len(hidden), len(gold)
    for direction, tokens in (("hidden_literal", hidden), ("gold_token", gold)):
        for token in sorted(tokens):
            if token in prompts:
                result.spec_stated += 1
                continue
            where = [f"source:{p}" for p, c in sources.items() if token in c]
            if not where:
                continue
            row = {"direction": direction, "token": token, "where": where}
            if token in GENERIC:
                row["reason"] = "generic language/stdlib/WSGI vocabulary"
                result.allowed.append(row)
            elif token in allow:
                row["reason"] = allow[token]
                result.allowed.append(row)
            else:
                result.findings.append(row)
    return result
