"""Selection exposes source integrity without treating familiarity as evidence quality."""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

from ttakkari.db import sessionmaker
from ttakkari.research.rules import rank_methods


def test_link_counts_do_not_rank_familiar_methods_above_otherwise_equal_candidates():
    methods = [
        {
            "id": "new",
            "title": "A new method",
            "conditions": {"backend": "cpu"},
            "cost_seconds": 1,
            "evidence_count": 0,
        },
        {
            "id": "familiar",
            "title": "Z familiar method",
            "conditions": {"backend": "cpu"},
            "cost_seconds": 1,
            "evidence_count": 100,
        },
    ]
    ranked = rank_methods(methods, {"backend": "cpu"}, 60)
    assert [row["id"] for row in ranked] == ["new", "familiar"]
    # Neither adding links nor reversing retrieval order may invent a quality advantage.
    methods[0]["evidence_count"] = 1000
    assert [
        row["id"]
        for row in rank_methods(list(reversed(methods)), {"backend": "cpu"}, 60)
    ] == ["new", "familiar"]
    assert all(row["reason"]["evidence_quality"] == "unassessed" for row in ranked)
    assert all(row["reason"]["information_value"] == "unassessed" for row in ranked)


async def test_selection_carries_versioned_quote_integrity_and_restores_it_without_truth_claims(
    client, auth
):
    async with sessionmaker()() as db:
        identity = list(
            (await db.execute(text("select current_database(), current_user"))).one()
        )
        expected = make_url(os.environ["TTAKKARI_TEST_DB"])
        assert identity[0] == expected.database != "ttakkari"
        if expected.username:
            assert identity[1] == expected.username

    async def post(path, body):
        response = await client.post("/api/research" + path, headers=auth, json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()

    study = await post(
        "/studies",
        {
            "title": "Evidence assessment",
            "goal": "Separate integrity from truth",
            "scope": "personal",
        },
    )
    sid = study["id"]
    quote = "Synthetic report: results hold only on dataset A."
    source_body = "Context before. " + quote + " Context after."
    source_input = {
        "source_key": "selection-evidence-" + uuid.uuid4().hex,
        "title": "Synthetic source",
        "locator": "https://example.invalid/selection-evidence",
        "body": source_body,
    }
    source = await post(f"/studies/{sid}/sources", source_input)
    node = await post(
        f"/studies/{sid}/nodes",
        {
            "kind": "method",
            "title": "Source-bound method",
            "conditions": {"backend": "cpu"},
            "details": {"cost_seconds": 2},
            "unit_id": source["units"][0]["id"],
            "start": source_body.index(quote),
            "quote": quote,
        },
    )
    followup = await post(
        "/studies",
        {"title": "Followup", "goal": "Inspect provenance", "scope": "personal"},
    )
    selection_path = f"/studies/{followup['id']}/select"
    request = {"environment": {"backend": "cpu"}}
    first = await post(selection_path, request)
    candidate = next(row for row in first["candidates"] if row["id"] == node["id"])
    assessment = candidate["reason"]["research_context"]["evidence"]
    assert assessment["status"] == "available"
    assert assessment["quote"] == quote and assessment["quote_integrity"] is True
    assert assessment["quote_sha256"] == hashlib.sha256(quote.encode()).hexdigest()
    assert (
        assessment["source_sha256"] == hashlib.sha256(source_body.encode()).hexdigest()
    )
    assert assessment["source_sha256"] != assessment["quote_sha256"]
    assert assessment["revision_id"] == source["revision"]["id"]
    assert (
        assessment["locator"] == source_input["locator"]
        and assessment["current"] is True
    )
    assert candidate["reason"]["evidence_quality"] == "unassessed"
    assert candidate["reason"]["expected_effect"] == "unknown_until_measured"

    newer = await post(
        f"/studies/{sid}/sources",
        {**source_input, "body": "Synthetic replacement; no confirmed generalization."},
    )
    second = await post(selection_path, request)
    saved = next(row for row in second["candidates"] if row["id"] == node["id"])
    old = saved["reason"]["research_context"]["evidence"]
    assert old == {**assessment, "current": False}
    # A later source does not refute a preserved quote or establish a method's effectiveness.
    assert saved["eligibility"] == candidate["eligibility"] == "candidate"
    assert saved["reason"]["evidence_quality"] == "unassessed"
    assert saved["reason"]["information_value"] == "unassessed"
    restored = await client.get(
        f"/api/research/studies/{followup['id']}/resume", headers=auth
    )
    assert restored.status_code == 200
    events = [
        event
        for event in restored.json()["timeline"]
        if event["kind"] == "selection_recorded"
    ]
    assert len(events) == 2
    assert (
        events[0]["payload"]["candidates"][0]["reason"]["research_context"]["evidence"]
        == assessment
    )
    assert (
        events[1]["payload"]["candidates"][0]["reason"]["research_context"]["evidence"]
        == old
    )
    # A later observation can make older bytes current again; rev_no alone is insufficient.
    from ttakkari.collector.control_models import KbObservation

    async with sessionmaker()() as db:
        now = datetime(2026, 10, 26, tzinfo=UTC)
        for revision, day in [(source, 0), (newer, 1), (source, 2), (newer, -1)]:
            db.add(
                KbObservation(
                    document_id=uuid.UUID(source["revision"]["document_id"]),
                    revision_id=uuid.UUID(revision["revision"]["id"]),
                    envelope_id=uuid.uuid4(),
                    event_time=now + timedelta(days=day),
                )
            )
        await db.commit()
    observed = await post(selection_path, request)
    observed_row = next(
        row for row in observed["candidates"] if row["id"] == node["id"]
    )
    assert observed_row["reason"]["research_context"]["evidence"] == assessment
    later = await client.get(
        f"/api/research/studies/{followup['id']}/resume", headers=auth
    )
    assert later.status_code == 200
    later_events = [
        event
        for event in later.json()["timeline"]
        if event["kind"] == "selection_recorded"
    ]
    assert later_events[:2] == events
    if target := os.environ.get("RESEARCH_EVIDENCE_RECEIPT"):
        Path(target).write_text(
            json.dumps(
                {
                    "scope": "Actual ASGI/Run PG; synthetic source, no model/GPU/UI execution",
                    "identity": identity,
                    "node": node,
                    "source": source,
                    "newer": newer,
                    "first": first,
                    "second": second,
                    "observed": observed,
                    "restored": later.json(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # The normal HTTP scope boundary and an unpersisted invalid reference both hide foreign evidence.
    from ttakkari.research import service
    from ttakkari.research.models import Node

    private = await post(
        "/studies", {"title": "Work source", "goal": "Scope boundary", "scope": "work"}
    )
    secret_quote = "PRIVATE WORK QUOTATION"
    private_source = await post(
        f"/studies/{private['id']}/sources",
        {
            "source_key": "private-" + uuid.uuid4().hex,
            "title": "Private source",
            "body": secret_quote,
            "locator": "https://example.invalid/private-work",
        },
    )
    private_node = await post(
        f"/studies/{private['id']}/nodes",
        {
            "kind": "method",
            "title": "Private method",
            "unit_id": private_source["units"][0]["id"],
            "start": 0,
            "quote": secret_quote,
        },
    )
    async with sessionmaker()() as db:
        invalid = Node(id=uuid.uuid4(), evidence_id=uuid.UUID(private_node["evidence_id"]))
        assert await service.selection_evidence(db, [invalid], "personal") == {}
    protected = await post(selection_path, request)
    assert secret_quote not in json.dumps(protected)
    assert private_node["id"] not in {row["id"] for row in protected["candidates"]}
