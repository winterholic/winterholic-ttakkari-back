"""B4-EXP-D exploration, read-only (ledger, 26 filler modules). Fixture: reused `L1-repo-explore`.

change class `read_only_report`: the correct end state is the untouched tree plus `report.json`.
The answer key is computed from the AST of the pristine files (not hand written). Spec section 3.1.
"""

import ast
import json

from .. import codes, fixtures
from ..model import Check, CheckOutcome, Ctx, Negative, Stage, Task
from ..negatives import EXIT0, m_add, m_noop, m_replace_in, m_set, neg, presolved_start
from ._common import check_unique, with_paths

REPORT = "report.json"
CONVERTER_NAMES = {"to_cents", "_cents"}
EXPECTED_CONVERTERS = {
    ("ledger/money.py", "to_cents"),
    ("ledger/legacy/fees.py", "_cents"),
}
EXPECTED_CALLERS = {
    ("ledger/tax.py", "tax_cents"),
    ("ledger/invoice.py", "line_cents"),
    ("ledger/legacy/fees.py", "processing_fee"),
}

PROMPT = (
    "ledger 패키지에서 달러 금액을 정수 센트로 바꾸는 함수를 모두 찾고, 그 함수를 호출하는 함수도 모두 찾아라. "
    "센트를 다시 달러 문자열로 바꾸는 함수는 제외한다. 파일은 수정하지 말고 결과만 report.json 에 써라. "
    '형식: {"converters": [{"path": "...", "function": "...", "line": N}], '
    '"callers": [{"path": "...", "function": "...", "line": N}]}. '
    "line 은 함수 정의가 시작되는 줄(1부터)이다."
)


def answer_key(files: dict[str, str]) -> dict[str, dict[tuple[str, str], int]]:
    """Converters and callers with definition lines, computed from the AST of `files`."""
    defs: dict[tuple[str, str], int] = {}
    callers: dict[tuple[str, str], int] = {}
    for path, source in sorted(files.items()):
        if not path.endswith(".py") or path.startswith("tests/"):
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in CONVERTER_NAMES:
                defs[(path, node.name)] = node.lineno
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id in CONVERTER_NAMES
                ):
                    callers[(path, node.name)] = node.lineno
                    break
    return {"converters": defs, "callers": callers}


def _entries(report: dict, field: str):
    return [
        (e.get("path"), e.get("function"), e.get("line")) for e in report.get(field, [])
    ]


def _load(ctx: Ctx) -> tuple[dict | None, CheckOutcome | None]:
    raw = ctx.end_files.get(REPORT)
    if raw is None or not raw.strip():
        return None, CheckOutcome(
            False, "FAIL_EMPTY_RESULT", "report.json missing/empty"
        )
    try:
        report = json.loads(raw)
    except ValueError:
        return None, CheckOutcome(False, "FAIL_HIDDEN", "report.json is not valid JSON")
    if not isinstance(report, dict):
        return None, CheckOutcome(False, "FAIL_HIDDEN", "report.json is not an object")
    return report, None


def _schema_check(ctx: Ctx) -> CheckOutcome:
    report, err = _load(ctx)
    if err:
        return err
    for field in ("converters", "callers"):
        rows = report.get(field)
        if not isinstance(rows, list):
            return CheckOutcome(False, "FAIL_HIDDEN", f"{field} is not a list")
        for row in rows:
            if not (
                isinstance(row, dict)
                and isinstance(row.get("path"), str)
                and isinstance(row.get("function"), str)
                and isinstance(row.get("line"), int)
            ):
                return CheckOutcome(
                    False, "FAIL_HIDDEN", f"bad row in {field}: {row!r}"
                )
    if not report["converters"] and not report["callers"]:
        return CheckOutcome(False, "FAIL_EMPTY_RESULT", "both lists empty")
    return CheckOutcome(True)


def _contains(path: str, function: str, field: str = "converters"):
    def run(ctx: Ctx) -> CheckOutcome:
        report, err = _load(ctx)
        if err:
            return err
        key = answer_key(ctx.base_files)[field]
        want = (path, function, key[(path, function)])
        if want not in _entries(report, field):
            return CheckOutcome(False, "FAIL_HIDDEN", f"missing {want}")
        return CheckOutcome(True)

    return run


def _set_equal(field: str, expected: set[tuple[str, str]]):
    def run(ctx: Ctx) -> CheckOutcome:
        report, err = _load(ctx)
        if err:
            return err
        got = {(p, f) for p, f, _ in _entries(report, field)}
        if got != expected:
            return CheckOutcome(
                False,
                "FAIL_HIDDEN",
                f"{field}: extra={sorted(got - expected)} missing={sorted(expected - got)}",
            )
        return CheckOutcome(True)

    return run


def _citations_resolve(ctx: Ctx) -> CheckOutcome:
    report, err = _load(ctx)
    if err:
        return err
    for field in ("converters", "callers"):
        for path, function, line in _entries(report, field):
            source = ctx.base_files.get(path)
            if source is None:
                return CheckOutcome(
                    False, "FAIL_MISSING_TARGET", f"no such file: {path}"
                )
            hit = any(
                isinstance(n, ast.FunctionDef)
                and n.name == function
                and n.lineno == line
                for n in ast.walk(ast.parse(source))
            )
            if not hit:
                return CheckOutcome(
                    False,
                    "FAIL_MISSING_TARGET",
                    f"{path}:{line} is not the definition of {function}",
                )
    return CheckOutcome(True)


def _read_only(ctx: Ctx) -> CheckOutcome:
    extra = [
        p
        for p in ctx.changed["added"] + ctx.changed["modified"] + ctx.changed["deleted"]
        if p != REPORT
    ]
    if extra:
        return CheckOutcome(False, "FAIL_FORBIDDEN_WRITE", f"changed {extra[:5]}")
    return CheckOutcome(True)


def _explored(ctx: Ctx) -> CheckOutcome:
    """Gating once a trace exists: every cited file was searched/read before the report was written."""
    report, err = _load(ctx)
    if err:
        return err
    if ctx.trace is None:
        return CheckOutcome(None, detail="trace unavailable (2.2-dependent)")
    events = sorted(ctx.trace, key=lambda e: e.get("t", 0))
    write_at = next(
        (
            i
            for i, e in enumerate(events)
            if e.get("kind") == "edit" and e.get("path") == REPORT
        ),
        len(events),
    )
    seen = {
        str(e.get("path"))
        for e in events[:write_at]
        if e.get("kind") in ("read", "search")
    }
    seen_text = " ".join(
        str(e.get("output", "")) for e in events[:write_at] if e.get("kind") == "search"
    )
    cited = {p for f in ("converters", "callers") for p, _, _ in _entries(report, f)}
    unread = sorted(p for p in cited if p not in seen and p not in seen_text)
    if unread:
        return CheckOutcome(
            False, "FAIL_MISSING_TARGET", f"cited without reading/searching: {unread}"
        )
    return CheckOutcome(True)


def _gold_report() -> str:
    key = answer_key(fixtures.files("ledger"))
    body = {
        field: [
            {"path": p, "function": f, "line": line}
            for (p, f), line in sorted(rows.items())
        ]
        for field, rows in key.items()
    }
    return json.dumps(body, indent=2) + "\n"


def _alt_report() -> str:
    key = answer_key(fixtures.files("ledger"))
    body = {
        "callers": [
            {"path": p, "function": f, "line": line}
            for (p, f), line in sorted(key["callers"].items(), reverse=True)
        ],
        "converters": [
            {"path": p, "function": f, "line": line}
            for (p, f), line in sorted(key["converters"].items(), reverse=True)
        ],
        "note": "extra fields are ignored",
    }
    return json.dumps(body) + "\n"


def build() -> Task:
    base = fixtures.files("ledger")
    key = answer_key(base)
    assert set(key["converters"]) == EXPECTED_CONVERTERS, key["converters"]
    assert set(key["callers"]) == EXPECTED_CALLERS, key["callers"]
    checks = [
        Check(
            "H1",
            codes.KIND_DELTA,
            "R1",
            native=_contains("ledger/money.py", "to_cents"),
        ),
        Check(
            "H2",
            codes.KIND_DELTA,
            "R2",
            native=_contains("ledger/legacy/fees.py", "_cents"),
        ),
        Check(
            "H3",
            codes.KIND_DELTA,
            "R3",
            native=_set_equal("converters", EXPECTED_CONVERTERS),
        ),
        Check(
            "H4", codes.KIND_DELTA, "R4", native=_set_equal("callers", EXPECTED_CALLERS)
        ),
        Check(
            "H5",
            codes.KIND_DELTA,
            "R5",
            native=_citations_resolve,
            fail_code="FAIL_MISSING_TARGET",
        ),
        Check("H6", codes.KIND_INVARIANT, "R6", native=_read_only),
        Check("H7", codes.KIND_DELTA, "R7", native=_schema_check),
        Check("H8", codes.KIND_DELTA, "R8", native=_explored),
    ]
    stage = Stage(
        "S1",
        change_class=codes.CHANGE_READ_ONLY,
        prompt=PROMPT,
        checks=checks,
        allowed_writes=(REPORT,),
        result_artifact=REPORT,
    )
    gold_text = _gold_report()

    def report_negative(id_, name, text, expect):
        return neg(id_, name, m_set(REPORT, text), expect)

    def converters_only(label):
        body = json.loads(gold_text)
        label(body)
        return json.dumps(body) + "\n"

    def drop_legacy(b):
        b["converters"] = [c for c in b["converters"] if "fees" not in c["path"]]

    def line_of(path, name):
        return next(
            n.lineno
            for n in ast.walk(ast.parse(base[path]))
            if isinstance(n, ast.FunctionDef) and n.name == name
        )

    def add_decoy(b):
        b["converters"].append(
            {
                "path": "ledger/money.py",
                "function": "from_cents",
                "line": line_of("ledger/money.py", "from_cents"),
            }
        )

    def ghost_file(b):
        b["converters"][0]["path"] = "ledger/moneyx.py"

    def off_by_one(b):
        b["callers"][0]["line"] += 1

    def drop_callers(b):
        b["callers"] = b["callers"][:1]

    def decoy_caller(b):
        b["callers"].append(
            {
                "path": "ledger/audit.py",
                "function": "window_audit",
                "line": line_of("ledger/audit.py", "window_audit"),
            }
        )

    negatives: list[Negative] = [
        neg("N1", "no report written (pristine unchanged)", m_noop, ("FAIL_NOOP",)),
        report_negative(
            "N2",
            "prose keywords only, no structured fields",
            "money.py to_cents and fees.py _cents are the conversion functions\n",
            ("FAIL_HIDDEN",),
        ),
        report_negative("N9", "empty report.json", "", ("FAIL_EMPTY_RESULT",)),
        report_negative(
            "N9",
            "report.json with two empty lists",
            json.dumps({"converters": [], "callers": []}),
            ("FAIL_EMPTY_RESULT",),
        ),
        report_negative(
            "N8",
            "cites a file that does not exist",
            converters_only(ghost_file),
            ("FAIL_MISSING_TARGET",),
        ),
        report_negative(
            "N8",
            "line number off by one",
            converters_only(off_by_one),
            ("FAIL_MISSING_TARGET",),
        ),
        report_negative(
            "N3",
            "legacy converter missed",
            converters_only(drop_legacy),
            ("FAIL_HIDDEN",),
        ),
        report_negative(
            "N3",
            "reverse-direction function listed as converter",
            converters_only(add_decoy),
            ("FAIL_HIDDEN",),
        ),
        report_negative(
            "N3", "callers incomplete", converters_only(drop_callers), ("FAIL_HIDDEN",)
        ),
        report_negative(
            "N3",
            "unrelated function listed as caller",
            converters_only(decoy_caller),
            ("FAIL_HIDDEN",),
        ),
        neg(
            "N10",
            "source file edited while reporting",
            m_replace_in(
                "ledger/money.py", "def from_cents", "def from_cents_x", on_gold=False
            ),
            ("FAIL_FORBIDDEN_WRITE",),
        ),
        neg(
            "N10",
            "git metadata written",
            m_add({".git/hooks/pre-commit": "x\n"}),
            ("FAIL_FORBIDDEN_WRITE",),
        ),
        neg(
            "N7",
            "sitecustomize.py added",
            m_add({"sitecustomize.py": EXIT0}),
            ("FAIL_CHECKER_PATCH",),
        ),
        Negative(
            "N11",
            "pre-solved: the report already exists at the start",
            end=lambda base_, gold: [dict(g) for g in gold],
            expect_verdict="INFRA",
            expect_codes=("INFRA_BASE_MISMATCH",),
            start=presolved_start,
        ),
    ]

    def gold(base_files):
        return [with_paths(base_files, {REPORT: gold_text})]

    def alt(base_files):
        return [with_paths(base_files, {REPORT: _alt_report()})]

    return check_unique(
        Task(
            id="B4-EXP-D",
            type="explore",
            prompt=PROMPT,
            base_files=base,
            stages=[stage],
            gold=gold,
            alt_gold=alt,
            negatives=negatives,
            sources=fixtures.sources("ledger"),
            notes="read_only_report: no diff expected; answer key from AST",
        )
    )
