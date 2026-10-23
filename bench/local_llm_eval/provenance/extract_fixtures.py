"""One-off provenance script: dump reused 2026-10-18 dev fixtures to JSON (read-only on the sources).

Usage: python3 extract_fixtures.py <bench/2026-10-18 dir> <bench/2026-10-18-longctx dir> <stage1 dir> <out dir>

Each output file records source paths and sha256 so the committed data can be traced back. The sources
live in qwen-local-harness / qwen-worker-stage1 (outside this repo). Nothing here runs a model.
"""

import contextlib
import hashlib
import io
import json
import os
import sys

bench, longctx, stage1, out = (os.path.abspath(p) for p in sys.argv[1:5])
sys.path.insert(0, bench)
sys.path.insert(0, os.path.join(bench, "evalbase"))
sys.path.insert(0, longctx)
with contextlib.redirect_stdout(io.StringIO()):
    import longtasks
    import tasks


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def dump(key, files, sources, prompt=None):
    data = {"sources": {p: sha(p) for p in sources}, "files": files}
    if prompt is not None:
        data["original_prompt"] = prompt
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, key + ".json"), "w") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


tasks_py = os.path.join(bench, "tasks.py")
stage3_py = os.path.join(bench, "evalbase", "stage3.py")
long_py = os.path.join(longctx, "longtasks.py")
for key, tid, srcs in (
    ("median", "t1-bug-median", [tasks_py]),
    ("dedupe", "t2-refactor-dedupe", [tasks_py]),
    ("discount", "t3-write-tests", [tasks_py]),
    ("long_rules", "t4-long-file", [tasks_py]),
    ("inventory", "s3-fix-inventory", [tasks_py, stage3_py]),
    ("slugify", "s3-feature-slugify", [tasks_py, stage3_py]),
    ("billing", "s3-rename-calc-tax", [tasks_py, stage3_py]),
):
    dump(key, tasks.TASKS[tid]["files"], srcs, tasks.TASKS[tid]["prompt"])

dump("ledger", longtasks.make_files(26), [long_py], longtasks.PROMPT)

shop = {}
template = os.path.join(stage1, "fixture-template")
for root, dirs, names in os.walk(template):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
    for name in names:
        path = os.path.join(root, name)
        with open(path) as fh:
            shop[os.path.relpath(path, template)] = fh.read()
with open(os.path.join(stage1, "task.txt")) as fh:
    shop_prompt = fh.read()
dump(
    "shop",
    shop,
    [os.path.join(stage1, "task.txt"), os.path.join(stage1, "verify_fixture.py")],
    shop_prompt,
)
print("ok", sorted(os.listdir(out)))
