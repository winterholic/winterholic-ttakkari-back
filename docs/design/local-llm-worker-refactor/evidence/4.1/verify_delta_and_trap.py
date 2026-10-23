"""Model-free CPU check of spec-draft claims about pristine base state (WBS 4.1).

Usage: python3 verify_delta_and_trap.py <copied bench/2026-10-18 dir> <stage1 fixture-template dir>

For each claim a check expression runs in a fresh temp dir at the pristine base (and, where a
reference patch exists, at the reference state). `delta` means the check must be red at base and
green at the reference; `invariant` means green at both. Also runs the naive `round()` patch of the
shop fixture to confirm the built-in recovery trap against the public test.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

BENCH = os.path.abspath(sys.argv[1])
SHOP = os.path.abspath(sys.argv[2])
sys.path.insert(0, BENCH)
sys.path.insert(0, os.path.join(BENCH, "evalbase"))
import contextlib
import io

import tasks

with contextlib.redirect_stdout(io.StringIO()):
    # validate_tasks defines SOL; importing it also runs the reference validation, so its stdout is muted
    import validate_tasks

ENV = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)


def materialize(files, root):
    for rel, content in files.items():
        write(root, rel, content)


def run_code(root, code):
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
        env=ENV,
    )
    return proc.returncode == 0


def run_unittest(root):
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
        env=ENV,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-300:]


# (task id, claim id, kind, python check code)
CLAIMS = [
    (
        "t1-bug-median",
        "SGL.H1 even average",
        "delta",
        "from stats import median\nassert median([10, 20]) == 15.0",
    ),
    (
        "t1-bug-median",
        "SGL.H2 empty ValueError",
        "delta",
        "from stats import median\ntry:\n    median([])\nexcept ValueError:\n    pass\nelse:\n    raise SystemExit(1)",
    ),
    (
        "t1-bug-median",
        "SGL.H3 odd",
        "invariant",
        "from stats import median\nassert median([3, 1, 2]) == 2 and median([5]) == 5",
    ),
    (
        "t1-bug-median",
        "SGL.H4 no input mutation",
        "invariant",
        "from stats import median\nxs = [3, 1, 2]\nmedian(xs)\nassert xs == [3, 1, 2]",
    ),
    (
        "t1-bug-median",
        "SGL.H5 negatives",
        "delta",
        "from stats import median\nassert median([-3, -1]) == -2.0",
    ),
    (
        "t1-bug-median",
        "SGL.H7 mean unchanged",
        "invariant",
        "from stats import mean\nassert mean([1, 2, 3]) == 2",
    ),
    (
        "s3-fix-inventory",
        "MUL.H1 total",
        "delta",
        "from inv import Item, total_value\nassert total_value([Item('x', 7, 9)]) == 63",
    ),
    (
        "s3-fix-inventory",
        "MUL.H5 empty report",
        "invariant",
        "from inv import format_report\nassert format_report([]) == 'TOTAL: 0'",
    ),
    (
        "s3-fix-inventory",
        "MUL.H3 qty separator",
        "delta",
        "from inv import Item, format_report\nassert '5 x 2,000' in format_report([Item('alpha', 5, 2000)])",
    ),
    (
        "t2-refactor-dedupe",
        "REF.H1 common.clean exists",
        "delta",
        "from report.common import clean\nassert clean('  a,  b ') == 'a; b'",
    ),
    (
        "t2-refactor-dedupe",
        "REF.H4 behaviour",
        "invariant",
        "from report.csvout import to_csv\nassert to_csv([['a b', 'c,d']]) == 'a b,c;d'",
    ),
]

SOL_KEY = {
    "t1-bug-median": "t1-bug-median",
    "s3-fix-inventory": "s3-fix-inventory",
    "t2-refactor-dedupe": "t2-refactor-dedupe",
}


def task_files(tid):
    return tasks.TASKS[tid]["files"]


results = []
for tid, claim, kind, code in CLAIMS:
    base_dir = tempfile.mkdtemp(prefix="base-")
    ref_dir = tempfile.mkdtemp(prefix="ref-")
    try:
        materialize(task_files(tid), base_dir)
        materialize(task_files(tid), ref_dir)
        validate_tasks.SOL[SOL_KEY[tid]](ref_dir)
        at_base = run_code(base_dir, code)
        at_ref = run_code(ref_dir, code)
    finally:
        shutil.rmtree(base_dir, True)
        shutil.rmtree(ref_dir, True)
    expected = (False, True) if kind == "delta" else (True, True)
    results.append(
        {
            "task": tid,
            "claim": claim,
            "kind": kind,
            "green_at_base": at_base,
            "green_at_reference": at_ref,
            "claim_holds": (at_base, at_ref) == expected,
        }
    )

# t4-long-file: public unittest at base (spec says it passes on the untouched workspace)
d = tempfile.mkdtemp(prefix="long-")
try:
    materialize(task_files("t4-long-file"), d)
    public_ok, _ = run_unittest(d)
    hidden_61 = run_code(d, "import rules\nassert rules.rule_043(61) == 27")
finally:
    shutil.rmtree(d, True)
results.append(
    {
        "task": "t4-long-file",
        "claim": "DBG public unittest green at base; rule_043(61)==27 red at base",
        "kind": "fixture-defect",
        "green_at_base": public_ok,
        "green_at_reference": None,
        "claim_holds": public_ok is True and hidden_61 is False,
    }
)

# shop fixture: naive round() patch must fail the public test (recovery trap)
trap = {}
for label, body in (
    ("pristine", None),
    ("naive_round_patch", "    return round(price_cents * (100 - percent) / 100)\n"),
    (
        "integer_half_up_patch",
        "    return (price_cents * (100 - percent) * 2 + 100) // 200\n",
    ),
):
    d = tempfile.mkdtemp(prefix="shop-")
    try:
        shutil.copytree(
            SHOP,
            d,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        if body is not None:
            pricing = os.path.join(d, "shop", "pricing.py")
            with open(pricing) as fh:
                text = fh.read()
            head = text.split("    discount = ")[0]
            write(d, "shop/pricing.py", head + body)
            cart = os.path.join(d, "shop", "cart.py")
            with open(cart) as fh:
                ctext = fh.read()
            write(
                d,
                "shop/cart.py",
                ctext.replace(
                    "sum(price for _, price, _ in self.items)",
                    "sum(p * q for _, p, q in self.items)",
                ),
            )
        ok, tail = run_unittest(d)
        trap[label] = {
            "public_unittest_green": ok,
            "tail": tail[-120:].replace("\n", " "),
        }
    finally:
        shutil.rmtree(d, True)

print(json.dumps({"claims": results, "shop_trap": trap}, ensure_ascii=False, indent=1))
