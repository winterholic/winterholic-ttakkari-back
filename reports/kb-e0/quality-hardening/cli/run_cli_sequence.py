"""Subprocess CLI regression for the review fixes. Fixture inputs only."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[4]
here = Path(__file__).resolve().parent
py = str(root / ".venv/bin/python")
fixture = root / "kb-eval/fixtures/oracle.json"
receipt = {}


def cli(name, *args):
    p = subprocess.run([py, "-m", "ttakkari.kb_eval", *args], cwd=root, capture_output=True, text=True)
    (here / (name + ".stdout")).write_text(p.stdout)
    (here / (name + ".stderr")).write_text(p.stderr)
    out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    receipt[name] = {"exit": p.returncode, "state": out.get("state"), "completed": out.get("completed"),
                     "failure": out.get("failure"), "replay": out.get("replay"),
                     "rights_authorization": out.get("rights_authorization", {}).get("status")
                     if isinstance(out.get("rights_authorization"), dict) else None}
    return out


for d in ("resume-run", "replay-run", "stale-manifest", "partial-cache", "rights-denied", "fake-public", "split-leak"):
    shutil.rmtree(here / d, ignore_errors=True)
cli("interrupt", "run", str(fixture), "--out", str(here / "resume-run"), "--limit", "1")
cli("resume", "run", str(fixture), "--out", str(here / "resume-run"))
cli("exact_replay", "run", str(fixture), "--out", str(here / "resume-run"))
cli("stale_interrupt", "run", str(fixture), "--out", str(here / "stale-manifest"), "--limit", "1")
stale = json.loads(fixture.read_text())
stale["manifest"]["seed"] = stale["manifest"]["seed"] + 1
(here / "stale-bundle.json").write_text(json.dumps(stale))
cli("stale_manifest", "run", str(here / "stale-bundle.json"), "--out", str(here / "stale-manifest"))
cli("partial_seed", "run", str(fixture), "--out", str(here / "partial-cache"))
(here / "partial-cache/cache/q1.json").write_text("{")
cli("partial_cache", "run", str(fixture), "--out", str(here / "partial-cache"))
cli("rights_denied", "run", str(root / "kb-eval/fixtures/rights-denied.json"), "--out", str(here / "rights-denied"))
cli("fake_public_rights", "run", str(root / "reports/kb-e0/review-fixes/fake-public-rights-input.json"), "--out", str(here / "fake-public"))
cli("split_leak", "run", str(root / "reports/kb-e0/review-fixes/split-leak-input.json"), "--out", str(here / "split-leak"))
(here / "receipt.json").write_text(json.dumps({"python": sys.version.split()[0], "commands": receipt}, indent=2, sort_keys=True))
print(json.dumps(receipt, indent=1, sort_keys=True))
