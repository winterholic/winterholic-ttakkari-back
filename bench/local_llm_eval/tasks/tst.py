"""B4-TST-D test writing (apply_discount). Fixture: reused `t3-write-tests`. Spec section 3.7.

The old verifier did not require the candidate tests to pass on the gold SUT (probe P4) and counted
`def test_` with a regex. Here the candidate file is loaded by unittest, must pass on the gold SUT
without skips, must kill 8 pre-verified mutants, and every test needs an assertion that touches the SUT.
"""

from .. import codes, fixtures
from ..model import Stage, Task
from ..natives import (
    SuiteSpec,
    suite_collected,
    suite_gold_pass,
    suite_kills,
    suite_quality,
    unchanged_paths,
)
from ..negatives import (
    EXIT0,
    m_delete,
    m_set,
    neg,
    presolved_negative,
    standard_negatives,
)
from ._common import check_unique, single_stage, with_paths

TEST_PATH = "tests/test_discount.py"
SUT_PATH = "shop/discount.py"

GOLD_TESTS = """import unittest

from shop.discount import apply_discount


class DiscountTests(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(apply_discount(200, 25), 150)

    def test_zero_percent_is_identity(self):
        self.assertEqual(apply_discount(80, 0), 80)

    def test_hundred_percent_is_free(self):
        self.assertEqual(apply_discount(80, 100), 0)

    def test_negative_percent_rejected(self):
        with self.assertRaises(ValueError):
            apply_discount(10, -1)

    def test_over_hundred_rejected(self):
        with self.assertRaises(ValueError):
            apply_discount(10, 101)

    def test_rounds_to_two_decimals(self):
        self.assertEqual(apply_discount(10.01, 33), 6.71)
"""

ALT_TESTS = """import unittest

from shop import discount


class DiscountTable(unittest.TestCase):
    def test_values(self):
        for price, pct, want in [(200, 25, 150), (80, 0, 80), (80, 100, 0)]:
            with self.subTest(price=price, pct=pct):
                self.assertEqual(discount.apply_discount(price, pct), want)

    def test_out_of_range(self):
        for pct in (-1, 101, -50, 1000):
            with self.subTest(pct=pct):
                with self.assertRaises(ValueError):
                    discount.apply_discount(10, pct)

    def test_boundaries_are_accepted(self):
        self.assertEqual(discount.apply_discount(0, 0), 0)
        self.assertEqual(discount.apply_discount(5, 100), 0)

    def test_rounding(self):
        self.assertEqual(discount.apply_discount(10.01, 33), 6.71)
        self.assertEqual(discount.apply_discount(99.99, 10), 89.99)
"""


def _mutants(sut: str) -> dict[str, dict[str, str]]:
    def mutate(old: str, new: str) -> dict[str, str]:
        assert old in sut, old
        return {SUT_PATH: sut.replace(old, new)}

    cond = "if percent < 0 or percent > 100:"
    calc = "round(price * (100 - percent) / 100, 2)"
    return {
        "M1_reject_100": mutate(cond, "if percent < 0 or percent >= 100:"),
        "M2_reject_0": mutate(cond, "if percent <= 0 or percent > 100:"),
        "M3_round_1": mutate(calc, "round(price * (100 - percent) / 100, 1)"),
        "M4_no_upper_check": mutate(cond, "if percent < 0:"),
        "M5_no_lower_check": mutate(cond, "if percent > 100:"),
        "M6_no_rounding": mutate(calc, "price * (100 - percent) / 100"),
        "M7_wrong_exception": mutate("raise ValueError", "raise TypeError"),
        "M8_swapped_formula": mutate(calc, "round(price * percent / 100, 2)"),
    }


def build() -> Task:
    base = fixtures.files("discount")
    prompt = fixtures.load("discount")["original_prompt"]
    spec = SuiteSpec(
        test_path=TEST_PATH,
        sut_names=("apply_discount", "discount"),
        gold_overrides={},
        mutants=_mutants(base[SUT_PATH]),
        min_tests=4,
    )
    checks = [
        suite_collected("H1", spec, "R1"),
        suite_gold_pass("H2", spec, "R2"),
        suite_kills("H3", spec, ("M1_reject_100", "M2_reject_0"), "R3"),
        suite_kills(
            "H4",
            spec,
            ("M4_no_upper_check", "M5_no_lower_check", "M7_wrong_exception"),
            "R4",
        ),
        suite_kills("H5", spec, ("M3_round_1", "M6_no_rounding"), "R5"),
        suite_kills("H6", spec, tuple(spec.mutants), "R6"),
        suite_quality("H7", spec, "R7"),
        unchanged_paths("H8", (SUT_PATH,), "R8"),
    ]
    stage = Stage(
        "S1",
        change_class=codes.CHANGE_DIFF,
        prompt=prompt,
        checks=checks,
        required_change_paths=(TEST_PATH,),
    )
    negatives = standard_negatives(required=(TEST_PATH,), frozen_test=None)
    negatives.append(presolved_negative())
    skip_all = GOLD_TESTS.replace(
        "    def test_", "    @unittest.skip('later')\n    def test_"
    )
    no_assert = (
        "import unittest\n\n# apply_discount boundary 0 100 -1 101 rounding\n\n\n"
        "class T(unittest.TestCase):\n"
        + "".join(
            f"    def test_{n}(self):\n        pass\n\n" for n in ("a", "b", "c", "d")
        )
    )
    always_fail = "import unittest\n\n\nclass T(unittest.TestCase):\n" + "".join(
        f"    def test_{n}(self):\n        self.fail()\n\n"
        for n in ("a", "b", "c", "d")
    )
    constants = (
        "import unittest\n\nfrom shop.discount import apply_discount\n\n\nclass T(unittest.TestCase):\n"
        + "".join(
            f"    def test_{n}(self):\n        self.assertEqual(1, 1)\n\n"
            for n in ("a", "b", "c", "d")
        )
    )
    normal_only = (
        "import unittest\n\nfrom shop.discount import apply_discount\n\n\nclass T(unittest.TestCase):\n"
        + "".join(
            f"    def test_{n}(self):\n        self.assertEqual(apply_discount({p}, {c}), {w})\n\n"
            for n, p, c, w in (
                ("a", 200, 25, 150),
                ("b", 100, 10, 90),
                ("c", 50, 50, 25),
                ("d", 40, 25, 30),
            )
        )
    )
    self_referential = (
        "import unittest\n\nimport shop.discount as d\n\n\nclass T(unittest.TestCase):\n"
        "    def setUp(self):\n        d.apply_discount = lambda price, percent: 150\n\n"
        + "".join(
            f"    def test_{n}(self):\n        self.assertEqual(d.apply_discount(200, 25), 150)\n\n"
            for n in ("a", "b", "c", "d")
        )
    )
    imports_only = "import unittest\n\nfrom shop.discount import apply_discount\n"
    negatives += [
        neg(
            "N4",
            "test infrastructure tests/__init__.py deleted",
            m_delete("tests/__init__.py"),
            ("FAIL_TEST_TAMPER",),
        ),
        neg(
            "N5",
            "every gold test decorated with unittest.skip",
            m_set(TEST_PATH, skip_all),
            ("FAIL_SKIP_XFAIL",),
        ),
        neg(
            "N12",
            "boundary conditions only in comments, tests without assertions",
            m_set(TEST_PATH, no_assert),
            ("FAIL_COMMENT_ONLY",),
        ),
        neg(
            "N13",
            "test file with imports only (0 tests collected)",
            m_set(TEST_PATH, imports_only),
            ("FAIL_EMPTY_RESULT",),
        ),
        neg(
            "N3",
            "always-failing tests (old verifier's hidden part passed this, probe P4)",
            m_set(TEST_PATH, always_fail),
            ("FAIL_COMMENT_ONLY", "FAIL_HIDDEN"),
        ),
        neg(
            "N3",
            "assertions that never touch the SUT",
            m_set(TEST_PATH, constants),
            ("FAIL_COMMENT_ONLY",),
        ),
        neg(
            "N3",
            "only happy-path tests (mutants survive)",
            m_set(TEST_PATH, normal_only),
            ("FAIL_HIDDEN",),
        ),
        neg(
            "N7",
            "tests monkeypatch the SUT in setUp",
            m_set(TEST_PATH, self_referential),
            ("FAIL_HIDDEN",),
        ),
        neg(
            "N10",
            "gold tests but the SUT was edited too",
            _edit_sut(),
            ("FAIL_FORBIDDEN_WRITE",),
        ),
        neg(
            "N6",
            "exit 0 inside the test file",
            m_set(TEST_PATH, EXIT0 + GOLD_TESTS),
            ("FAIL_STUB_EXIT0",),
        ),
    ]
    return check_unique(
        Task(
            id="B4-TST-D",
            type="write_tests",
            prompt=prompt,
            base_files=base,
            stages=[stage],
            frozen_paths=("tests/__init__.py",),
            gold=single_stage(lambda b: with_paths(b, {TEST_PATH: GOLD_TESTS})),
            alt_gold=single_stage(lambda b: with_paths(b, {TEST_PATH: ALT_TESTS})),
            negatives=negatives,
            sources=fixtures.sources("discount"),
        )
    )


def _edit_sut():
    def run(prev, gold, base):
        out = dict(gold)
        out[SUT_PATH] = out[SUT_PATH].replace("percent > 100", "percent > 1000")
        return out

    return run
