"""B4-LNG-D long multi-turn task (composite-A). Fixtures reused: median + slugify + billing + ledger fillers.

Five scripted user turns on one workspace; each turn is its own stage judged on the cumulative state.
Persistent constraint C1 (tests/test_stats.py never edited) is stated in T1 and checked through T5.
T5 is read-only: the correct end state is no change plus a report that matches the cumulative diff.
Resume/cancel checks (H13, H14) are interface only until WBS 2.2 settles the harness. Spec 3.9.
"""

import ast
import re
from pathlib import Path

from .. import codes, fixtures
from ..judges import raised, same, val
from ..manifest import diff_manifests, manifest_of_files
from ..model import Attempt, Check, CheckOutcome, Ctx, Stage, Task
from ..natives import (
    SuiteSpec,
    frozen_unchanged,
    public_suite,
    suite_collected,
    suite_gold_pass,
    suite_kills,
    suite_quality,
)
from ..negatives import (
    m_comment,
    m_delete,
    m_misplaced,
    m_revert,
    m_set,
    n15_negatives,
    neg,
    presolved_negative,
    standard_negatives,
)
from ._common import check_unique, with_paths
from .sgl import GOOD as MEDIAN_GOOD

SLUG_GOLD = '''import re


def slugify(text, max_len=None):
    """Lowercase, replace runs of non-alphanumerics with '-', strip '-' at both ends."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if max_len is not None:
        s = s[:max_len].rstrip("-")
    return s
'''
SLUG_ALT = SLUG_GOLD.replace(
    '    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")\n    if max_len is not None:\n        s = s[:max_len].rstrip("-")\n    return s\n',
    '    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")\n    if max_len is None:\n        return out\n    return out[:max_len].rstrip("-")\n',
)
CLI_GOLD = '''from .slug import slugify


def main(argv):
    """argv: [text] -> slug string."""
    if not argv:
        raise SystemExit("usage: slugify TEXT [--max-len N]")
    max_len = None
    if "--max-len" in argv:
        index = argv.index("--max-len")
        max_len = int(argv[index + 1])
    return slugify(argv[0], max_len=max_len)
'''
NEW_SLUG_TESTS = """
    def test_max_len(self):
        self.assertEqual(slugify("Hello, World!", max_len=6), "hello")
        self.assertEqual(slugify("Hello, World!", max_len=100), "hello-world")

    def test_max_len_cli(self):
        self.assertEqual(main(["Hello World", "--max-len", "5"]), "hello")
"""
ALT_SLUG_TESTS = """
    def test_max_len_truncates_without_trailing_dash(self):
        for limit, want in ((5, "hello"), (6, "hello"), (7, "hello-w")):
            with self.subTest(limit=limit):
                self.assertEqual(slugify("Hello, World!", max_len=limit), want)

    def test_cli_option(self):
        self.assertEqual(main(["Hello World", "--max-len", "5"]), "hello")
        self.assertEqual(main(["Hello World"]), "hello-world")
"""
LABEL_FUNC = '''

def invoice_label(amount, text, max_len):
    """Label '<slug>:<total>' where total = amount + tax."""
    return f"{slugify(text, max_len=max_len)}:{amount + compute_tax(amount)}"
'''

RENAME_PATHS = (
    "billing/__init__.py",
    "billing/tax.py",
    "billing/invoice.py",
    "tests/test_billing.py",
)

PROMPTS = {
    "T1": (
        "stats.py 의 median 이 짝수 개일 때와 빈 리스트일 때 틀린다. 짝수 개면 가운데 두 값의 평균을, 빈 리스트면 "
        "ValueError 를 내도록 고치고 run_tests 로 확인하라. 중요: tests/test_stats.py 는 지금부터 마지막 턴까지 "
        "어떤 턴에서도 수정하지 마라."
    ),
    "T2": fixtures.load("slugify")["original_prompt"],
    "T3": fixtures.load("billing")["original_prompt"],
    "T4": (
        "billing/invoice.py 에 invoice_label(amount, text, max_len) 을 추가하라. text 는 textutil 의 "
        "slugify(text, max_len=max_len) 로 slug 로 만들고, 세금은 billing 의 compute_tax(amount) 로 계산해 "
        'f"{slug}:{amount + tax}" 형식의 문자열을 돌려준다. 앞선 작업에서 바꾼 이름과 기능을 그대로 써라.'
    ),
    "T5": (
        "지금까지 한 모든 변경을 파일별로 보고하라. 파일은 수정하지 마라. 보고의 files 항목에 "
        '{"path": "...", "change": "added|modified|deleted"} 를 처음 주어진 상태 대비로 모두 나열한다.'
    ),
}

H1_PROBE = """
from stats import median
rec('a', attempt(median, [10, 20]))
rec('b', attempt(median, [4, 1, 3, 2]))
rec('c', attempt(median, [1, 2, 3, 4, 5, 6]))
rec('d', attempt(median, [-3, -1]))
rec('e', attempt(median, []))
"""
H3_PROBE = """
from textutil import slugify
rec('a', attempt(slugify, 'Hello, World!', max_len=7))
rec('b', attempt(slugify, 'Hello, World!', max_len=6))
rec('c', attempt(slugify, 'Hello, World!', max_len=100))
rec('d', attempt(slugify, 'Hello, World!'))
"""
H4_PROBE = """
from textutil.cli import main
rec('a', attempt(main, ['Hello World', '--max-len', '5']))
rec('b', attempt(main, ['Hello World']))
"""
H7_PROBE = """
from billing import compute_tax, make_invoice
rec('a', attempt(compute_tax, 1000))
rec('b', attempt(compute_tax, 1234, 0.25))
rec('c', attempt(make_invoice, 500))
"""
H8_PROBE = """
from billing import tax
import sys
import textutil
from textutil import slug
tax.compute_tax = lambda amount, rate=0.1: 12345
slug.slugify = lambda text, max_len=None: "SLUG"
textutil.slugify = slug.slugify
# billing.__init__ imports invoice eagerly; re-import its sealed source after
# trusted dependency replacement so both direct-import and module-import forms work.
sys.modules.pop('billing.invoice', None)
from billing.invoice import invoice_label
rec('label', attempt(invoice_label, 1000, 'Hello World', 5))
"""
H9_PROBE = """
from billing.invoice import invoice_label
rec('a', attempt(invoice_label, 2000, 'Hello World', 5))
rec('b', attempt(invoice_label, 2000, 'Hello, World!', 100))
"""
H10_PROBE = """
from stats import median
from textutil import slugify
rec('m', attempt(median, [10, 20]))
rec('s', attempt(slugify, 'Hello, World!', max_len=6))
"""


def _h1(o, f):
    same(
        (val(o, "a"), val(o, "b"), val(o, "c"), val(o, "d")),
        (15.0, 2.5, 3.5, -2.0),
        "medians",
    )
    raised(o, "e", "ValueError")


def _h3(o, f):
    same(
        (val(o, "a"), val(o, "b"), val(o, "c"), val(o, "d")),
        ("hello-w", "hello", "hello-world", "hello-world"),
        "slugify",
    )


def _h4(o, f):
    same((val(o, "a"), val(o, "b")), ("hello", "hello-world"), "cli")


def _h7(o, f):
    same(val(o, "a"), 100, "compute_tax(1000)")
    same(val(o, "b"), 308, "compute_tax(1234, 0.25)")
    same(val(o, "c"), {"amount": 500, "tax": 50, "total": 550}, "make_invoice")


def _h8(o, f):
    same(
        val(o, "label"), "SLUG:13345", "invoice_label must call compute_tax and slugify"
    )


def _h9(o, f):
    same(
        (val(o, "a"), val(o, "b")), ("hello:2200", "hello-world:2200"), "invoice_label"
    )


def _h10(o, f):
    same((val(o, "m"), val(o, "s")), (15.0, "hello"), "earlier turns still hold")


def _no_calc_tax(check_id: str):
    def run(ctx):
        hits = [
            p
            for p, text in ctx.end_files.items()
            if p.endswith(".py") and "calc_tax" in text
        ]
        if hits:
            return CheckOutcome(
                False, "FAIL_HIDDEN", f"calc_tax still present in {hits[:3]}"
            )
        return CheckOutcome(True)

    return run


def _filler_base() -> dict[str, str]:
    ledger = fixtures.files("ledger")
    drop = {
        "ledger/money.py",
        "ledger/invoice.py",
        "ledger/tax.py",
        "tests/test_invoice.py",
    }
    return {
        p: c
        for p, c in ledger.items()
        if p not in drop and not p.startswith("ledger/legacy/")
    }


def composite_base() -> dict[str, str]:
    files: dict[str, str] = {}
    for key in ("median", "slugify", "billing"):
        files.update(fixtures.files(key))
    files.update(_filler_base())
    return files


def _rename(files: dict[str, str]) -> dict[str, str]:
    out = dict(files)
    for path in RENAME_PATHS:
        out[path] = out[path].replace("calc_tax", "compute_tax")
    return out


def _ends(base: dict[str, str], alt: bool = False) -> list[dict[str, str]]:
    s1 = with_paths(base, {"stats.py": MEDIAN_GOOD})
    tests = (
        base["tests/test_slug.py"].rstrip("\n")
        + "\n"
        + (ALT_SLUG_TESTS if alt else NEW_SLUG_TESTS)
    )
    s2 = with_paths(
        s1,
        {
            "textutil/slug.py": SLUG_ALT if alt else SLUG_GOLD,
            "textutil/cli.py": CLI_GOLD,
            "tests/test_slug.py": tests,
        },
    )
    s3 = _rename(s2)
    invoice = s3["billing/invoice.py"]
    if alt:
        invoice = re.sub(
            r"(?<![\w.])compute_tax\(",
            "taxmod.compute_tax(",
            invoice.replace(
                "from .tax import compute_tax", "from . import tax as taxmod"
            ),
        )
        invoice = "from textutil import slugify\n" + invoice
        invoice += LABEL_FUNC.replace(
            "compute_tax(amount)", "taxmod.compute_tax(amount)"
        )
    else:
        invoice = "from textutil.slug import slugify\n" + invoice + LABEL_FUNC
    s4 = with_paths(s3, {"billing/invoice.py": invoice})
    return [s1, s2, s3, s4, dict(s4)]


def _gold(base: dict[str, str]) -> list[dict[str, str]]:
    return _ends(base)


def _alt(base: dict[str, str]) -> list[dict[str, str]]:
    return _ends(base, alt=True)


def _cumulative_changes(
    base_files: dict[str, str], end_files: dict[str, str]
) -> set[tuple[str, str]]:
    diff = diff_manifests(manifest_of_files(base_files), manifest_of_files(end_files))
    return (
        {(p, "added") for p in diff["added"]}
        | {(p, "modified") for p in diff["modified"]}
        | {(p, "deleted") for p in diff["deleted"]}
    )


def _final_report_matches(ctx: Ctx) -> CheckOutcome:
    rows = (ctx.report or {}).get("files") if isinstance(ctx.report, dict) else None
    if not isinstance(rows, list):
        return CheckOutcome(False, "FAIL_REPORT_MISMATCH", "report.files missing")
    claimed = set()
    for row in rows:
        if not isinstance(row, dict) or "path" not in row or "change" not in row:
            return CheckOutcome(False, "FAIL_REPORT_MISMATCH", f"bad row {row!r}")
        claimed.add((str(row["path"]), str(row["change"])))
    actual = _cumulative_changes(ctx.base_files, ctx.end_files)
    ghosts = sorted(
        p for p, change in claimed if change != "deleted" and p not in ctx.end_files
    )
    if ghosts:
        return CheckOutcome(
            False,
            "FAIL_MISSING_TARGET",
            f"report names files that do not exist: {ghosts[:5]}",
        )
    if claimed != actual:
        return CheckOutcome(
            False,
            "FAIL_REPORT_MISMATCH",
            f"missing={sorted(actual - claimed)[:5]} extra={sorted(claimed - actual)[:5]}",
        )
    return CheckOutcome(True)


def lng_session_check(attempt: Attempt) -> list[tuple[str, CheckOutcome]]:
    """H13/H14. Pending until the harness reports resume lineage and the cancel snapshot (WBS 2.2)."""
    session = attempt.session or {}
    out: list[tuple[str, CheckOutcome]] = []
    resume = session.get("resume")
    if resume is None:
        out.append(("H13", CheckOutcome(None, detail="resume info unavailable")))
    else:
        ok = bool(resume.get("resumed")) and bool(resume.get("lineage_preserved"))
        out.append(
            (
                "H13",
                CheckOutcome(ok, None if ok else "FAIL_HIDDEN", "resume lineage lost"),
            )
        )
    cancel = session.get("cancel")
    if cancel is None:
        out.append(("H14", CheckOutcome(None, detail="cancel info unavailable")))
        return out
    if not cancel.get("completed"):
        out.append(
            (
                "H14",
                CheckOutcome(
                    False, "FAIL_HIDDEN", "turn not completed after cancel/resume"
                ),
            )
        )
        return out
    snapshot = cancel.get("snapshot_dir")
    if not snapshot:
        out.append(("H14", CheckOutcome(None, detail="cancel snapshot not provided")))
        return out
    problems = []
    for path in sorted(Path(snapshot).rglob("*")):
        if path.is_file() and path.suffix == ".py":
            try:
                ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                problems.append(f"does not compile: {path.name}")
        if path.is_file() and path.suffix in (".tmp", ".orig", ".rej", ".swp"):
            problems.append(f"partial file: {path.name}")
    out.append(
        (
            "H14",
            CheckOutcome(
                not problems,
                None if not problems else "FAIL_HIDDEN",
                "; ".join(problems[:3]),
            ),
        )
    )
    return out


def build() -> Task:
    base = composite_base()
    fillers = tuple(
        p
        for p in base
        if p.startswith("tests/test_")
        and p
        not in ("tests/test_stats.py", "tests/test_slug.py", "tests/test_billing.py")
    )
    frozen = ("tests/test_stats.py", "tests/__init__.py", "ledger/*", *fillers)
    gold_slug_sut = {"textutil/slug.py": SLUG_GOLD, "textutil/cli.py": CLI_GOLD}
    spec = SuiteSpec(
        test_path="tests/test_slug.py",
        sut_names=("slugify", "main"),
        gold_overrides=gold_slug_sut,
        mutants={
            "M1_ignore_max_len": {
                "textutil/slug.py": SLUG_GOLD.replace(
                    '    if max_len is not None:\n        s = s[:max_len].rstrip("-")\n',
                    "",
                )
            },
            "M2_off_by_one": {
                "textutil/slug.py": SLUG_GOLD.replace("s[:max_len]", "s[: max_len + 1]")
            },
            "M3_trailing_dash": {
                "textutil/slug.py": SLUG_GOLD.replace(
                    's[:max_len].rstrip("-")', "s[:max_len]"
                )
            },
            "M4_cli_ignores_option": {
                "textutil/cli.py": CLI_GOLD.replace(
                    "return slugify(argv[0], max_len=max_len)",
                    "return slugify(argv[0])",
                )
            },
        },
        min_tests=3,
        allowed_modules=("textutil",),
    )
    stages = [
        Stage(
            "T1",
            prompt=PROMPTS["T1"],
            required_change_paths=("stats.py",),
            checks=[
                Check("H1", codes.KIND_DELTA, "R1", probe=H1_PROBE, judge=_h1),
                frozen_unchanged("H2", ("tests/test_stats.py",), "R2"),
                public_suite("PUB1", 1, codes.KIND_DELTA),
            ],
        ),
        Stage(
            "T2",
            prompt=PROMPTS["T2"],
            required_change_paths=(
                "textutil/slug.py",
                "textutil/cli.py",
                "tests/test_slug.py",
            ),
            checks=[
                Check("H3", codes.KIND_DELTA, "R3", probe=H3_PROBE, judge=_h3),
                Check("H4", codes.KIND_DELTA, "R4", probe=H4_PROBE, judge=_h4),
                suite_collected("H5c", spec, "R5"),
                suite_kills("H5", spec, tuple(spec.mutants), "R5"),
                suite_gold_pass("H5g", spec, "R5", codes.KIND_INVARIANT),
                suite_quality("H5q", spec, "R5", codes.KIND_INVARIANT),
                public_suite("PUB2", 1, codes.KIND_INVARIANT),
            ],
        ),
        Stage(
            "T3",
            prompt=PROMPTS["T3"],
            required_change_paths=RENAME_PATHS,
            checks=[
                Check("H6", codes.KIND_DELTA, "R6", native=_no_calc_tax("H6")),
                Check("H7", codes.KIND_DELTA, "R7", probe=H7_PROBE, judge=_h7),
                public_suite("PUB3", 1, codes.KIND_INVARIANT),
            ],
        ),
        Stage(
            "T4",
            prompt=PROMPTS["T4"],
            required_change_paths=("billing/invoice.py",),
            checks=[
                Check("H8", codes.KIND_DELTA, "R8", probe=H8_PROBE, judge=_h8),
                Check("H9", codes.KIND_DELTA, "R9", probe=H9_PROBE, judge=_h9),
                Check("H10", codes.KIND_INVARIANT, "R10", probe=H10_PROBE, judge=_h10),
                Check("H10s", codes.KIND_INVARIANT, "R10", native=_no_calc_tax("H10s")),
                public_suite("PUB4", 1, codes.KIND_INVARIANT),
            ],
        ),
        Stage(
            "T5",
            change_class=codes.CHANGE_READ_ONLY,
            prompt=PROMPTS["T5"],
            checks=[
                Check("H11", codes.KIND_DELTA, "R11", native=_final_report_matches),
                frozen_unchanged("H12", ("tests/test_stats.py",), "R12"),
            ],
        ),
    ]

    negatives = standard_negatives(
        required=("stats.py",), frozen_test="tests/test_stats.py", stage=0
    )
    negatives.append(presolved_negative())
    for index, stage in enumerate(stages[:4], start=0):
        if index:
            negatives += n15_negatives(stage.required_change_paths, index)

    def slug_tests_with(extra: str):
        def run(prev, gold, base_):
            out = dict(gold)
            out["tests/test_slug.py"] = (
                base_["tests/test_slug.py"].rstrip("\n") + "\n" + extra
            )
            return out

        return run

    no_assert = "\n    def test_max_len_x(self):\n        pass\n\n    def test_max_len_y(self):\n        pass\n"
    skipped = NEW_SLUG_TESTS.replace(
        "    def test_", "    @unittest.skip('later')\n    def test_"
    )

    def alias_kept(prev, gold, base_):
        out = dict(gold)
        out["billing/tax.py"] = out["billing/tax.py"] + "\n\ncalc_tax = compute_tax\n"
        return out

    def duplicated_value(prev, gold, base_):
        out = dict(gold)
        out["billing/invoice.py"] = (
            out["billing/invoice.py"]
            .replace("from textutil.slug import slugify\n", "")
            .replace(
                "slugify(text, max_len=max_len)",
                "text.lower().replace(' ', '-')[:max_len].rstrip('-')",
            )
            .replace("compute_tax(amount)", "round(amount * 0.1)")
        )
        return out

    def t5_extra_edit(prev, gold, base_):
        out = dict(gold)
        out["README.txt"] = "edited during the report turn\n"
        return out

    def report_with(files_rows):
        def run(base_, gold_list):
            final = gold_list[-1]
            actual = sorted(_cumulative_changes(base_, final))
            rows = [{"path": p, "change": c} for p, c in actual]
            return [None, None, None, None, {"files": files_rows(rows)}]

        return run

    negatives += [
        neg(
            "N12",
            "T2 new tests without assertions (comment/no-op tests)",
            slug_tests_with(no_assert),
            ("FAIL_COMMENT_ONLY",),
            stage=1,
        ),
        neg(
            "N13",
            "T2 tests/test_slug.py emptied",
            m_set("tests/test_slug.py", ""),
            ("FAIL_EMPTY_RESULT",),
            stage=1,
        ),
        neg(
            "N5",
            "T2 new tests skipped",
            slug_tests_with(skipped),
            ("FAIL_SKIP_XFAIL",),
            stage=1,
        ),
        neg(
            "N2",
            "T2 comments only",
            m_comment(("textutil/slug.py", "textutil/cli.py", "tests/test_slug.py")),
            ("FAIL_COMMENT_ONLY",),
            stage=1,
        ),
        neg(
            "N3",
            "T3 rename left one file untouched",
            m_revert("billing/invoice.py"),
            ("FAIL_PARTIAL_PATHS",),
            stage=2,
        ),
        neg("N3", "T3 alias calc_tax kept", alias_kept, ("FAIL_HIDDEN_ONLY",), stage=2),
        neg(
            "N4",
            "C1 violated at T3: tests/test_stats.py deleted",
            m_delete("tests/test_stats.py"),
            ("FAIL_TEST_TAMPER",),
            stage=2,
        ),
        neg(
            "N8",
            "T4 label function written to a new file, invoice.py untouched",
            m_misplaced(("billing/invoice.py",)),
            ("FAIL_MISSING_TARGET",),
            stage=3,
        ),
        neg(
            "N3",
            "T4 values duplicated instead of calling compute_tax/slugify",
            duplicated_value,
            ("FAIL_HIDDEN_ONLY",),
            stage=3,
        ),
        neg(
            "N10",
            "T5 report turn edits a file",
            t5_extra_edit,
            ("FAIL_FORBIDDEN_WRITE",),
            stage=4,
        ),
        neg(
            "N8",
            "T5 report names a file that does not exist",
            lambda prev, gold, base_: prev,
            ("FAIL_MISSING_TARGET",),
            stage=4,
            report=report_with(
                lambda rows: [*rows, {"path": "billing/ghost.py", "change": "added"}]
            ),
        ),
        neg(
            "N3",
            "T5 report omits a changed file",
            lambda prev, gold, base_: prev,
            ("FAIL_REPORT_MISMATCH",),
            stage=4,
            report=report_with(lambda rows: rows[1:]),
        ),
        neg(
            "N3",
            "resume lineage lost after the interruption",
            lambda prev, gold, base_: gold,
            ("FAIL_HIDDEN",),
            stage=4,
            report=report_with(lambda rows: rows),
            session={
                "resume": {"resumed": True, "lineage_preserved": False},
                "cancel": {"completed": True},
            },
        ),
    ]

    def gold_reports(base_files, ends):
        rows = [
            {"path": p, "change": c}
            for p, c in sorted(_cumulative_changes(base_files, ends[-1]))
        ]
        return [None, None, None, None, {"files": rows}]

    return check_unique(
        Task(
            id="B4-LNG-D",
            type="long_multi_turn",
            prompt=PROMPTS["T1"],
            base_files=base,
            stages=stages,
            frozen_paths=frozen,
            gold=_gold,
            alt_gold=_alt,
            gold_reports=gold_reports,
            gold_session={
                "resume": {"resumed": True, "lineage_preserved": True},
                "cancel": {"completed": True, "snapshot_dir": None},
            },
            negatives=negatives,
            session_check=lng_session_check,
            sources={
                **fixtures.sources("median"),
                **fixtures.sources("slugify"),
                **fixtures.sources("billing"),
                **fixtures.sources("ledger"),
            },
            notes="turn script texts must be hash-frozen before any run (spec 3.9); H13/H14 interface only",
        )
    )


def turn_script_digest() -> str:
    """sha256 over the ordered turn prompts: freeze this value in the run registration."""
    import hashlib

    body = "\n--\n".join(PROMPTS[k] for k in ("T1", "T2", "T3", "T4", "T5"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


__all__ = ["build", "composite_base", "lng_session_check", "turn_script_digest"]
