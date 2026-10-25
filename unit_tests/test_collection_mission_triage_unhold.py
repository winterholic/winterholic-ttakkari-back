import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from ttakkari.collector import missions
from ttakkari.collector_models import CollectorItem


@pytest.mark.asyncio
async def test_unhold_item_recovers_held_document_and_enqueues_outbox():
    mission_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    envelope_id = uuid.uuid4()

    item = CollectorItem(
        id=item_id,
        job_id=job_id,
        tenant_id="owner",
        scope="personal",
        source_key="test-source",
        kind="document",
        status="held",
        stage="intake",
        url="https://docs.test/doc1",
        url_key="testkey1",
        decision_state="hold",
        reason_codes=["triage:irrelevant_to_goal"],
        signals={"triage": {"status": "auto_held"}},
        envelope_id=envelope_id,
        payload_hash="hash123",
    )

    db = MagicMock()
    # db.scalar returns item first, then None for existing_outbox
    db.scalar = AsyncMock(side_effect=[item, None])
    db.add = MagicMock()
    db.flush = AsyncMock()

    result = await missions.unhold_item(db, mission_id, item_id)

    assert result["status"] == "accepted"
    assert result["delivery_queued"] is True
    assert "manual_unhold" in result["reasons"]
    assert item.status == "accepted"
    assert item.decision_state == "accept"
    assert item.signals["unhold_override"]["previous_status"] == "held"
    assert db.add.called


@pytest.mark.asyncio
async def test_unhold_item_fails_when_not_found_or_not_held():
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="item_not_found"):
        await missions.unhold_item(db, uuid.uuid4(), uuid.uuid4())

    item = CollectorItem(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        tenant_id="owner",
        scope="personal",
        source_key="test-source",
        kind="document",
        status="accepted",
        stage="intake",
        url="https://docs.test/doc2",
        url_key="testkey2",
    )
    db.scalar = AsyncMock(return_value=item)
    res = await missions.unhold_item(db, uuid.uuid4(), item.id)
    assert res["message"] == "item_not_held"
    assert res["status"] == "accepted"
