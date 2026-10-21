"""Frozen loader/runner contracts and CLI failure paths; no product search claims."""

import json
import subprocess
import sys

import pytest
from test_oracles import cases, predictions, quote, source

from ttakkari.kb_eval import core as e


def fixture_bundle():
    sources = []
    for i in range(1, 11):
        src = source(f"Synthetic document {i}: answer {i}.")
        src.update(
            doc_key=f"d{i}", source_revision_id=f"r{i}", source_uri=f"fixture://d{i}"
        )
        src["raw_utf8"] = src["body"]
        src["passages"] = [{"id": f"d{i}", "start": 0, "end": len(src["body"])}]
        sources.append(src)
    cs = cases()
    for c in cs:
        c.update(query=c["id"], verification="draft", anchors=[])
        if c["answerable"]:
            for doc in c["documents"]:
                src = next(s for s in sources if s["doc_key"] == doc)
                c["anchors"].append(quote(src, f"answer {doc[1:]}"))
    return {
        "schema_version": "kb-e0-v1",
        "kind": "fixture",
        "corpus_id": "oracle-10",
        "corpus_version": "1",
        "sources": sources,
        "cases": cs,
        "links": [],
        "predictions": predictions(),
        "rights": [
            {
                "component": part,
                "original_url": "fixture://authored-e0",
                "version": "1",
                "checked_at": "2026-10-21T00:00:00Z",
                "storage_allowed": True,
                "transform_allowed": True,
                "redistribute_allowed": True,
                "status": "verified",
                "basis": "locally authored synthetic fixture",
                "provenance_kind": "synthetic",
            }
            for part in ("code", "data", "qrels", "corpus")
        ],
        "manifest": {
            "code_sha": "e" * 40,
            "build_id": "fixture-1",
            "model_id": "fixture-no-inference",
            "model_sha256": "0" * 64,
            "analyzer_id": "exact-v1",
            "analyzer_sha256": "1" * 64,
            "quantization": "fixture",
            "query_policy_version": "fixture-v1",
            "seed": 20261021,
            "config": {"N": 40, "window": "none", "role": "query"},
        },
        "policy": {
            "margin": 0.05,
            "alpha": 0.05,
            "bootstrap_replicates": 10000,
            "live_tolerance": {
                "version": "initial-unvalidated",
                "cosine": 0.0001,
                "logit": 0.001,
            },
        },
        "exposure": {
            "benchmark_family": "none-synthetic",
            "split_overlap": "unverified",
            "checkpoint_lineage": "unverified",
        },
        "observations": {
            "kind": "fixture",
            "database_revision": None,
            "events": [
                {
                    "id": "o1",
                    "source_revision_id": "r1",
                    "event_at": "2026-10-21T00:00:00Z",
                    "received_at": "2026-10-21T00:01:00Z",
                }
            ],
        },
    }


def write_bundle(tmp_path, value=None):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(value or fixture_bundle()))
    return path


def test_loader_freezes_manifest_and_refuses_unverified_rights(tmp_path):
    bundle = e.load_bundle(write_bundle(tmp_path))
    assert len(bundle["sources"]) == 10
    assert len(bundle["identity"]["corpus"]) == 64
    data = fixture_bundle()
    data["rights"][2]["storage_allowed"] = None
    with pytest.raises(ValueError, match="rights_unverified:qrels"):
        e.load_bundle(write_bundle(tmp_path, data))


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda b: b["manifest"].update(silent_unknown=1), "extra_forbidden"),
        (lambda b: b["sources"][0].update(body="changed"), "source_hash_mismatch"),
        (
            lambda b: b["cases"][0].update(verification="human_verified"),
            "human_attestation_required",
        ),
        (lambda b: b["policy"].update(margin=0.10), "literal_error"),
        (
            lambda b: b["predictions"].update(
                unknown={"ranked": [], "coverage": "none"}
            ),
            "unknown_prediction",
        ),
    ],
)
def test_loader_negative_controls(tmp_path, mutation, expected):
    data = fixture_bundle()
    mutation(data)
    with pytest.raises(ValueError, match=expected):
        e.load_bundle(write_bundle(tmp_path, data))


def test_runner_resume_integrity_and_exact_replay(tmp_path):
    bundle = e.load_bundle(write_bundle(tmp_path))
    out = tmp_path / "run"
    first = e.run(bundle, out, limit=1)
    assert first["state"] == "interrupted"
    assert first["completed"] == 1
    result = e.run(bundle, out)
    assert result["state"] == "complete_fixture"
    assert result["product_search"] == "unverified"
    assert result["final_quality"] == "not_authorized"
    assert result["execution"]["inference"] == "unexecuted"
    assert result["execution"]["actual_database"] == "unverified"
    assert result["replay"]["status"] == "exact_match"
    assert result["resources"]["gpu_bytes"]["status"] == "unverified"
    assert result["resources"]["rss_bytes"]["value"] > 0
    assert result["accounting"]["total"] == 3
    assert result["accounting"]["completed"] == 3
    assert (
        json.loads((out / "manifest.json").read_text())["identity"]
        == bundle["identity"]
    )
    record_path = out / "cache" / "q1.json"
    corrupt = json.loads(record_path.read_text())
    corrupt["payload"]["ranked"] = []
    record_path.write_text(json.dumps(corrupt))
    with pytest.raises(ValueError, match="cache_corrupt"):
        e.run(bundle, out)


def test_resume_rejects_changed_input_not_just_id(tmp_path):
    path = write_bundle(tmp_path)
    bundle = e.load_bundle(path)
    e.run(bundle, tmp_path / "run", limit=1)
    changed = fixture_bundle()
    changed["cases"][0]["query"] += "!"
    with pytest.raises(ValueError, match="resume_manifest_mismatch"):
        e.run(e.load_bundle(write_bundle(tmp_path, changed)), tmp_path / "run")


def test_public_or_unverified_gold_cannot_be_final_pass(tmp_path):
    data = fixture_bundle()
    data["kind"] = "public"
    with pytest.raises(ValueError, match="rights_review_authentication_required"):
        e.load_bundle(write_bundle(tmp_path, data))
    data["kind"] = "fixture"
    result = e.run(e.load_bundle(write_bundle(tmp_path, data)), tmp_path / "run")
    assert result["final_quality"] == "not_authorized"
    assert all(c["verification"] == "draft" for c in data["cases"])


def test_cli_preserves_machine_readable_failures_and_resume(tmp_path):
    path = write_bundle(tmp_path)
    args = [
        sys.executable,
        "-m",
        "ttakkari.kb_eval",
        "run",
        str(path),
        "--out",
        str(tmp_path / "cli"),
    ]
    first = subprocess.run(
        args + ["--limit", "1"], capture_output=True, text=True, check=False
    )
    assert first.returncode == 3, first.stderr
    assert json.loads(first.stdout)["state"] == "interrupted"
    resumed = subprocess.run(args, capture_output=True, text=True, check=False)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["final_quality"] == "not_authorized"
    data = fixture_bundle()
    data["rights"][0]["storage_allowed"] = False
    write_bundle(tmp_path, data)
    failure = subprocess.run(args, capture_output=True, text=True, check=False)
    assert failure.returncode == 2
    assert json.loads(failure.stdout)["failure"]["category"] == "rights"
    assert (tmp_path / "cli" / "failure.json").exists()


def test_replay_key_covers_prediction_fixture_bytes(tmp_path):
    one = e.load_bundle(write_bundle(tmp_path))
    data = fixture_bundle()
    data["predictions"]["q1"]["ranked"] = ["d1", "d2"]
    two = e.load_bundle(write_bundle(tmp_path, data))
    assert one["identity"]["config"] != two["identity"]["config"]


def test_comparison_requires_frozen_inputs_and_registered_treatment(tmp_path):
    first = e.load_bundle(write_bundle(tmp_path))
    left = tmp_path / "left"
    right = tmp_path / "right"
    e.run(first, left)
    value = fixture_bundle()
    value["manifest"]["build_id"] = "fixture-2"
    value["predictions"]["q1"]["ranked"] = ["d1", "d2"]
    e.run(e.load_bundle(write_bundle(tmp_path, value)), right)
    with pytest.raises(ValueError, match="unregistered_treatment"):
        e.compare_runs(left, right, treatments=[])
    r = e.compare_runs(left, right, treatments=["build", "config"])
    assert r["quality"]["status"] == "inconclusive"
    assert r["final_quality"] == "not_authorized"
    assert r["pairs"] == 2
    value["cases"][0]["query"] += "?"
    e.run(e.load_bundle(write_bundle(tmp_path, value)), tmp_path / "changed")
    with pytest.raises(ValueError, match="fixed_input_mismatch:gold"):
        e.compare_runs(left, tmp_path / "changed", treatments=["build", "config"])


def test_duplicate_body_split_leak_and_holdout_runner_denied(tmp_path):
    value = fixture_bundle()
    value["cases"][1]["split"] = "holdout"
    value["links"] = [["d2", "d6"]]
    with pytest.raises(ValueError, match="split_leakage"):
        e.load_bundle(write_bundle(tmp_path, value))
    value["links"] = []
    with pytest.raises(ValueError, match="holdout_access_denied"):
        e.run(e.load_bundle(write_bundle(tmp_path, value)), tmp_path / "holdout")


def test_invalid_required_anchor_blocks_gate_instead_of_scoring_subset(tmp_path):
    value = fixture_bundle()
    value["cases"][0]["anchors"][0]["quote_sha256"] = "f" * 64
    result = e.run(e.load_bundle(write_bundle(tmp_path, value)), tmp_path / "invalid")
    assert result["quality_gate"]["status"] == "input_invalid"
    assert result["metrics"]["invalid"] == 1
    assert result["metrics"]["total"] == 3
    assert result["accounting"]["completed"] == 3


def test_exact_duplicate_hash_auto_connects_split(tmp_path):
    value = fixture_bundle()
    src = value["sources"][1]
    target = value["sources"][5]
    for key in ("raw_utf8", "body", "raw_sha256", "body_sha256"):
        target[key] = src[key]
    target["passages"][0]["end"] = len(target["body"])
    value["cases"][1]["split"] = "holdout"
    with pytest.raises(ValueError, match="split_leakage"):
        e.load_bundle(write_bundle(tmp_path, value))


def test_cli_comparison_and_schema_export(tmp_path):
    path = write_bundle(tmp_path)
    bundle = e.load_bundle(path)
    e.run(bundle, tmp_path / "a")
    e.run(bundle, tmp_path / "b")
    command = [
        sys.executable,
        "-m",
        "ttakkari.kb_eval",
        "compare",
        str(tmp_path / "a"),
        str(tmp_path / "b"),
        "--out",
        str(tmp_path / "comparison.json"),
    ]
    response = subprocess.run(command, capture_output=True, text=True, check=False)
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["quality"]["status"] == "inconclusive"
    schema = subprocess.run(
        [
            sys.executable,
            "-m",
            "ttakkari.kb_eval",
            "schema",
            "--out",
            str(tmp_path / "schema.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert schema.returncode == 0
    assert (
        json.loads((tmp_path / "schema.json").read_text())["additionalProperties"]
        is False
    )


def test_first_metrics_completion_records_baseline_then_exact_replays(tmp_path):
    bundle = e.load_bundle(write_bundle(tmp_path))
    out = tmp_path / "replay"
    first = e.run(bundle, out)
    assert first["replay"]["metrics"] == "baseline_recorded"
    second = e.run(bundle, out)
    assert second["replay"]["metrics"] == "exact_match"
    (out / "checkpoint.json").write_text("{broken checkpoint")
    rebuilt = e.run(bundle, out)
    assert rebuilt["completed"] == 3
    assert rebuilt["replay"]["metrics"] == "exact_match"


def test_duplicate_json_config_key_is_rejected(tmp_path):
    data = json.dumps(fixture_bundle()).replace('"N": 40', '"N": 20, "N": 40')
    path = tmp_path / "duplicate.json"
    path.write_text(data)
    with pytest.raises(ValueError, match="duplicate_json_key:N"):
        e.load_bundle(path)


def test_comparison_keeps_invalid_input_gate(tmp_path):
    data = fixture_bundle()
    data["cases"][0]["anchors"][0]["quote_sha256"] = "f" * 64
    bundle = e.load_bundle(write_bundle(tmp_path, data))
    e.run(bundle, tmp_path / "bad")
    result = e.compare_runs(tmp_path / "bad", tmp_path / "bad", treatments=[])
    assert result["quality"]["status"] == "input_invalid"
    assert len(result["excluded"]) == 2
    assert result["total"] == 3
