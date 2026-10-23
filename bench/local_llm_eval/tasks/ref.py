"""B4-REF-D refactor (dedupe helper). Fixture: reused `t2-refactor-dedupe`. Spec section 3.6.

The old verifier matched strings ('common' in source), so an inline copy plus a comment passed
(probe P6). H2 substitutes report/common.py with a sentinel and requires both callers to route
through it; H5 forbids a surviving duplicated body.
"""

from .. import codes, fixtures
from ..model import Check, Stage, Task
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

H2 = """
open('report/common.py', 'w').write('def clean(s):\\n    return "SENTINEL"\\n')
from report.csvout import to_csv
from report.jsonout import to_json
assert to_csv([['a', 'b']]) == 'SENTINEL,SENTINEL', to_csv([['a', 'b']])
assert to_json([['a']]) == '[["SENTINEL"]]', to_json([['a']])
"""

H3 = """
import os
for root, _, files in os.walk('.'):
    for name in files:
        if name.endswith('.py'):
            text = open(os.path.join(root, name)).read()
            assert '_clean' not in text, os.path.join(root, name)
"""

H4 = r"""
import json
from report.csvout import to_csv
from report.jsonout import to_json


def ref(s):
    return " ".join(str(s).split()).replace(",", ";")


rows = [
    ["a b", "c,d"],
    ["  x\t\ty  ", "1,2,3", 5],
    [" nbsp ", None, 3.5],
    ["한국어  공백", "a\nb"],
    [],
]
assert to_csv(rows) == "\n".join(",".join(ref(c) for c in r) for r in rows)
assert to_json(rows) == json.dumps([[ref(c) for c in r] for r in rows], ensure_ascii=False)
"""

H5 = """
import ast
import os
seen = {}
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d != 'tests']
    for name in files:
        if not name.endswith('.py'):
            continue
        path = os.path.join(root, name)
        for node in ast.walk(ast.parse(open(path).read())):
            if isinstance(node, ast.FunctionDef) and node.body:
                key = ast.dump(ast.Module(body=node.body, type_ignores=[]))
                assert key not in seen, 'duplicate body: %s and %s:%s' % (seen[key], path, node.name)
                seen[key] = '%s:%s' % (path, node.name)
"""

H7 = """
import inspect
from report.csvout import to_csv
from report.jsonout import to_json
assert list(inspect.signature(to_csv).parameters) == ['rows']
assert list(inspect.signature(to_json).parameters) == ['rows']
"""


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
        Check(
            "H1",
            codes.KIND_DELTA,
            "R1",
            snippet="from report.common import clean\nassert clean('  a,  b ') == 'a; b'\n",
        ),
        Check("H2", codes.KIND_DELTA, "R2", snippet=H2),
        Check("H3", codes.KIND_DELTA, "R3", snippet=H3),
        Check("H4", codes.KIND_INVARIANT, "R4", snippet=H4),
        Check("H5", codes.KIND_DELTA, "R5", snippet=H5),
        frozen_unchanged("H6", FROZEN, "R6"),
        Check("H7", codes.KIND_INVARIANT, "R7", snippet=H7),
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
