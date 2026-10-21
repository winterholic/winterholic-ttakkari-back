"""Delete one verdict-reason producer at a time; a surviving mutant is a weak test.

Mutants: `reasons.append("X")` / `blockers.append("X")` become `pass`, and a
literal `"reasons": ["X", ...]` drops X. Files are restored after every mutant.
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[3]
src = root / "src/ttakkari/kb_eval"
py = str(root / ".venv/bin/python")
APPEND = re.compile(r'^(\s*)(reasons|blockers)\.append\("([a-z_]+)"\)\s*$')
LITERAL = re.compile(r'"reasons": \[([^\]]*)\]')


def mutants():
    for path in sorted(src.glob("*.py")):
        lines = path.read_text().splitlines(keepends=True)
        for n, line in enumerate(lines):
            m = APPEND.match(line)
            if m:
                yield path, n, m.group(3), m.group(1) + "pass\n"
                continue
            for lit in LITERAL.finditer(line):
                items = [s.strip() for s in lit.group(1).split(",") if s.strip()]
                for item in items:
                    rest = ", ".join(i for i in items if i != item)
                    new = line[: lit.start()] + '"reasons": [' + rest + "]" + line[lit.end() :]
                    yield path, n, item.strip('"'), new


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


before = {p.name: sha(p) for p in src.glob("*.py")}
results = []
for path, n, reason, replacement in mutants():
    original = path.read_text()
    lines = original.splitlines(keepends=True)
    lines[n] = replacement
    try:
        path.write_text("".join(lines))
        p = subprocess.run(
            [py, "-m", "pytest", "unit_tests/kb_eval", "-x", "-q", "-p", "no:cacheprovider"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    finally:
        path.write_text(original)
    tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
    results.append(
        {
            "file": path.name,
            "line": n + 1,
            "reason": reason,
            "killed": p.returncode != 0,
            "pytest_tail": tail,
        }
    )
    print(("KILLED  " if p.returncode else "SURVIVED"), path.name, n + 1, reason, "|", tail, flush=True)
after = {p.name: sha(p) for p in src.glob("*.py")}
assert before == after, "source not restored"
survivors = [r for r in results if not r["killed"]]
summary = {"mutants": len(results), "killed": len(results) - len(survivors), "survivors": survivors, "results": results}
out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("mutation.json")
out.write_text(json.dumps(summary, indent=2))
print(f"mutants={len(results)} killed={len(results) - len(survivors)} survived={len(survivors)}; sources restored")
