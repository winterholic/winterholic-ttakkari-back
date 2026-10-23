"""B4-REF-D refactor (dedupe helper). Fixture: reused `t2-refactor-dedupe`. Spec section 3.6.

The old verifier matched strings ('common' in source), so an inline copy plus a comment passed
(probe P6). H2 substitutes report/common.py with a sentinel and requires both callers to route
through it; H5 forbids a surviving duplicated body.
"""

import ast
import json

from .. import codes, fixtures
from ..judges import same, val
from ..model import Check, CheckOutcome, Stage, Task
from ..natives import frozen_unchanged, public_suite
from ..negatives import neg, presolved_negative, standard_negatives
from ._common import check_unique, single_stage, with_paths

FROZEN = ("tests/*",)
REQUIRED = ("report/common.py", "report/csvout.py", "report/jsonout.py")

COMMON = 'def clean(s):\n    return " ".join(str(s).split()).replace(",", ";")\n'
CSVOUT = (
    "from .common import clean\n\n\n"
    "def to_csv(rows):\n"
    '    return "\\n".join(",".join(clean(c) for c in r) for r in rows)\n'
)
JSONOUT = (
    "import json\n\nfrom .common import clean\n\n\n"
    "def to_json(rows):\n"
    "    return json.dumps([[clean(c) for c in r] for r in rows], ensure_ascii=False)\n"
)
ALT_CSVOUT = CSVOUT.replace(
    "from .common import clean", "from . import common"
).replace("clean(c)", "common.clean(c)")
ALT_JSONOUT = JSONOUT.replace(
    "from .common import clean", "from . import common"
).replace("clean(c)", "common.clean(c)")

H1_PROBE = "from report.common import clean\nrec('c', attempt(clean, '  a,  b '))\n"

H2_PROBE = """
from report import common
common.clean = lambda s: "SENTINEL"
from report.csvout import to_csv
from report.jsonout import to_json
rec('csv', attempt(to_csv, [['a', 'b']]))
rec('json', attempt(to_json, [['a']]))
"""

H4_ROWS = [
    ["a b", "c,d"],
    ["  x\t\ty  ", "1,2,3", 5],
    ["\u00a0nbsp\u00a0", None, 3.5],
    ["한국어  공백", "a\nb"],
    [],
]
H4_PROBE = (
    "from report.csvout import to_csv\nfrom report.jsonout import to_json\n"
    f"rows = {H4_ROWS!r}\n"
    "rec('csv', attempt(to_csv, rows))\nrec('json', attempt(to_json, rows))\n"
)

H7_PROBE = """
import inspect
from report.csvout import to_csv
from report.jsonout import to_json
rec('csv', list(inspect.signature(to_csv).parameters))
rec('json', list(inspect.signature(to_json).parameters))
"""


def _reference_clean(value):
    return " ".join(str(value).split()).replace(",", ";")


def _h1(o, f):
    same(val(o, "c"), "a; b", "clean")


def _h2(o, f):
    same(
        val(o, "csv"),
        "SENTINEL,SENTINEL",
        "to_csv must route through report.common.clean",
    )
    same(
        val(o, "json"),
        '[["SENTINEL"]]',
        "to_json must route through report.common.clean",
    )


def _h4(o, f):
    csv_want = "\n".join(",".join(_reference_clean(c) for c in r) for r in H4_ROWS)
    json_want = json.dumps(
        [[_reference_clean(c) for c in r] for r in H4_ROWS], ensure_ascii=False
    )
    same(val(o, "csv"), csv_want, "to_csv behaviour")
    same(val(o, "json"), json_want, "to_json behaviour")


def _h7(o, f):
    same((o["csv"], o["json"]), (["rows"], ["rows"]), "signatures")


def _no_old_name(ctx):
    """Static scan in the evaluator process: the old private name must be gone from every .py file."""
    hits = [
        p for p, text in ctx.end_files.items() if p.endswith(".py") and "_clean" in text
    ]
    if hits:
        return CheckOutcome(False, "FAIL_HIDDEN", f"_clean still present in {hits[:3]}")
    return CheckOutcome(True)


def _no_duplicate_bodies(ctx):
    """Static scan: no two non-test functions may share a normalised body."""
    seen = {}
    for path, text in sorted(ctx.end_files.items()):
        if not path.endswith(".py") or path.startswith("tests/"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return CheckOutcome(False, "FAIL_HIDDEN", f"{path} does not parse")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.body:
                key = ast.dump(ast.Module(body=node.body, type_ignores=[]))
                if key in seen:
                    return CheckOutcome(
                        False,
                        "FAIL_HIDDEN",
                        f"duplicate body: {seen[key]} and {path}:{node.name}",
                    )
                seen[key] = f"{path}:{node.name}"
    return CheckOutcome(True)


def _gold(base):
    return with_paths(
        base,
        {
            "report/common.py": COMMON,
            "report/csvout.py": CSVOUT,
            "report/jsonout.py": JSONOUT,
        },
    )


def _alt(base):
    return with_paths(
        base,
        {
            "report/common.py": COMMON,
            "report/csvout.py": ALT_CSVOUT,
            "report/jsonout.py": ALT_JSONOUT,
        },
    )


def build() -> Task:
    base = fixtures.files("dedupe")
    prompt = fixtures.load("dedupe")["original_prompt"]
    checks = [
        Check("H1", codes.KIND_DELTA, "R1", probe=H1_PROBE, judge=_h1),
        Check("H2", codes.KIND_DELTA, "R2", probe=H2_PROBE, judge=_h2),
        Check("H3", codes.KIND_DELTA, "R3", native=_no_old_name),
        Check("H4", codes.KIND_INVARIANT, "R4", probe=H4_PROBE, judge=_h4),
        Check("H5", codes.KIND_DELTA, "R5", native=_no_duplicate_bodies),
        frozen_unchanged("H6", FROZEN, "R6"),
        Check("H7", codes.KIND_INVARIANT, "R7", probe=H7_PROBE, judge=_h7),
        public_suite("PUB", 2, codes.KIND_INVARIANT),
    ]
    stage = Stage(
        "S1",
        change_class=codes.CHANGE_DIFF,
        prompt=prompt,
        checks=checks,
        required_change_paths=REQUIRED,
    )
    negatives = standard_negatives(required=REQUIRED, frozen_test="tests/test_out.py")
    negatives.append(presolved_negative())

    def inline_copy_comment(prev, gold, base_):
        out = dict(gold)
        out["report/csvout.py"] = (
            "# uses common\n"
            "def to_csv(rows):\n"
            '    return "\\n".join(",".join(" ".join(str(x).split()).replace(",", ";") for x in r) for r in rows)\n'
        )
        out["report/jsonout.py"] = (
            "import json\n# uses common\n"
            "def to_json(rows):\n"
            '    return json.dumps([[" ".join(str(x).split()).replace(",", ";") for x in r] for r in rows], '
            "ensure_ascii=False)\n"
        )
        return out

    def common_unused(prev, gold, base_):
        out = dict(prev)
        out["report/common.py"] = COMMON
        out["report/csvout.py"] = prev["report/csvout.py"] + "\n# TODO use common\n"
        out["report/jsonout.py"] = prev["report/jsonout.py"] + "\n# TODO use common\n"
        return out

    def renamed_only(prev, gold, base_):
        out = dict(gold)
        out["report/csvout.py"] = prev["report/csvout.py"].replace("_clean", "_clean2")
        out["report/jsonout.py"] = prev["report/jsonout.py"].replace(
            "_clean", "_clean2"
        )
        return out

    def behaviour_changed(prev, gold, base_):
        out = dict(gold)
        out["report/common.py"] = 'def clean(s):\n    return " ".join(str(s).split())\n'
        return out

    negatives += [
        neg(
            "N2",
            "inline copies + 'uses common' comment (probe P6: old verifier passed)",
            inline_copy_comment,
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N3",
            "common.clean exists but both modules keep their own copy",
            common_unused,
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N3",
            "duplicate kept under a new name",
            renamed_only,
            ("FAIL_HIDDEN_ONLY", "FAIL_PARTIAL_PATHS"),
        ),
        neg(
            "N3",
            "behaviour changed while extracting (comma no longer replaced)",
            behaviour_changed,
            ("FAIL_PUBLIC",),
        ),
    ]
    return check_unique(
        Task(
            id="B4-REF-D",
            type="refactor",
            prompt=prompt,
            base_files=base,
            stages=[stage],
            frozen_paths=FROZEN,
            gold=single_stage(_gold),
            alt_gold=single_stage(_alt),
            negatives=negatives,
            sources=fixtures.sources("dedupe"),
        )
    )
