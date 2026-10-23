"""B4-MUL-D multi-file fix (inventory), prompt rev2. Fixture: reused `s3-fix-inventory`. Spec 3.3.

Rev2 prompt states what the hidden checks require (qty/total separators, empty list line); the
original prompt left them implicit (1.2 audit P1-2: raw C_opt line 60 failed on `5 x 2,000`).
"""

from .. import codes, fixtures
from ..judges import same, val
from ..model import Check, CheckOutcome, Ctx, Stage, Task
from ..natives import frozen_unchanged, public_suite
from ..negatives import neg, presolved_negative, standard_negatives
from ._common import check_unique, single_stage, with_paths

FROZEN = ("tests/*",)
REQUIRED = ("inv/items.py", "inv/report.py")

PROMPT_ADDENDUM = (
    " 보고서(format_report)의 가격·수량·TOTAL 등 모든 숫자에는 천 단위 구분자(,)를 쓰고,"
    " 항목이 하나도 없으면 'TOTAL: 0' 한 줄만 출력한다."
)

GOLD_REPORT = '''from .items import total_value


def format_report(items):
    """One line per item sorted by name, then a TOTAL line. Numbers use thousands separators."""
    lines = []
    for i in sorted(items, key=lambda i: i.name):
        lines.append(f"{i.name}: {i.price:,} x {i.qty:,}")
    lines.append(f"TOTAL: {total_value(items):,}")
    return "\\n".join(lines)
'''
ALT_REPORT = '''from operator import attrgetter

from .items import total_value


def _n(value):
    return format(value, ",")


def format_report(items):
    """One line per item sorted by name, then a TOTAL line. Numbers use thousands separators."""
    rows = [f"{i.name}: {_n(i.price)} x {_n(i.qty)}" for i in sorted(items, key=attrgetter("name"))]
    return "\\n".join(rows + ["TOTAL: " + _n(total_value(items))])
'''


def _gold(base: dict[str, str]) -> dict[str, str]:
    items = base["inv/items.py"].replace("i.price + i.qty", "i.price * i.qty")
    return with_paths(base, {"inv/items.py": items, "inv/report.py": GOLD_REPORT})


def _alt(base: dict[str, str]) -> dict[str, str]:
    items = base["inv/items.py"].replace(
        "return sum(i.price + i.qty for i in items)",
        "return sum(i.qty * i.price for i in items)",
    )
    return with_paths(base, {"inv/items.py": items, "inv/report.py": ALT_REPORT})


def _both_files_changed(ctx: Ctx) -> CheckOutcome:
    changed = set(ctx.changed["modified"] + ctx.changed["added"])
    missing = [p for p in REQUIRED if p not in changed]
    if missing:
        return CheckOutcome(False, "FAIL_PARTIAL_PATHS", f"unchanged: {missing}")
    return CheckOutcome(True)


H1_PROBE = (
    "from inv import Item, total_value\n"
    "rec('t', attempt(lambda: total_value([Item('x', 7, 9)])))\n"
    "rec('z', attempt(total_value, []))\n"
)
H2_PROBE = (
    "from inv import Item, format_report\n"
    "rec('r', attempt(lambda: format_report([Item('b', 100, 1), Item('a', 500, 1), Item('c', 50, 1)])))\n"
)
H3_PROBE = (
    "from inv import Item, format_report\n"
    "rec('r', attempt(lambda: format_report([Item('alpha', 5, 2000), Item('zeta', 1000000, 1)])))\n"
)
H4_PROBE = (
    "from inv import Item, format_report\n"
    "rec('r', attempt(lambda: format_report([Item('zeta', 1000000, 1), Item('alpha', 5, 2000), Item('mid', 10, 10)])))\n"
)
H5_PROBE = "from inv import format_report\nrec('r', attempt(format_report, []))\n"


def _h1(o, f):
    same((val(o, "t"), val(o, "z")), (63, 0), "totals")


def _h2(o, f):
    lines = val(o, "r").splitlines()[:-1]
    same([line.split(":")[0] for line in lines], ["a", "b", "c"], "item order")


def _h3(o, f):
    report = val(o, "r")
    assert "alpha: 5 x 2,000" in report, report
    assert "zeta: 1,000,000 x 1" in report, report


def _h4(o, f):
    want = "alpha: 5 x 2,000\nmid: 10 x 10\nzeta: 1,000,000 x 1\nTOTAL: 1,010,100"
    same(val(o, "r"), want, "report")


def _h5(o, f):
    same(val(o, "r"), "TOTAL: 0", "empty report")


def build() -> Task:
    base = fixtures.files("inventory")
    prompt = fixtures.load("inventory")["original_prompt"] + PROMPT_ADDENDUM
    checks = [
        Check("H1", codes.KIND_DELTA, "R1", probe=H1_PROBE, judge=_h1),
        Check("H2", codes.KIND_DELTA, "R2", probe=H2_PROBE, judge=_h2),
        Check("H3", codes.KIND_DELTA, "R3", probe=H3_PROBE, judge=_h3),
        Check("H4", codes.KIND_DELTA, "R4", probe=H4_PROBE, judge=_h4),
        Check("H5", codes.KIND_INVARIANT, "R5", probe=H5_PROBE, judge=_h5),
        frozen_unchanged("H6", FROZEN, "R6"),
        Check("H7", codes.KIND_DELTA, "R7", native=_both_files_changed),
        public_suite("PUB", 3, codes.KIND_DELTA),
    ]
    stage = Stage(
        "S1",
        change_class=codes.CHANGE_DIFF,
        prompt=prompt,
        checks=checks,
        required_change_paths=REQUIRED,
    )
    negatives = standard_negatives(required=REQUIRED, frozen_test="tests/test_inv.py")
    negatives.append(presolved_negative())

    def total_only(prev, gold, base_):
        return with_paths(prev, {"inv/items.py": gold["inv/items.py"]})

    def price_only_separator(prev, gold, base_):
        out = dict(gold)
        out["inv/report.py"] = GOLD_REPORT.replace("{i.qty:,}", "{i.qty}")
        return out

    def hardcoded(prev, gold, base_):
        out = dict(gold)
        out["inv/report.py"] = (
            GOLD_REPORT.replace(
                "    lines = []\n",
                "    if [i.name for i in items] == ['pear', 'apple']:\n"
                "        return 'apple: 3,000 x 2\\npear: 1,200 x 1\\nTOTAL: 7,200'\n    lines = []\n",
            )
            .replace("{i.price:,} x {i.qty:,}", "{i.price} x {i.qty}")
            .replace("{total_value(items):,}", "{total_value(items)}")
        )
        return out

    def tests_expectations_edited(prev, gold, base_):
        out = dict(gold)
        out["tests/test_inv.py"] = prev["tests/test_inv.py"].replace("7,200", "7200")
        return out

    negatives += [
        neg(
            "N3",
            "only the total fixed (report untouched)",
            total_only,
            ("FAIL_PARTIAL_PATHS",),
        ),
        neg(
            "N14",
            "qty without thousands separator ('5 x 2000', audit raw line 60)",
            price_only_separator,
            ("FAIL_PUBLIC", "FAIL_HIDDEN", "FAIL_HIDDEN_ONLY"),
        ),
        neg(
            "N3",
            "public example hard-coded, separators dropped",
            hardcoded,
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N4",
            "public test expectation edited to match broken output",
            tests_expectations_edited,
            ("FAIL_TEST_TAMPER",),
        ),
    ]
    return check_unique(
        Task(
            id="B4-MUL-D",
            type="multi_file",
            prompt=prompt,
            base_files=base,
            stages=[stage],
            frozen_paths=FROZEN,
            gold=single_stage(_gold),
            alt_gold=single_stage(_alt),
            negatives=negatives,
            sources=fixtures.sources("inventory"),
        )
    )
