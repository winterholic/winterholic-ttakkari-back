"""Run reason and guard mutants against the E0 unit tests in isolated copies.

Usage:
    .venv/bin/python kb-eval/mutation/run_mutation.py [--rev REV] [--out FILE]
        [--workers N] [--only ID[,ID...]]

The working tree is never edited. For each mutant the subset src/, docs/, unit_tests/
kb_eval/, kb-eval/ and pyproject.toml is copied to a temporary directory (from
`git archive REV` when --rev is given), one snippet is replaced, and pytest runs
there with PYTHONPATH pointing at the copy. Every mutant gets an empty
PYTHONPYCACHEPREFIX: reusing bytecode across mutants with the same size and
mtime second silently imported the previous mutant (third review, 26/26 vs
25/26). A pytest plugin asserts the imported package is the mutated copy.

Exit status 1 when a mutant marked expect="kill" survives or the unmutated
baseline fails.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUBSET = ["src", "unit_tests/kb_eval", "kb-eval", "docs", "pyproject.toml"]
APPEND = re.compile(r'^(\s*)(reasons|blockers)\.append\("([a-z_]+)"\)\s*$')
LITERAL = re.compile(r'"reasons": \[([^\]]*)\]')
GUARD_PLUGIN = """
import os
from pathlib import Path


def pytest_configure(config):
    import ttakkari.kb_eval as package

    expected = Path(os.environ["KB_EVAL_MUTANT_ROOT"]).resolve()
    actual = Path(package.__file__).resolve()
    if expected not in actual.parents:
        raise RuntimeError(f"mutant isolation broken: imported {actual}")
"""


def export(rev, target):
    if rev:
        archive = subprocess.run(
            ["git", "archive", rev, *SUBSET], cwd=ROOT, capture_output=True, check=True
        ).stdout
        subprocess.run(["tar", "-x", "-C", str(target)], input=archive, check=True)
    else:
        for item in SUBSET:
            src = ROOT / item
            if src.is_dir():
                shutil.copytree(
                    src,
                    target / item,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                shutil.copy2(src, target / item)
    (target / "_mutation_guard.py").write_text(GUARD_PLUGIN)


def reason_mutants(base):
    found = []
    for path in sorted((base / "src/ttakkari/kb_eval").glob("*.py")):
        rel = str(path.relative_to(base))
        lines = path.read_text().splitlines(keepends=True)
        for n, line in enumerate(lines):
            hit = APPEND.match(line)
            if hit:
                found.append(
                    {
                        "id": f"R:{path.name}:{hit.group(3)}",
                        "file": rel,
                        "line": n + 1,
                        "find": line,
                        "replace": hit.group(1) + "pass\n",
                        "expect": "kill",
                        "note": "reason producer",
                    }
                )
                continue
            for lit in LITERAL.finditer(line):
                items = [s.strip() for s in lit.group(1).split(",") if s.strip()]
                for item in items:
                    rest = ", ".join(i for i in items if i != item)
                    found.append(
                        {
                            "id": f"R:{path.name}:{item.strip(chr(34))}@{n + 1}",
                            "file": rel,
                            "line": n + 1,
                            "find": line,
                            "replace": line[: lit.start()]
                            + '"reasons": ['
                            + rest
                            + "]"
                            + line[lit.end() :],
                            "expect": "kill",
                            "note": "reason literal",
                        }
                    )
    return found


def run_one(base, mutant, py):
    with tempfile.TemporaryDirectory(prefix="kb-e0-mut-") as tmp:
        work = Path(tmp) / "w"
        shutil.copytree(base, work)
        target = work / mutant["file"]
        text = target.read_text()
        if mutant.get("line"):
            lines = text.splitlines(keepends=True)
            if lines[mutant["line"] - 1] != mutant["find"]:
                return {**mutant, "status": "not_applicable"}
            lines[mutant["line"] - 1] = mutant["replace"]
            target.write_text("".join(lines))
        else:
            if text.count(mutant["find"]) != 1:
                return {
                    **mutant,
                    "status": "not_applicable",
                    "occurrences": text.count(mutant["find"]),
                }
            target.write_text(text.replace(mutant["find"], mutant["replace"]))
        status, tail, failed = pytest(work, py, Path(tmp) / "pyc")
        return {
            **mutant,
            "status": "killed" if status else "survived",
            "pytest_tail": tail,
            "first_failure": failed,
        }


def pytest(work, py, pyc):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work),
        "PYTHONPATH": f"{work / 'src'}:{work}",
        "PYTHONPYCACHEPREFIX": str(pyc),
        "PYTHONDONTWRITEBYTECODE": "1",
        "KB_EVAL_MUTANT_ROOT": str(work),
    }
    p = subprocess.run(
        [
            py,
            "-m",
            "pytest",
            "unit_tests/kb_eval",
            "-x",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "_mutation_guard",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = p.stdout.strip().splitlines()
    if "mutant isolation broken" in p.stdout + p.stderr:
        raise RuntimeError(p.stdout + p.stderr)
    failed = next((ln for ln in out if ln.startswith(("FAILED", "ERROR"))), None)
    return p.returncode != 0, out[-1] if out else p.stderr[-300:], failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rev")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--only")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).parent))
    from mutants import GUARDS

    py = str(ROOT / ".venv/bin/python")
    with tempfile.TemporaryDirectory(prefix="kb-e0-base-") as tmp:
        base = Path(tmp) / "base"
        base.mkdir()
        export(args.rev, base)
        base_failed, tail, _ = pytest(base, py, Path(tmp) / "pyc")
        if base_failed:
            print("baseline failed:", tail)
            return 1
        mutants = reason_mutants(base) + [dict(g) for g in GUARDS]
        if args.only:
            keep = set(args.only.split(","))
            mutants = [x for x in mutants if x["id"] in keep]
        with ThreadPoolExecutor(args.workers) as pool:
            results = list(pool.map(lambda x: run_one(base, x, py), mutants))
    for r in results:
        print(
            f"{r['status']:<15} {r['expect']:<10} {r['id']:<45} {r.get('first_failure') or r.get('pytest_tail') or ''}"[
                :200
            ]
        )
    applicable = [r for r in results if r["status"] != "not_applicable"]
    bad = [r for r in applicable if r["status"] == "survived" and r["expect"] == "kill"]
    summary = {
        "rev": args.rev or "working-tree",
        "baseline": tail,
        "total": len(results),
        "not_applicable": len(results) - len(applicable),
        "killed": sum(r["status"] == "killed" for r in applicable),
        "survived": sum(r["status"] == "survived" for r in applicable),
        "survived_expected_kill": [r["id"] for r in bad],
        "survived_equivalent": [
            r["id"]
            for r in applicable
            if r["status"] == "survived" and r["expect"] != "kill"
        ],
        "results": results,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}))
    if args.out:
        args.out.write_text(json.dumps(summary, indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
