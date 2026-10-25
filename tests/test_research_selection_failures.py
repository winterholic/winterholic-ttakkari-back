"""Failure history is condition-bound context, never proof of method ineffectiveness."""

import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from ttakkari.db import sessionmaker
from ttakkari.research import runner
from ttakkari.research.rules import rank_methods


@pytest.mark.parametrize("v2", [False, True])
async def test_real_failure_reappears_in_followup_selection_without_global_exclusion(client, auth, v2):
    async def post(path, body):
        response = await client.post("/api/research" + path, headers=auth, json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()

    async with sessionmaker()() as db:
        identity = list((await db.execute(text("select current_database(), current_user"))).one())
        expected = make_url(os.environ["TTAKKARI_TEST_DB"])
        assert identity[0] == expected.database != "ttakkari"
        if expected.username:
            assert identity[1] == expected.username

    source = await post("/studies", {"title": "Failure source", "goal": "Preserve failed execution", "scope": "personal"})
    method = await post(f"/studies/{source['id']}/nodes", {"kind": "method", "title": "CPU method",
        "conditions": {"backend": "cpu"}, "details": {"cost_seconds": 1}})
    body = {"title": "Invalid corpus execution", "method_ids": [method["id"]],
            "environment": {"dataset": "A"}, "adapter": "retrieval_eval"}
    if v2:
        body.update(adapter_id="retrieval_eval", fixed_inputs={"corpus": [{"id": "a"}],
                    "queries": [{"query": "a", "relevant": ["a"]}], "k": 1})
    else:
        body["config"] = {"corpus": [{"id": "a", "text": "a"}],
                          "queries": [{"query": "a", "relevant": ["missing"]}]}
    exp = await post(f"/studies/{source['id']}/experiments", body)
    if v2:
        async with sessionmaker()() as db:
            await runner.start_experiment_runs(db, uuid.UUID(exp["id"]), background=False)
        fresh = await client.get(f"/api/research/experiments/{exp['id']}", headers=auth)
        assert fresh.status_code == 200, fresh.text
        run = fresh.json()["runs"][0]
        assert run["status"] == "failed", run
        failure_id = run["id"]
    else:
        failed = await post(f"/experiments/{exp['id']}/execute", {})
        assert failed["state"] == "failed" and failed["receipt"]["result"] is None
        failure_id = exp["id"]

    followup = await post("/studies", {"title": "Followup failure review", "goal": "Do not repeat blindly", "scope": "personal"})
    selections = {}
    for dataset, eligibility, fit in [("A", "needs_review", "applicable"), ("B", "candidate", "incompatible"),
                                       (None, "needs_review", "unknown")]:
        environment = {"backend": "cpu", **({"dataset": dataset} if dataset else {})}
        selected = await post(f"/studies/{followup['id']}/select", {"environment": environment})
        row = next(item for item in selected["candidates"] if item["id"] == method["id"])
        assert row["eligibility"] == eligibility
        failures = row["reason"]["research_context"]["failures"]
        assert len(failures) == 1
        failure = failures[0]
        assert failure["run_id" if v2 else "experiment_id"] == failure_id
        assert failure["declared_environment"] == {"dataset": "A"}
        assert failure["applicability"]["status"] == fit
        assert failure["affects_selection"] == (dataset != "B")
        assert failure["causal_status"] == "unassessed"
        assert row["reason"]["expected_effect"] == "unknown_until_measured"
        selections[str(dataset)] = selected
    restored = await client.get(f"/api/research/studies/{followup['id']}/resume", headers=auth)
    assert restored.status_code == 200
    saved = [event for event in restored.json()["timeline"] if event["kind"] == "selection_recorded"]
    assert len(saved) == 3
    assert any(event["payload"]["candidates"][0]["reason"]["research_context"]["failures"][0]["affects_selection"] for event in saved)
    if target := os.environ.get("RESEARCH_FAILURE_RECEIPT"):
        path = Path(target)
        receipts = json.loads(path.read_text()) if path.exists() else []
        receipts.append({"v2": v2, "identity": identity, "method": method, "experiment": exp,
                         "failure_id": failure_id, "selections": selections, "restored": restored.json()})
        path.write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")


def test_conflicting_declared_and_observed_environments_require_review():
    method = {"id": "method", "title": "Method", "conditions": {"backend": "cpu"}, "cost_seconds": 1,
              "research_context": {"failures": [{"declared_environment": {"dataset": "A"},
                                                 "observed_environment": {"dataset": "B"}}]}}
    row = rank_methods([method], {"backend": "cpu", "dataset": "B"}, 60)[0]
    assert row["eligibility"] == "needs_review"
    failure = row["reason"]["research_context"]["failures"][0]
    assert failure["environment_conflicts"] == ["dataset"]
    assert failure["applicability"]["status"] == "unknown"


async def test_failure_query_is_bounded_and_does_not_expose_other_scope(client, auth):
    from ttakkari.research import service
    from ttakkari.research.models import Experiment, Node, Study

    async with sessionmaker()() as db:
        personal = Study(title="Personal", goal="Scope boundary", scope="personal")
        work = Study(title="Work", goal="Private", scope="work")
        db.add_all([personal, work])
        await db.flush()
        method = Node(study_id=personal.id, kind="method", title="Public method", summary="",
                      epistemic_status="unverified", conditions={"backend": "cpu"})
        db.add(method)
        await db.flush()
        # JSON legacy IDs have no FK. Even a stored cross-scope link must not leak.
        private = Experiment(study_id=work.id, title="PRIVATE", hypothesis="x", rationale="x",
                             adapter="retrieval_eval", method_ids=[str(method.id)], state="failed",
                             receipt={"error": "PRIVATE WORK ERROR"})
        db.add(private)
        await db.flush()
        empty = await service.selection_context(db, [method], "personal", limit=1)
        assert empty[str(method.id)] == {"relations": [], "truncated": False}
        for i in range(2):
            db.add(Experiment(study_id=personal.id, title=f"Failure {i}", hypothesis="x", rationale="x",
                              adapter="retrieval_eval", method_ids=[str(method.id)], state="failed",
                              environment={"dataset": "A"}, receipt={"error": f"LOCAL {i}"}))
        await db.flush()
        bounded = await service.selection_context(db, [method], "personal", limit=1)
        item = bounded[str(method.id)]
        assert item["truncated"] is True and len(item["failures"]) == 1
        assert "PRIVATE" not in json.dumps(bounded)
        ranked = rank_methods([{"id": str(method.id), "title": "Method", "conditions": {"backend": "cpu"},
                                "cost_seconds": 1, "research_context": item}], {"backend": "cpu", "dataset": "B"}, 60)
        assert ranked[0]["eligibility"] == "needs_review"  # unseen rows may apply to B


@pytest.mark.parametrize("compared", [False, True])
async def test_registry_source_node_lineage_exposes_failed_run_only_once(compared):
    from ttakkari.research import service
    from ttakkari.research.models import (
        Configuration,
        Experiment,
        Method,
        Node,
        Run,
        Study,
    )

    # Synthetic DB rows isolate identity/lineage; actual executions are covered above.
    async with sessionmaker()() as db:
        study = Study(title="Lineage", goal="Registry identity", scope="personal")
        db.add(study)
        await db.flush()
        node = Node(study_id=study.id, kind="method", title="Source", summary="", epistemic_status="unverified")
        db.add(node)
        await db.flush()
        method = Method(scope="personal", slug="lineage-" + uuid.uuid4().hex, name="Linked", name_ko="Linked",
                        summary="Synthetic fixture", source_node_id=node.id)
        db.add(method)
        await db.flush()
        exp = Experiment(study_id=study.id, title="Failed combination", hypothesis="x", rationale="x",
                         adapter="retrieval_eval", state="failed", environment={"dataset": "A"},
                         method_ids=[str(node.id)], method_id=None if compared else method.id,
                         compared_method_ids=[str(method.id)] if compared else [])
        db.add(exp)
        await db.flush()
        cfg = Configuration(experiment_id=exp.id, label="Synthetic", config_hash="fixture")
        db.add(cfg)
        await db.flush()
        run = Run(experiment_id=exp.id, configuration_id=cfg.id, study_id=study.id, adapter_id="retrieval_eval",
                  status="failed", error_code="fixture_error", raw_output={"preserved": True})
        db.add(run)
        await db.flush()
        context = await service.selection_context(db, [node], "personal")
        failures = context[str(node.id)]["failures"]
        assert len(failures) == 1 and failures[0]["run_id"] == str(run.id)
        # Remove legacy linkage to prove source_node_id reaches primary/compared registry IDs.
        exp.method_ids = []
        await db.flush()
        assert await service.selection_context(db, [node], "personal") == context
        assert run.raw_output == {"preserved": True}
        db.add(Method(scope="personal", slug="another-" + uuid.uuid4().hex, name="New revision", name_ko="New",
                      summary="Synthetic fixture", source_node_id=node.id))
        await db.flush()
        bounded = await service.selection_context(db, [node], "personal", limit=1)
        assert bounded[str(node.id)]["truncated"] is True  # omitted registry identity may own failures
