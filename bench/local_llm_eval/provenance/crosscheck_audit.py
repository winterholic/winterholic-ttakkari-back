"""Recompute per-profile costs from the audit manifests with the ledger code and print JSON.

Usage: python3 -m local_llm_eval.provenance.crosscheck_audit <evidence-audit dir>
"""

import json
import sys
from pathlib import Path

from ..ledger import summarize
from .audit_adapter import load_attempt_rows, load_batch_elapsed

audit = Path(sys.argv[1])
rows = load_attempt_rows(audit / "attempt_manifest.csv")
summary = summarize(
    rows,
    batch_elapsed=load_batch_elapsed(audit / "config_summary.csv"),
    require_comparable=False,
)
print(
    json.dumps(
        {
            "attempt_rows": len(rows),
            "zero_wall_rows_total": sum(v["zero_wall_rows"] for v in summary.values()),
            "profiles": summary,
        },
        indent=1,
        sort_keys=True,
    )
)
