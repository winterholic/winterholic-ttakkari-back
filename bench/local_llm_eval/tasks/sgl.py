"""B4-SGL-D single-file fix (median). Fixture: reused `t1-bug-median`. Spec section 3.2."""

from .. import codes, fixtures
from ..judges import raised, same, val
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


H1_PROBE = (
    "from stats import median\n"
    "rec('a', attempt(median, [10, 20]))\n"
    "rec('b', attempt(median, [4, 1, 3, 2]))\n"
    "rec('c', attempt(median, [1, 2, 3, 4, 5, 6]))\n"
)
H2_PROBE = "from stats import median\nrec('e', attempt(median, []))\n"
H3_PROBE = "from stats import median\nrec('a', attempt(median, [3, 1, 2]))\nrec('b', attempt(median, [5]))\n"
H4_PROBE = (
    "from stats import median\n"
    "xs = [3, 1, 2]\nrec('r', attempt(median, xs))\nrec('xs', list(xs))\n"
    "t = (3, 1, 2)\nrec('rt', attempt(median, t))\nrec('t', list(t))\n"
)
H5_PROBE = (
    "from stats import median\n"
    "rec('a', attempt(median, [-3, -1]))\n"
    "rec('b', attempt(median, [0.5, 1.5, 2.5, 3.5]))\n"
    "rec('c', attempt(median, [1.5]))\n"
)
H7_PROBE = (
    "from stats import mean\n"
    "rec('m', attempt(mean, [1, 2, 3]))\n"
    "rec('e', attempt(mean, []))\n"
)


def _h1(o, f):
    same(
        (val(o, "a"), val(o, "b"), val(o, "c")), (15.0, 2.5, 3.5), "even-length medians"
    )


def _h2(o, f):
    raised(o, "e", "ValueError")


def _h3(o, f):
    same((val(o, "a"), val(o, "b")), (2, 5), "odd-length medians")


def _h4(o, f):
    same(val(o, "r"), 2, "list median")
    same(o["xs"], [3, 1, 2], "list input must not be modified")
    same(val(o, "rt"), 2, "tuple median")
    same(o["t"], [3, 1, 2], "tuple input")


def _h5(o, f):
    same(
        (val(o, "a"), val(o, "b"), val(o, "c")),
        (-2.0, 2.0, 1.5),
        "negative/float medians",
    )


def _h7(o, f):
    same(val(o, "m"), 2, "mean unchanged")
    raised(o, "e", "ZeroDivisionError")


def build() -> Task:
    base = fixtures.files("median")
    checks = [
        Check("H1", codes.KIND_DELTA, "R1", probe=H1_PROBE, judge=_h1),
        Check("H2", codes.KIND_DELTA, "R2", probe=H2_PROBE, judge=_h2),
        Check("H3", codes.KIND_INVARIANT, "R3", probe=H3_PROBE, judge=_h3),
        Check("H4", codes.KIND_INVARIANT, "R4", probe=H4_PROBE, judge=_h4),
        Check("H5", codes.KIND_DELTA, "R5", probe=H5_PROBE, judge=_h5),
        frozen_unchanged("H6", FROZEN, "R6"),
        Check("H7", codes.KIND_INVARIANT, "R7", probe=H7_PROBE, judge=_h7),
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
