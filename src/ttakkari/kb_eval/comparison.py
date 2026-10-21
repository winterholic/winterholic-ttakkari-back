"""Compare sealed run artifacts under explicitly declared treatment factors."""

import json
from pathlib import Path

from .integrity import digest
from .state import canonical, verify_replay
from .statistics import quality_family


def compare_runs(control, candidate, *, treatments):
    artifacts = []
    for directory in (control, candidate):
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_bytes())
        report = json.loads((directory / "report.json").read_bytes())
        deterministic = json.loads((directory / "deterministic.json").read_bytes())
        verify_replay(
            deterministic,
            digest(canonical(manifest)),
            {k: report[k] for k in ("metrics", "anchors", "accounting")},
        )
        if report["state"] != "complete_fixture":
            raise ValueError("comparison_incomplete_run")
        artifacts.append((manifest, report))
    (cm, cr), (tm, tr) = artifacts
    for key in ("corpus", "gold", "policy", "seed", "role", "window"):
        if cm["identity"][key] != tm["identity"][key]:
            raise ValueError("fixed_input_mismatch:" + key)
    allowed = {"build", "model", "analyzer", "quantization", "code", "config"}
    if not set(treatments) <= allowed:
        raise ValueError("invalid_treatment")
    for key in allowed:
        if cm["identity"][key] != tm["identity"][key] and key not in treatments:
            raise ValueError("unregistered_treatment:" + key)
    baseline = {r["id"]: r for r in cr["metrics"]["per_query"]}
    deltas = {"recall": [], "ndcg": []}
    groups, excluded = [], []
    for r in tr["metrics"]["per_query"]:
        b = baseline.get(r["id"])
        if b is None:
            raise ValueError("unpaired_comparison")
        if b["failure"] or r["failure"] or not r["answerable"]:
            excluded.append(
                {
                    "id": r["id"],
                    "control_failure": b["failure"],
                    "candidate_failure": r["failure"],
                    "answerable": r["answerable"],
                }
            )
            continue
        for metric, values in deltas.items():
            values.append(r[metric] - b[metric])
        groups.append(r["group"])
    quality = (
        quality_family(deltas, groups, seed=cm["identity"]["seed"])
        if groups
        else {"status": "inconclusive", "reasons": ["no_valid_pairs"]}
    )
    if cr["metrics"]["invalid"] or tr["metrics"]["invalid"]:
        quality = {"status": "input_invalid", "reasons": ["invalid_required_gold"]}
    elif cr["metrics"]["failed"] or tr["metrics"]["failed"]:
        quality = {"status": "inconclusive", "reasons": ["execution_failures_present"]}
    return {
        "pairs": len(groups),
        "excluded": excluded,
        "total": len(baseline),
        "quality": quality,
        "treatments": treatments,
        "final_quality": "not_authorized",
        "scope": "fixture_regression_only",
        "preregistration": "caller_declared_external_auth_unverified",
        "latency": "unverified",
    }
