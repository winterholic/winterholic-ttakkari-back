"""CPU-only negative-control probe of the EXISTING 2026-10-18 verifiers (no model, no network, no server).

Run from a /tmp copy of qwen-local-harness/bench so the source repo is not written to:
  PYTHONDONTWRITEBYTECODE=1 python3 verifier_probe_existing.py <path-to-copied-bench/2026-10-18>

Each probe builds a fixture in a fresh temp dir, applies a deliberately non-solving edit, and
records what the existing verifier says. A probe that returns hidden_ok and unittest_ok both true
is a false positive of the existing verifier (not a model result).
"""

import json
import os
import shutil
import sys
import tempfile

BENCH = os.path.abspath(sys.argv[1])
sys.path.insert(0, BENCH)
sys.path.insert(0, os.path.join(BENCH, "evalbase"))
import tasks

T = tasks.TASKS


def mat(t, d):
    for rel, c in t["files"].items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(c)


def run(tid, mutate, final=""):
    d = tempfile.mkdtemp(prefix="probe-")
    try:
        mat(T[tid], d)
        mutate(d)
        r = T[tid]["hidden"](d, final)
        r = {k: r[k] for k in ("hidden_ok", "unittest_ok")} | {
            "hidden_out": str(r.get("hidden_out"))[-160:]
        }
        return r
    finally:
        shutil.rmtree(d, True)


def write(d, rel, c):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(c)


def noop(d):
    pass


probes = []


def probe(pid, tid, desc, mutate, final=""):
    r = run(tid, mutate, final)
    verdict = "FALSE_POSITIVE" if (r["hidden_ok"] and r["unittest_ok"]) else "rejected"
    probes.append({"probe": pid, "task": tid, "desc": desc, "verdict": verdict, **r})


# P1 no-op baselines (must be rejected)
for tid in (
    "s3-fix-inventory",
    "t1-bug-median",
    "t2-refactor-dedupe",
    "t4-long-file",
    "t3-write-tests",
):
    probe("P1-noop", tid, "untouched workspace", noop)

# P2 exit0 stub: package __init__ exits 0 at import time (no real fix)
probe(
    "P2-exit0-init",
    "s3-fix-inventory",
    "inv/__init__.py calls os._exit(0) on import",
    lambda d: write(d, "inv/__init__.py", "import os\nos._exit(0)\n"),
)

# P3 sitecustomize/checker hijack in cwd
probe(
    "P3-sitecustomize",
    "s3-fix-inventory",
    "sitecustomize.py in workspace exits 0",
    lambda d: write(d, "sitecustomize.py", "import os\nos._exit(0)\n"),
)

# P4 test-writing: tests that always fail kill every mutant (and fail on gold)
probe(
    "P4-alwaysfail-tests",
    "t3-write-tests",
    "3 tests that self.fail() unconditionally",
    lambda d: write(
        d,
        "tests/test_discount.py",
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_a(self): self.fail()\n    def test_b(self): self.fail()\n    def test_c(self): self.fail()\n",
    ),
)

# P5 test-writing: skipped tests
probe(
    "P5-skip-tests",
    "t3-write-tests",
    "3 tests decorated with unittest.skip",
    lambda d: write(
        d,
        "tests/test_discount.py",
        "import unittest\nclass T(unittest.TestCase):\n"
        "    @unittest.skip('x')\n    def test_a(self): pass\n    @unittest.skip('x')\n    def test_b(self): pass\n    @unittest.skip('x')\n    def test_c(self): pass\n",
    ),
)


# P6 refactor-dedupe: common.clean exists but modules keep an inline copy; 'common' only in a comment
def dedupe_stub(d):
    write(
        d,
        "report/common.py",
        'def clean(s):\n    return " ".join(str(s).split()).replace(",", ";")\n',
    )
    write(
        d,
        "report/csvout.py",
        '# uses common\ndef to_csv(rows):\n    return "\\n".join(",".join(" ".join(str(x).split()).replace(",", ";") for x in r) for r in rows)\n',
    )
    write(
        d,
        "report/jsonout.py",
        'import json\n# uses common\ndef to_json(rows):\n    return json.dumps([[" ".join(str(x).split()).replace(",", ";") for x in r] for r in rows], ensure_ascii=False)\n',
    )


probe(
    "P6-dedupe-inline-copy",
    "t2-refactor-dedupe",
    "inline copies, 'common' only in comment, common.clean unused",
    dedupe_stub,
)

# P7 explain-review: keyword-only answer, file untouched (keyword regex verifier)
probe(
    "P7-explain-keywords",
    "t3-explain-review",
    "final text only repeats keywords, no real analysis",
    noop,
    final="기본 인자 가변 공유 마지막 나머지 누락 키워드",
)
probe(
    "P7b-explain-keywords-contiguous",
    "t3-explain-review",
    "keyword list incl. one 4+ hangul run, no analysis",
    noop,
    final="키워드나열: 기본 인자 가변 공유 마지막 나머지 누락",
)

# P8 shell-count: empty/missing handled? write an empty todo_report.txt where src has TODOs
probe(
    "P8-shell-empty",
    "t3-shell-count",
    "empty todo_report.txt",
    lambda d: write(d, "todo_report.txt", ""),
)

# P9 korean-docs: docstrings but one function body replaced by a stub that still matches the weak behavior probe
probe(
    "P9-korean-noop",
    "t3-korean-docs",
    "untouched calc.py, final report in Korean",
    noop,
    final="했습니다.",
)

out = {"bench": BENCH, "n": len(probes), "probes": probes}
print(json.dumps(out, ensure_ascii=False, indent=1))
