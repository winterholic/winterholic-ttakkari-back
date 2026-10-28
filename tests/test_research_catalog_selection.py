"""Registry selection must work without a legacy method Node."""
from __future__ import annotations

import uuid

from ttakkari.db import sessionmaker
from ttakkari.research.models import (
    Configuration,
    Experiment,
    Method,
    MethodRelation,
    Node,
    Run,
    Study,
)


async def test_catalog_selection_keeps_failure_conditions_scope_and_event(client, auth):
    async with sessionmaker()() as db:
        personal = Study(title="Registry selection", goal="Reuse failures", scope="personal")
        work = Study(title="Private", goal="Do not leak", scope="work")
        db.add_all([personal, work])
        await db.flush()
        method = Method(slug="node-free", name="Node free", name_ko="노트 없는 방법", summary="Registry only",
                        scope="personal", origin="user", lifecycle_status="candidate", parameters=[], metrics=[])
        private = Method(slug="private", name="Private", name_ko="PRIVATE METHOD", summary="PRIVATE", scope="work")
        other = Method(slug="conflict", name="Conflict", name_ko="상충 보고", summary="Conditioned conflict", scope="personal")
        db.add_all([method, private, other])
        await db.flush()
        db.add(MethodRelation(from_method_id=other.id, to_method_id=method.id, relation_type="conflicts_with",
                              scope="personal", conditions={"dataset": "A"}, note="Unverified interaction"))
        for study in [personal, work]:
            exp = Experiment(study_id=study.id, title="Failed", hypothesis="Probe", rationale="Keep raw",
                             adapter="retrieval_eval", method_id=method.id, state="failed", environment={"dataset": "A"})
            db.add(exp)
            await db.flush()
            cfg = Configuration(experiment_id=exp.id, label="one", config_hash=uuid.uuid4().hex, parameters={})
            db.add(cfg)
            await db.flush()
            db.add(Run(experiment_id=exp.id, study_id=study.id, configuration_id=cfg.id,
                       adapter_id="retrieval_eval", status="failed", error_code="KeyError",
                       error_message="PUBLIC FAILURE" if study is personal else "PRIVATE FAILURE",
                       environment_snapshot={"dataset": "A"}, raw_output={"preserved": True}))
        await db.commit()
        sid, mid, private_id = personal.id, method.id, private.id
    results = []
    for environment in [{"dataset": "A"}, {"dataset": "B"}, {}]:
        response = await client.post(f"/api/research/studies/{sid}/select", headers=auth,
            json={"candidate_source": "catalog", "environment": environment})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["candidate_source"] == "catalog" and data["truncated"] is False
        assert str(private_id) not in response.text and "PRIVATE FAILURE" not in response.text
        row = next(r for r in data["candidates"] if r["id"] == str(mid))
        assert row["method_id"] == str(mid) and row["source_node_id"] is None
        assert row["execution_status"] == "hypothesis_stage" and row["is_executable"] is False
        assert row["eligibility"] == "needs_review"  # no cost or applicability declaration
        assert row["reason"]["expected_effect"] == "unknown_until_measured"
        context = row["reason"]["research_context"]
        assert len(context["failures"]) == 1
        assert context["failures"][0]["error_message"] == "PUBLIC FAILURE"
        assert context["failures"][0]["affects_selection"] == (environment != {"dataset": "B"})
        assert context["relations"][0]["affects_selection"] == (environment != {"dataset": "B"})
        results.append(row)
    response = await client.get(f"/api/research/studies/{sid}/resume", headers=auth)
    assert response.status_code == 200
    events = [e for e in response.json()["timeline"] if e["kind"] == "selection_recorded"]
    assert len(events) == 3
    assert all(e["payload"]["request"]["candidate_source"] == "catalog" for e in events)
    assert {r["reason"]["research_context"]["failures"][0]["applicability"]["status"] for r in results} == {"applicable", "incompatible", "unknown"}
    # The omitted source stays backward compatible with research Node selection.
    legacy = await client.post(f"/api/research/studies/{sid}/select", headers=auth, json={})
    assert legacy.status_code == 200 and legacy.json()["candidates"] == []


async def test_catalog_selection_reports_bounded_candidate_and_failure_coverage(client, auth):
    from ttakkari.research import service

    async with sessionmaker()() as db:
        db.add_all([Method(scope="personal", slug=f"bound-{i}", name=f"M{i}", name_ko=f"M{i}", summary="bounded") for i in range(3)])
        await db.commit()
        result = await service.catalog_selection_candidates(db, "personal", "bound-", limit=1)
        assert len(result["methods"]) == 1 and result["truncated"] is True
        assert result["methods"][0]["research_context"]["truncated"] is True


async def test_catalog_relation_preserves_other_source_node_condition(client, auth):
    async with sessionmaker()() as db:
        study = Study(title="Relation endpoint conditions", goal="Keep conjunction", scope="personal")
        db.add(study)
        await db.flush()
        node = Node(study_id=study.id, kind="method", title="Candidate", summary="", epistemic_status="unverified",
                    conditions={"backend": "cpu"}, details={"cost_seconds": 1})
        other_node = Node(study_id=study.id, kind="method", title="Other", summary="", epistemic_status="unverified", conditions={"dataset": "B"})
        db.add_all([node, other_node])
        await db.flush()
        method = Method(scope="personal", slug="candidate", name="C", name_ko="C", summary="x", source_node_id=node.id)
        other = Method(scope="personal", slug="other", name="O", name_ko="O", summary="x", source_node_id=other_node.id)
        db.add_all([method, other])
        await db.flush()
        db.add(MethodRelation(scope="personal", from_method_id=other.id, to_method_id=method.id,
                              relation_type="challenges_under_conditions", conditions={"dataset": "A"}))
        await db.commit()
        sid, mid = study.id, str(method.id)
    response = await client.post(f"/api/research/studies/{sid}/select", headers=auth,
        json={"candidate_source": "catalog", "environment": {"backend": "cpu", "dataset": "A"}})
    assert response.status_code == 200, response.text
    row = next(c for c in response.json()["candidates"] if c["id"] == mid)
    assert row["eligibility"] == "candidate"
    [relation] = row["reason"]["research_context"]["relations"]
    assert relation["other_node"]["conditions"] == {"dataset": "B"}
    assert relation["affects_selection"] is False and relation["applicability"]["status"] == "incompatible"


async def test_catalog_selection_initializes_seed_catalog_for_direct_callers(client, auth):
    created = await client.post('/api/research/studies', headers=auth,
        json={'title': 'Direct catalog review', 'goal': 'No empty catalog'})
    assert created.status_code == 201, created.text
    response = await client.post(f"/api/research/studies/{created.json()['id']}/select", headers=auth,
        json={'candidate_source': 'catalog', 'query': 'kv_cache_quantization'})
    assert response.status_code == 200, response.text
    assert any(c['slug'] == 'kv_cache_quantization' for c in response.json()['candidates'])
