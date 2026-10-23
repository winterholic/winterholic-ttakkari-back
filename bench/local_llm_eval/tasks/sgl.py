"""B4-SGL-D single-file fix (median). Fixture: reused `t1-bug-median`. Spec section 3.2."""

from .. import codes, fixtures
from ..model import Check, Stage, Task
from ..natives import frozen_unchanged, public_suite
from ..negatives import (
    neg,
    presolved_negative,
    standard_negatives,
)
from ._common import check_unique, single_stage, with_paths

FROZEN = ("tests/*",)

GOOD = '''def median(xs):
    """Median of a list of numbers (does not modify xs)."""
    if not xs:
        raise ValueError("median of empty sequence")
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def mean(xs):
    return sum(xs) / len(xs)
'''

ALT = '''import statistics


def median(xs):
    """Median of a list of numbers (does not modify xs)."""
    values = list(xs)
    if len(values) == 0:
        raise ValueError("empty")
    return statistics.median(values)


def mean(xs):
    return sum(xs) / len(xs)
'''


def _variant(body: str) -> str:
    return GOOD.replace(
        "    s = sorted(xs)\n    n = len(s)\n    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2\n",
        body,
    )


NEGATIVE_SOURCES = {
    "only the even case fixed": GOOD.replace(
        '    if not xs:\n        raise ValueError("median of empty sequence")\n', ""
    ),
    "floor average on even": _variant(
        "    s = sorted(xs)\n    n = len(s)\n    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) // 2\n"
    ),
    "empty returns None": GOOD.replace(
        '        raise ValueError("median of empty sequence")', "        return None"
    ),
    "always ValueError": GOOD.replace(
        "    s = sorted(xs)\n    n = len(s)\n    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2\n",
        '    raise ValueError("x")\n',
    ),
}
SORT_IN_PLACE = GOOD.replace("    s = sorted(xs)\n", "    xs.sort()\n    s = xs\n")
PUBLIC_ONLY = GOOD.replace(
    "    s = sorted(xs)\n",
    "    if sorted(xs) == [1, 2, 3, 4]:\n        return 2.5\n    s = sorted(xs)\n",
).replace(
    "(s[n // 2 - 1] + s[n // 2]) / 2",
    "s[n // 2]",
)


def build() -> Task:
    base = fixtures.files("median")
    checks = [
        Check(
            "H1",
            codes.KIND_DELTA,
            "R1",
            snippet=(
                "from stats import median\n"
                "assert median([10, 20]) == 15.0\n"
                "assert median([4, 1, 3, 2]) == 2.5\n"
                "assert median([1, 2, 3, 4, 5, 6]) == 3.5\n"
            ),
        ),
        Check(
            "H2",
            codes.KIND_DELTA,
            "R2",
            snippet=(
                "from stats import median\n"
                "try:\n    median([])\nexcept ValueError:\n    pass\n"
                "else:\n    raise AssertionError('no ValueError')\n"
            ),
        ),
        Check(
            "H3",
            codes.KIND_INVARIANT,
            "R3",
            snippet="from stats import median\nassert median([3, 1, 2]) == 2 and median([5]) == 5\n",
        ),
        Check(
            "H4",
            codes.KIND_INVARIANT,
            "R4",
            snippet=(
                "from stats import median\n"
                "xs = [3, 1, 2]\nmedian(xs)\nassert xs == [3, 1, 2]\n"
                "t = (3, 1, 2)\nassert median(t) == 2 and t == (3, 1, 2)\n"
            ),
        ),
        Check(
            "H5",
            codes.KIND_DELTA,
            "R5",
            snippet=(
                "from stats import median\n"
                "assert median([-3, -1]) == -2.0\n"
                "assert median([0.5, 1.5, 2.5, 3.5]) == 2.0\n"
                "assert median([1.5]) == 1.5\n"
            ),
        ),
        frozen_unchanged("H6", FROZEN, "R6"),
        Check(
            "H7",
            codes.KIND_INVARIANT,
            "R7",
            snippet=(
                "from stats import mean\n"
                "assert mean([1, 2, 3]) == 2\n"
                "try:\n    mean([])\nexcept ZeroDivisionError:\n    pass\n"
                "else:\n    raise AssertionError('mean([]) changed')\n"
            ),
        ),
        public_suite("PUB", 4, codes.KIND_DELTA),
    ]
    stage = Stage(
        "S1",
        change_class=codes.CHANGE_DIFF,
        prompt=fixtures.load("median")["original_prompt"],
        checks=checks,
        required_change_paths=("stats.py",),
    )
    negatives = standard_negatives(
        required=("stats.py",), frozen_test="tests/test_stats.py"
    )
    negatives.append(presolved_negative())
    for name, source in NEGATIVE_SOURCES.items():
        negatives.append(
            neg(
                "N3",
                f"partial/incorrect fix: {name}",
                _set_stats(source),
                ("FAIL_PUBLIC",),
            )
        )
    negatives.append(
        neg(
            "N3",
            "input list sorted in place",
            _set_stats(SORT_IN_PLACE),
            ("FAIL_REGRESSION",),
        )
    )
    negatives.append(
        neg(
            "N3",
            "special-cases the public example only (public green, hidden red)",
            _set_stats(PUBLIC_ONLY),
            ("FAIL_HIDDEN_ONLY",),
        )
    )
    return check_unique(
        Task(
            id="B4-SGL-D",
            type="single_file",
            prompt=stage.prompt,
            base_files=base,
            stages=[stage],
            frozen_paths=FROZEN,
            gold=single_stage(lambda b: with_paths(b, {"stats.py": GOOD})),
            alt_gold=single_stage(lambda b: with_paths(b, {"stats.py": ALT})),
            negatives=negatives,
            sources=fixtures.sources("median"),
        )
    )


def _set_stats(source: str):
    def run(prev, gold, base):
        out = dict(prev)
        out["stats.py"] = source
        return out

    return run
