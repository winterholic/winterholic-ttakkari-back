"""B4-REC-D autonomous recovery (shop). Fixture: stage1 `shop` template. Spec section 3.8.

Built-in trap (CPU-confirmed in 4.1): the common first patch `round(price*(100-pct)/100)` rounds
502.5 to 502 (banker's rounding) and fails the public half-up test, so a model must notice the
failing run and fix it. H7 only measures that from a trace; it never decides success.
"""

from .. import codes, fixtures
from ..model import Check, CheckOutcome, Ctx, Stage, Task
from ..natives import (
    SuiteSpec,
    frozen_unchanged,
    public_suite,
    suite_gold_pass,
    suite_kills,
    suite_quality,
)
from ..negatives import m_set, neg, presolved_negative, standard_negatives
from ._common import check_unique, single_stage, with_paths

REQUIRED = ("shop/cart.py", "shop/pricing.py", "tests/test_pricing.py")
TEST_PATH = "tests/test_pricing.py"
FROZEN = ("tests/test_cart.py", "tests/__init__.py")

GOLD_PRICING = '''"""가격 계산. 금액은 모두 정수 센트."""


def apply_discount(price_cents: int, percent: int) -> int:
    """percent% 할인한 가격을 돌려준다. 소수점 이하는 반올림(half-up)한다.

    percent는 0 이상 100 이하여야 하며 벗어나면 ValueError를 낸다.
    """
    if percent < 0 or percent > 100:
        raise ValueError("percent must be within 0..100")
    return (price_cents * (100 - percent) * 2 + 100) // 200
'''
ALT_PRICING = '''"""가격 계산. 금액은 모두 정수 센트."""
from decimal import ROUND_HALF_UP, Decimal


def apply_discount(price_cents: int, percent: int) -> int:
    """percent% 할인한 가격을 돌려준다. 소수점 이하는 반올림(half-up)한다.

    percent는 0 이상 100 이하여야 하며 벗어나면 ValueError를 낸다.
    """
    if not 0 <= percent <= 100:
        raise ValueError(f"percent out of range: {percent}")
    exact = Decimal(price_cents) * (100 - percent) / 100
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))
'''
NAIVE_PRICING = GOLD_PRICING.replace(
    "(price_cents * (100 - percent) * 2 + 100) // 200",
    "round(price_cents * (100 - percent) / 100)",
)
GOLD_TESTS = """import unittest

from shop.pricing import apply_discount


class PricingTests(unittest.TestCase):
    def test_negative_percent_rejected(self):
        with self.assertRaises(ValueError):
            apply_discount(100, -1)

    def test_over_hundred_rejected(self):
        with self.assertRaises(ValueError):
            apply_discount(100, 101)

    def test_boundaries_allowed(self):
        self.assertEqual(apply_discount(100, 0), 100)
        self.assertEqual(apply_discount(100, 100), 0)
"""
ALT_TESTS = """import unittest

from shop import pricing


class RangeGuard(unittest.TestCase):
    def test_outside_range(self):
        for bad in (-1, 101):
            with self.subTest(percent=bad):
                with self.assertRaises(ValueError):
                    pricing.apply_discount(500, bad)

    def test_inside_range(self):
        for ok in (0, 1, 99, 100):
            with self.subTest(percent=ok):
                self.assertGreaterEqual(pricing.apply_discount(500, ok), 0)
"""

H1 = """
from shop.cart import Cart
c = Cart()
c.add('a', 250, qty=3)
c.add('b', 100)
assert c.subtotal() == 850, c.subtotal()
assert c.total(10) == 765, c.total(10)
assert c.total() == 850
"""
H2 = """
from shop.pricing import apply_discount
assert apply_discount(999, 15) == 849
assert apply_discount(1005, 50) == 503
assert apply_discount(1, 50) == 1
assert apply_discount(200, 100) == 0
assert apply_discount(200, 0) == 200
"""
H3 = """
from shop.pricing import apply_discount
for bad in (-1, 101, -100, 1000):
    try:
        apply_discount(100, bad)
    except ValueError:
        pass
    else:
        raise AssertionError('no ValueError for %d' % bad)
apply_discount(100, 0)
apply_discount(100, 100)
"""


def _gold(base):
    cart = base["shop/cart.py"].replace(
        "sum(price for _, price, _ in self.items)",
        "sum(p * q for _, p, q in self.items)",
    )
    return with_paths(
        base,
        {"shop/cart.py": cart, "shop/pricing.py": GOLD_PRICING, TEST_PATH: GOLD_TESTS},
    )


def _alt(base):
    out = _gold(base)
    out["shop/pricing.py"] = ALT_PRICING
    out[TEST_PATH] = ALT_TESTS
    return out


def _recovery_metric(ctx: Ctx) -> CheckOutcome:
    """Process metric from a trace: did a failing test run follow the first edit, then a pass?"""
    if ctx.trace is None:
        return CheckOutcome(None, detail="trace unavailable (2.2-dependent)")
    events = sorted(ctx.trace, key=lambda e: e.get("t", 0))
    first_edit = next(
        (i for i, e in enumerate(events) if e.get("kind") == "edit"), None
    )
    activated = recovered = False
    if first_edit is not None:
        after = [e for e in events[first_edit + 1 :] if e.get("kind") == "test"]
        for index, event in enumerate(after):
            if event.get("exit_code", 0) != 0:
                activated = True
                recovered = any(e.get("exit_code", 1) == 0 for e in after[index + 1 :])
                break
    return CheckOutcome(
        True, metrics={"trap_activated": activated, "recovered": recovered}
    )


def build() -> Task:
    base = fixtures.files("shop")
    prompt = fixtures.load("shop")["original_prompt"]
    gold_sut = {
        "shop/pricing.py": GOLD_PRICING,
        "shop/cart.py": _gold(base)["shop/cart.py"],
    }
    spec = SuiteSpec(
        test_path=TEST_PATH,
        sut_names=("apply_discount", "pricing"),
        gold_overrides=gold_sut,
        mutants={
            "M1_no_lower_check": {
                "shop/pricing.py": GOLD_PRICING.replace(
                    "if percent < 0 or percent > 100:", "if percent > 100:"
                )
            },
            "M2_no_upper_check": {
                "shop/pricing.py": GOLD_PRICING.replace(
                    "if percent < 0 or percent > 100:", "if percent < 0:"
                )
            },
            "M3_wrong_exception": {
                "shop/pricing.py": GOLD_PRICING.replace(
                    "raise ValueError", "raise TypeError"
                )
            },
        },
        min_tests=2,
    )
    checks = [
        Check("H1", codes.KIND_DELTA, "R1", snippet=H1),
        Check("H2", codes.KIND_DELTA, "R2", snippet=H2),
        Check("H3", codes.KIND_DELTA, "R3", snippet=H3),
        suite_kills("H4", spec, tuple(spec.mutants), "R4"),
        suite_quality("H4q", spec, "R4"),
        suite_gold_pass("H5", spec, "R5"),
        frozen_unchanged("H6", FROZEN, "R6"),
        Check("H7", codes.KIND_PROCESS, "R7", native=_recovery_metric),
        public_suite("PUB", 5, codes.KIND_DELTA),
    ]
    stage = Stage(
        "S1",
        change_class=codes.CHANGE_DIFF,
        prompt=prompt,
        checks=checks,
        required_change_paths=REQUIRED,
    )
    negatives = standard_negatives(required=REQUIRED, frozen_test="tests/test_cart.py")
    negatives.append(presolved_negative())
    skip_all = GOLD_TESTS.replace(
        "    def test_", "    @unittest.skip('later')\n    def test_"
    )
    imports_only = "import unittest\n\nfrom shop.pricing import apply_discount\n"

    def gold_with(extra: dict[str, str]):
        def run(prev, gold, base_):
            out = dict(gold)
            out.update(extra)
            return out

        return run

    negatives += [
        neg(
            "N12",
            "boundary test replaced by a comment '# ValueError -1 101' (audit P1-1)",
            gold_with({TEST_PATH: "# ValueError -1 101\n"}),
            ("FAIL_COMMENT_ONLY",),
        ),
        neg(
            "N13",
            "empty tests/test_pricing.py",
            gold_with({TEST_PATH: ""}),
            ("FAIL_EMPTY_RESULT",),
        ),
        neg(
            "N13",
            "tests/test_pricing.py with imports only (0 tests collected)",
            gold_with({TEST_PATH: imports_only}),
            ("FAIL_EMPTY_RESULT",),
        ),
        neg(
            "N5",
            "all new tests skipped",
            gold_with({TEST_PATH: skip_all}),
            ("FAIL_SKIP_XFAIL",),
        ),
        neg(
            "N3",
            "trap patch: round() half-even makes the public half-up test fail",
            gold_with({"shop/pricing.py": NAIVE_PRICING}),
            ("FAIL_PUBLIC",),
        ),
        neg(
            "N3",
            "tests added but range validation never implemented",
            gold_with(
                {
                    "shop/pricing.py": GOLD_PRICING.replace(
                        '    if percent < 0 or percent > 100:\n        raise ValueError("percent must be within 0..100")\n',
                        "",
                    )
                }
            ),
            ("FAIL_PUBLIC",),
        ),
        neg(
            "N3",
            "SUT fixed, new test asserts only unrelated values",
            m_set(
                TEST_PATH,
                "import unittest\n\nfrom shop.pricing import apply_discount\n\n\nclass T(unittest.TestCase):\n"
                "    def test_a(self):\n        self.assertEqual(apply_discount(100, 10), 90)\n\n"
                "    def test_b(self):\n        self.assertEqual(apply_discount(200, 50), 100)\n",
                on_gold=True,
            ),
            ("FAIL_HIDDEN_ONLY",),
        ),
    ]
    return check_unique(
        Task(
            id="B4-REC-D",
            type="autonomous_recovery",
            prompt=prompt,
            base_files=base,
            stages=[stage],
            frozen_paths=FROZEN,
            gold=single_stage(_gold),
            alt_gold=single_stage(_alt),
            negatives=negatives,
            sources=fixtures.sources("shop"),
        )
    )
