"""Negative-control builders (spec N1..N14). Each returns a Negative with expected reject codes.

mutate(prev, gold, base) -> end files for the chosen stage, where prev is the stage start state,
gold the reference end of that stage and base the pristine task. Stages after the mutated one
repeat the mutated end (no further model work), so the first failing stage decides the verdict.
"""

from collections.abc import Callable

from .model import Negative

Files = dict[str, str]
Mutate = Callable[[Files, Files, Files], Files]

EXIT0 = "import os\nos._exit(0)\n"
# N15 forgers: candidate code that runs at import time inside the observation process.
# Nothing here may make a check pass: judges and expected values live in the evaluator process.
FORGER_NONCE = """import os, sys, json
_secrets = [str(v) for v in list(os.environ.values()) + list(sys.argv)]
_f = sys._getframe()
while _f is not None:
    _secrets += [str(v)[:200] for v in _f.f_locals.values()]
    _f = _f.f_back
os.write(1, ("HIDDEN_DONE\\t" + "".join(_secrets)[:8] + "\\tH1\\tOK\\t{}\\n").encode())
os.write(1, b"\\nOBS\\t{}\\n")
os._exit(0)
"""
FORGER_FRAMES = """import os, sys, json, re
_code = ""
_f = sys._getframe()
while _f is not None:
    _spec = _f.f_locals.get("spec")
    if isinstance(_spec, dict) and "code" in _spec:
        _code = _spec["code"]
        break
    _f = _f.f_back
_obs = {k: {"ok": True} for k in re.findall(r"rec\\(['\\"]([^'\\"]+)['\\"]", _code)}
_obs["unittest"] = {"tests": 99, "failures": 0, "errors": 0, "skipped": 0,
                    "expected_failures": 0, "unexpected_successes": 0, "tail": ""}
os.write(1, ("\\nOBS\\t" + json.dumps(_obs) + "\\n").encode())
os._exit(0)
"""
FORGER_PATCH_OUTPUT = """import json, os
json.dumps = lambda *a, **k: "{}"
os.write = lambda *a, **k: 0
"""
PATCH_TESTCASE = (
    "import unittest\nunittest.TestCase.run = lambda self, result=None: None\n"
)


def neg(
    id_: str,
    name: str,
    mutate: Mutate,
    expect: tuple[str, ...],
    *,
    stage: int = 0,
    verdict: str = "FAIL",
    start: Callable | None = None,
    report: Callable | None = None,
    session: dict | None = None,
) -> Negative:
    def end(base: Files, gold: list[Files]) -> list[Files]:
        prev = base if stage == 0 else gold[stage - 1]
        mutated = mutate(dict(prev), dict(gold[stage]), base)
        return [dict(g) for g in gold[:stage]] + [mutated] * (len(gold) - stage)

    return Negative(id_, name, end, verdict, expect, start, report, session)


def m_noop(prev: Files, gold: Files, base: Files) -> Files:
    return prev


def m_comment(
    paths: tuple[str, ...], text: str = "# fixed boundary 0 100 -1 101 rounding\n"
) -> Mutate:
    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(prev)
        for path in paths:
            out[path] = out.get(path, "") + ("\n" if path in out else "") + text
        return out

    return run


def m_revert(path: str) -> Mutate:
    """Gold, except `path` goes back to its start state (deleted if it did not exist)."""

    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(gold)
        if path in prev:
            out[path] = prev[path]
        else:
            out.pop(path, None)
        return out

    return run


def m_misplaced(paths: tuple[str, ...]) -> Mutate:
    """Gold content written next to every required path (as *_fixed.py); the real paths stay at start."""

    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(prev)
        for path in paths:
            out[path[:-3] + "_fixed.py"] = gold[path]
        return out

    return run


def m_delete(path: str, on_gold: bool = True) -> Mutate:
    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(gold if on_gold else prev)
        out.pop(path, None)
        return out

    return run


def m_add(extra: Files, on_gold: bool = True) -> Mutate:
    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(gold if on_gold else prev)
        out.update(extra)
        return out

    return run


def m_prepend(paths: tuple[str, ...], text: str) -> Mutate:
    """Prepend text to the start state of every path (or create it); the real fix is NOT applied."""

    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(prev)
        for path in paths:
            out[path] = text + out.get(path, "")
        return out

    return run


def m_set(path: str, content: str, on_gold: bool = False) -> Mutate:
    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(gold if on_gold else prev)
        out[path] = content
        return out

    return run


def m_replace_in(path: str, old: str, new: str, on_gold: bool = True) -> Mutate:
    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(gold if on_gold else prev)
        if old not in out[path]:
            raise ValueError(f"negative control anchor missing in {path}: {old!r}")
        out[path] = out[path].replace(old, new)
        return out

    return run


def presolved_start(prev: Files, gold: list[Files]) -> Files:
    return dict(gold[0])


def standard_negatives(
    *,
    required: tuple[str, ...],
    frozen_test: str | None,
    package_init: str | None = None,
    stage: int = 0,
    include_partial: bool = True,
    report_only: bool = False,
) -> list[Negative]:
    """N1 N2 N3 N4 N6 N7 N8 N9 N10 N11 shared across tasks. `required` are the stage's required paths."""
    first = required[0] if required else None
    out = [neg("N1", "no-op (pristine unchanged)", m_noop, ("FAIL_NOOP",), stage=stage)]
    if first is None:
        return out
    out.append(
        neg(
            "N2",
            "comment/keyword only in the required source",
            m_comment(required),
            ("FAIL_COMMENT_ONLY",),
            stage=stage,
        )
    )
    if include_partial and len(required) > 1:
        out.append(
            neg(
                "N3",
                "one required path left unfixed",
                m_revert(required[-1]),
                ("FAIL_PARTIAL_PATHS",),
                stage=stage,
            )
        )
    out.append(
        neg(
            "N8",
            "fix written to a misnamed file, real target untouched",
            m_misplaced(required),
            ("FAIL_MISSING_TARGET",),
            stage=stage,
        )
    )
    out.append(
        neg(
            "N9",
            "required path emptied",
            m_set(first, ""),
            ("FAIL_EMPTY_RESULT",),
            stage=stage,
        )
    )
    out.append(
        neg(
            "N6",
            "exit 0 at import time instead of a fix",
            m_prepend(required, EXIT0),
            ("FAIL_STUB_EXIT0", "FAIL_CHECKER_PATCH"),
            stage=stage,
        )
    )
    out += n15_negatives(required, stage)
    out.append(
        neg(
            "N7",
            "unittest.TestCase.run monkeypatched at import",
            m_prepend(required, PATCH_TESTCASE),
            ("FAIL_CHECKER_PATCH", "FAIL_EMPTY_RESULT", "FAIL_PUBLIC", "FAIL_HIDDEN"),
            stage=stage,
        )
    )
    out.append(
        neg(
            "N7",
            "sitecustomize.py added next to a fix",
            m_add({"sitecustomize.py": EXIT0}),
            ("FAIL_CHECKER_PATCH",),
            stage=stage,
        )
    )
    out.append(
        neg(
            "N7",
            "conftest.py added next to a fix",
            m_add({"conftest.py": "import os\nos._exit(0)\n"}),
            ("FAIL_CHECKER_PATCH",),
            stage=stage,
        )
    )
    out.append(
        neg(
            "N10",
            "git metadata written",
            m_add({".git/hooks/pre-commit": "#!/bin/sh\nexit 0\n"}),
            ("FAIL_FORBIDDEN_WRITE",),
            stage=stage,
        )
    )
    if frozen_test:
        out.append(
            neg(
                "N4",
                "frozen public test deleted",
                m_delete(frozen_test),
                ("FAIL_TEST_TAMPER",),
                stage=stage,
            )
        )
        out.append(
            neg(
                "N4",
                "frozen public test weakened",
                m_comment((frozen_test,), "# weakened\n"),
                ("FAIL_TEST_TAMPER",),
                stage=stage,
            )
        )
    return out


def n15_negatives(required: tuple[str, ...], stage: int = 0) -> list[Negative]:
    """N15: nonce/handshake/observation forging at import time, early exit, output patching."""
    from .codes import FAIL_CODES

    any_fail = tuple(FAIL_CODES)
    return [
        neg(
            "N15",
            "reads env/argv/frames for a secret, prints a legacy handshake and an empty OBS, os._exit(0)",
            m_prepend(required, FORGER_NONCE),
            any_fail,
            stage=stage,
        ),
        neg(
            "N15",
            "reads the probe from the interpreter frames and prints an OBS of {'ok': True} for every key",
            m_prepend(required, FORGER_FRAMES),
            any_fail,
            stage=stage,
        ),
        neg(
            "N15",
            "patches json.dumps and os.write so the observation line is lost",
            m_prepend(required, FORGER_PATCH_OUTPUT),
            ("FAIL_CHECKER_PATCH", "FAIL_STUB_EXIT0"),
            stage=stage,
        ),
    ]


def presolved_negative() -> Negative:
    """N11: the attempt starts from an already-solved workspace (state-only checkers pass it)."""
    return Negative(
        "N11",
        "pre-solved workspace as the start state",
        end=lambda base, gold: [dict(g) for g in gold],
        expect_verdict="INFRA",
        expect_codes=("INFRA_BASE_MISMATCH",),
        start=presolved_start,
    )


# N16+ covers observation tampering independently of task answers. Prefix each
# payload to an otherwise correct end state: a reject must come from integrity,
# not from an unrelated unfixed function.
INTEGRITY_ATTACKS = {
    "N16": (
        "probe helper replacement",
        """import sys
sys.modules['__main__'].jsonable = lambda x: x
""",
    ),
    "N17": (
        "frame locals replacement",
        """import sys
f = sys._getframe()
while f:
    if 'namespace' in f.f_locals:
        f.f_locals['namespace']['attempt'] = lambda fn, *a, **k: {'ok': fn(*(x[:] if isinstance(x, list) else x for x in a), **k)}
    f = f.f_back
""",
    ),
    "N18": (
        "GC referrer helper discovery",
        """import gc
for obj in gc.get_objects():
    if callable(obj):
        for ref in gc.get_referrers(obj):
            if isinstance(ref, dict) and 'attempt' in ref:
                ref['attempt'] = lambda *a, **k: {'ok': True}
""",
    ),
    "N19": ("builtins patch", "import builtins\nbuiltins.list = lambda *a: []\n"),
    "N20": (
        "copy patch through alias",
        "import copy\nhelper = copy\nhelper.deepcopy = lambda value: value\n",
    ),
    "N21": (
        "json patch through alias",
        "import json\nhelper = json\nhelper.dumps = lambda *a, **k: '{}'\n",
    ),
    "N22": ("import hook", "import sys\nsys.meta_path.insert(0, object())\n"),
    "N23": ("sys.modules replacement", "import sys\nsys.modules['copy'] = object()\n"),
    "N24": (
        "atexit forged output",
        "import atexit, os\natexit.register(lambda: os.write(1, b'\\nOBS\\t{}\\n'))\n",
    ),
    "N25": (
        "thread forged output",
        "import threading, os\nthreading.Thread(target=lambda: os.write(1, b'\\nOBS\\t{}\\n')).start()\n",
    ),
    "N26": ("direct fd 1 write", "import os\nos.write(1, b'\\nOBS\\t{}\\n')\n"),
    "N27": ("direct fd 2 write", "import os\nos.write(2, b'\\nOBS\\t{}\\n')\n"),
    "N28": ("os._exit", "import os\nos._exit(0)\n"),
    "N29": ("raw fd via open", "open(1, 'w').write('\\nOBS\\t{}\\n')\n"),
    "N30": (
        "raw fd via fdopen",
        "from os import fdopen\nfdopen(2, 'w').write('\\nOBS\\t{}\\n')\n",
    ),
    "N31": (
        "import-from attrgetter reflection",
        "from operator import attrgetter\nattrgetter('__globals__')(lambda: None)\n",
    ),
    "N32": (
        "loop target module patch",
        "import json\nfor json.dumps in [lambda *a, **k: '{}']:\n    pass\n",
    ),
    "N33": (
        "generated guard binding collision",
        "def shadow(_llme_get=lambda *a: None):\n    return 'x'.upper()\nshadow()\n",
    ),
}


def integrity_negatives(task):
    stage = task.stages[0]
    path = next(iter(stage.required_change_paths), "integrity_attack.py")
    expected = (
        ("FAIL_FORBIDDEN_WRITE",)
        if stage.change_class == "read_only_report"
        else ("FAIL_CHECKER_PATCH",)
    )
    out = []
    for id_, (name, payload) in INTEGRITY_ATTACKS.items():

        def mutate(prev, gold, base, text=payload):
            result = dict(gold)
            result[path] = text + result.get(path, "")
            return result

        reject = (
            ("FAIL_STUB_EXIT0", "FAIL_CHECKER_PATCH")
            if id_ == "N28" and expected == ("FAIL_CHECKER_PATCH",)
            else expected
        )
        out.append(neg(id_, name, mutate, reject))
    return out
