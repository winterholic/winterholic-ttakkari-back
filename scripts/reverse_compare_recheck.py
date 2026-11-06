#!/usr/bin/env python3
"""Recompute a comparison report from its hashed raw captures using reverse_tasks."""
import argparse
import json
from pathlib import Path

from ttakkari.reverse_tasks import verify_comparisons

parser = argparse.ArgumentParser()
parser.add_argument("--workspace", type=Path, required=True)
parser.add_argument("--report", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--run-id", required=True)
args = parser.parse_args()
data = json.loads(args.report.read_text(encoding="utf-8"))
comparisons = data.get("cases", data.get("comparisons", [data]))
checked = verify_comparisons(args.workspace, comparisons, expected_run_id=args.run_id)
args.output.write_text(json.dumps({"checked": checked, "count": len(checked)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"count": len(checked), "results": {status: sum(c["result"] == status for c in checked)
      for status in ("match", "mismatch", "unverified")}}, ensure_ascii=False))
