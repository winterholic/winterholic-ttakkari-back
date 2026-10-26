"""Check preserved receipts and source continuity; does not execute the product/UI."""

import hashlib
import json
import subprocess
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
ROOT = EVIDENCE.parents[2]
FRONT = ROOT.parent / "front-research-v2"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def check(condition, label):
    if not condition:
        raise AssertionError(label)


def reconcile(evidence=EVIDENCE):
    def read(name):
        return json.loads((evidence / name).read_text())

    d = read("scenario-d-ui-readback-12aaf56.json")
    method, new = d["method"], d["new_method"]
    check(method["lifecycle_status"] == "candidate", "external result remains candidate")
    check(method["evidence_status"] == "unverified", "external method remains unverified")
    check(method["execution_status"] == "adapter_required" and not method["is_executable"], "adapter required")
    check(method["local_measurements"]["measured_run_count"] == 0, "no fabricated local measurements")
    check(new["lifecycle_status"] == "hypothesis" and new["execution_status"] == "hypothesis_stage", "new hypothesis readiness")
    check(d["original_experiment"]["study_id"] != d["followup_experiment"]["study_id"], "different followup study")
    check(d["original_experiment"]["method_id"] == d["followup_experiment"]["method_id"] == method["id"], "same catalog method reused")
    db_method = next(m for m in d["db"]["research_methods"] if m["id"] == method["id"])
    check(db_method["lifecycle_status"] == "candidate" and db_method["evidence_status"] == "unverified", "separate database readback")
    for key in ["original_experiment_run", "followup_experiment_run"]:
        check(d[key]["execution_origin"] == "external_report", "external origin: " + key)
    run = d["followup_experiment_run"]
    for field in ["source_title", "source_url"]:
        check(bool(run[field]), "nonempty source: " + field)
        check(run[field] == run["raw_output"][field] == run["environment_snapshot"][field], "raw/env source: " + field)
        check(bool(run["measurements"]), "source measurements exist")
        check(all(m["meta"][field] == run[field] for m in run["measurements"]), "measurement source: " + field)
    check(any(n["kind"] == "decision" and n["conditions"]["run_id"] == method["promoted_from_run_id"] for n in d["db"]["research_nodes"]), "promotion decision preserved")

    late = read("late-ui-evidence-237f135.json")["fresh_readback"]
    check(late["error_code"] == "KeyError" and late["measurement_count"] == 0, "actual invalid-input failure has no invented metrics")
    check(late["resume_http_status"] == 200 and late["note"] == late["resume_note"], "failure interpretation resumed")
    check(late["note"]["epistemic_status"] == "unverified", "interpretation stays unverified")
    a = read("lead-artifact-ui-564d497.json")
    check(bool(a["artifacts"]), "artifact receipts exist")
    check(all(x["bytes"] == 662 and x["metadata_http"] == x["content_http"] == 200 for x in a["artifacts"]), "artifact receipt status/size")
    check(all(x["sha256"] == a["browser_download"]["sha256"] for x in a["artifacts"]), "artifact/download hash agreement")

    paths = ["src/ttakkari/research", "src/ttakkari/api/research.py", "src/ttakkari/artifacts", "src/ttakkari/tts", "src/ttakkari/host_gpu.py", "alembic", "deploy/generation-contract.json"]
    git(ROOT, "diff", "--exit-code", "e9e3260", "--", *paths)
    check(git(FRONT, "rev-parse", "HEAD") == d["frontend_revision"], "frontend revision matches D receipt")
    changed = set(git(FRONT, "diff", "--name-only", "5a2650f..HEAD").splitlines())
    expected = {"src/pages/research/" + n for n in ["api.ts", "types.ts", "components/ExternalReportForm.tsx", "method-mutation-contract.test.ts", "external-report-form.test.tsx"]}
    check(changed == expected, "frontend delta is the reviewed D fix only")
    names = ["scenario-d-ui-readback-12aaf56.json", "scenario-d-ui-contract-fix.md", "scenario-d-ui-created.txt", "scenario-d-ui-candidate.txt", "scenario-d-ui-external.txt", "scenario-d-create-before.png", "scenario-d-create-after.png", "scenario-d-candidate-reload.png", "scenario-d-source-reload.png", "late-ui-evidence-237f135.json", "lead-artifact-ui-564d497.json", "final-ui-adoption-review-5a2650f.json", "combination-cpu-e9e3260.json"]
    hashes = {name: hashlib.sha256((evidence / name).read_bytes()).hexdigest() for name in names}
    return {"scope": "preserved receipt checks and current source continuity only; no new runtime or UI execution", "backend_head": git(ROOT, "rev-parse", "HEAD"), "frontend_head": git(FRONT, "rev-parse", "HEAD"), "d_candidate_reuse": True, "failure_interpretation": True, "artifact_hash_agreement": True, "evidence_sha256": hashes}


if __name__ == "__main__":
    print(json.dumps(reconcile(), ensure_ascii=False, indent=2))
