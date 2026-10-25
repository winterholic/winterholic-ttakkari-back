"""API/PG acceptance: source refresh preserves citations and unknown conditions."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbRevision, KbSource


async def test_source_refresh_retains_old_quotes_and_uncertain_method_conditions(client, auth):
    async with sessionmaker()() as db:
        identity = (await db.execute(text("select current_database(), current_user"))).one()
        expected = make_url(os.environ["TTAKKARI_TEST_DB"])
        assert expected.database != "ttakkari"
        assert identity[0] == expected.database
        if expected.username is not None:
            assert identity[1] == expected.username

    async def post(path, body, expected=201):
        response = await client.post(path, headers=auth, json=body)
        assert response.status_code == expected, response.text
        return response.json()

    study = await post("/api/research/studies", {
        "title": "Source refresh acceptance", "goal": "Retain conflicting reports and applicability uncertainty",
        "scope": "personal", "environment": {"backend": "metal"},
    })
    sid = study["id"]
    key = "refresh-acceptance-" + uuid.uuid4().hex
    source_path = f"/api/research/studies/{sid}/sources"
    quote1 = "Recall improved only on dataset A; the cause is unknown."
    quote2 = "No improvement was found on dataset B; generalization remains unverified."
    first_body = {"source_key": key, "title": "Synthetic versioned research report",
                  "locator": "https://example.invalid/research-refresh", "body": quote1}
    first = await post(source_path, first_body)
    conditions = {"backend": ["metal"], "dataset": ["A"]}
    method = await post(f"/api/research/studies/{sid}/nodes", {
        "kind": "method", "title": "Version-bound retrieval ordering", "summary": quote1,
        "conditions": conditions, "details": {"cost_seconds": 3},
        "unit_id": first["units"][0]["id"], "start": 0, "quote": quote1,
    })
    response = await client.get(f"/api/research/studies/{sid}/resume", headers=auth)
    assert response.status_code == 200, response.text
    before = response.json()
    assert before["evidence"][0]["active"] is True

    second = await post(source_path, {**first_body, "body": quote2})
    replay = await post(source_path, {**first_body, "body": quote2})
    assert first["revision"]["rev_no"] == 1 and first["revision"]["created"] is True
    assert second["revision"]["rev_no"] == 2 and second["revision"]["created"] is True
    assert second["revision"]["document_id"] == first["revision"]["document_id"]
    assert second["revision"]["id"] != first["revision"]["id"]
    assert replay["revision"]["id"] == second["revision"]["id"]
    assert replay["revision"]["created"] is False
    assert first["extraction"] == second["extraction"] == "pending_review"
    counterclaim = await post(f"/api/research/studies/{sid}/nodes", {
        "kind": "claim", "title": "Conflicting result under a changed condition", "summary": quote2,
        "conditions": {"dataset": ["B"]}, "unit_id": second["units"][0]["id"],
        "start": 0, "quote": quote2,
    })
    await post(f"/api/research/studies/{sid}/relations", {
        "from_id": counterclaim["id"], "to_id": method["id"],
        "predicate": "challenges_under_conditions", "rationale": "Different dataset; no universal causal claim",
        "conditions": {"dataset": "B"},
    })
    await post(f"/api/research/studies/{sid}/nodes", {
        "kind": "claim", "title": "Wrong version must not validate the earlier quote",
        "unit_id": second["units"][0]["id"], "start": 0, "quote": quote1,
    }, expected=422)

    response = await client.get(f"/api/research/studies/{sid}/resume", headers=auth)
    assert response.status_code == 200, response.text
    after = response.json()
    assert next(n for n in after["nodes"] if n["id"] == method["id"]) == before["nodes"][0]
    evidence = {e["revision_id"]: e for e in after["evidence"]}
    for imported, quote, active in ((first, quote1, False), (second, quote2, True)):
        saved = evidence[imported["revision"]["id"]]
        assert saved["quote"] == quote and saved["active"] is active
        assert saved["sha256"] == hashlib.sha256(quote.encode()).hexdigest()
    assert len(after["nodes"]) == 2 and len(after["relations"]) == 1
    assert all(n["epistemic_status"] == "source_reported" for n in after["nodes"])
    assert after["relations"][0]["conditions"] == {"dataset": "B"}

    followup = await post("/api/research/studies", {
        "title": "Reuse without inventing a dataset", "goal": "Investigate applicability",
        "scope": "personal", "environment": {"backend": "metal"},
    })
    selection_path = f"/api/research/studies/{followup['id']}/select"
    selections = {}
    for label, environment, expected in (
        ("unknown_dataset", {"backend": "metal"}, "needs_review"),
        ("matching_dataset", {"backend": "metal", "dataset": "A"}, "candidate"),
        ("changed_dataset", {"backend": "metal", "dataset": "B"}, "excluded"),
    ):
        selection = await post(selection_path, {"environment": environment, "budget_seconds": 10}, expected=200)
        candidate = next(c for c in selection["candidates"] if c["id"] == method["id"])
        assert candidate["eligibility"] == expected
        assert candidate["conditions"] == conditions
        assert candidate["reason"]["expected_effect"] == "unknown_until_measured"
        if label == "unknown_dataset":
            assert candidate["applicability"]["missing"] == ["dataset"]
        selections[label] = selection

    async with sessionmaker()() as db:
        revisions = (await db.scalars(select(KbRevision).where(
            KbRevision.document_id == uuid.UUID(first["revision"]["document_id"])
        ).order_by(KbRevision.rev_no))).all()
        sources = (await db.scalars(select(KbSource).where(
            KbSource.source_key == f"research:personal:{key}"
        ))).all()
        assert [r.rev_no for r in revisions] == [1, 2]
        assert [r.body for r in revisions] == [quote1, quote2]
        assert {s.sha256 for s in sources} == {
            hashlib.sha256(quote1.encode()).hexdigest(), hashlib.sha256(quote2.encode()).hexdigest()
        }

    output = os.environ.get("RESEARCH_SOURCE_REFRESH_RECEIPT")
    if output:
        root = Path(__file__).resolve().parents[1]
        source_files = ["src/ttakkari/research/service.py", "src/ttakkari/research/rules.py",
                        "src/ttakkari/api/research.py", "src/ttakkari/kb_store.py"]
        Path(output).write_text(json.dumps({
            "environment": "Codex command sandbox; actual ASGI API and Run PostgreSQL; synthetic source texts",
            "identity": list(identity), "first": first, "second": second, "replay": replay,
            "before": before, "after": after, "selections": selections,
            "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in source_files},
            "limitations": ["No network recollection or Collector Platform auto refresh executed.",
                            "No automatic evidence extraction, truth verification or calibrated method selection.",
                            "No operating MCP, browser, GPU or research quality improvement claim."],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
