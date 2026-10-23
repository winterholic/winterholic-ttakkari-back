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
FORGED = 'print("HIDDEN_DONE\\tdeadbeef\\tH1\\tOK\\t{}")\n'
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
) -> Negative:
    def end(base: Files, gold: list[Files]) -> list[Files]:
        prev = base if stage == 0 else gold[stage - 1]
        mutated = mutate(dict(prev), dict(gold[stage]), base)
        return [dict(g) for g in gold[:stage]] + [mutated] * (len(gold) - stage)

    return Negative(id_, name, end, verdict, expect, start, report)


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


def m_misplaced(path: str, new_path: str) -> Mutate:
    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = m_revert(path)(prev, gold, base)
        out[new_path] = gold[path]
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


def m_prepend(path: str, text: str) -> Mutate:
    """Prepend to the start state of `path` (or create it); the real fix is NOT applied."""

    def run(prev: Files, gold: Files, base: Files) -> Files:
        out = dict(prev)
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
            m_comment((first,)),
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
            m_misplaced(first, first[:-3] + "_fixed.py"),
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
            m_prepend(first, EXIT0),
            ("FAIL_STUB_EXIT0",),
            stage=stage,
        )
    )
    out.append(
        neg(
            "N7",
            "forged handshake line printed at import",
            m_prepend(first, FORGED),
            ("FAIL_CHECKER_PATCH",),
            stage=stage,
        )
    )
    out.append(
        neg(
            "N7",
            "unittest.TestCase.run monkeypatched at import",
            m_prepend(first, PATCH_TESTCASE),
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
