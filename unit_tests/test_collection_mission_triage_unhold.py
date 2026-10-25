import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from ttakkari.collector import missions
from ttakkari.collector.control_models import CollectorReview
from ttakkari.collector_models import CollectorItem, CollectorOutbox


@pytest.mark.asyncio
async def test_unhold_item_appends_utility_review_and_enqueues_knowledge_outbox(monkeypatch):
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
        envelope={"rights": {"permission": "allowed", "purposes": ["store", "knowledge"], "retain_until": None}},
        payload_hash="hash123",
        text_sha256="textsha256",
    )

    from ttakkari.collector import store

    active_policy = MagicMock()
    active_policy.allows.return_value = True

    async def policy_for_item(*_args):
        return active_policy, None

    monkeypatch.setattr(store, "active_policy", policy_for_item)
    db = MagicMock()
    # db.scalar returns the held item, then no existing knowledge outbox.
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
    assert result["review"]["meaning"] == "document_utility_review_only"
    assert result["review"]["factuality"] == "unverified"
    added = [call.args[0] for call in db.add.call_args_list]
    review = next(row for row in added if isinstance(row, CollectorReview))
    outbox = next(row for row in added if isinstance(row, CollectorOutbox))
    assert review.decision == "accept" and review.text_sha256 == item.text_sha256
    assert "factuality unverified" in review.reason
    assert outbox.sink == "knowledge" and outbox.payload_hash == item.payload_hash


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


@pytest.mark.asyncio
async def test_unhold_refuses_missing_or_revoked_knowledge_permission(monkeypatch):
    from ttakkari.collector import store

    mission_id, item_id = uuid.uuid4(), uuid.uuid4()
    item = CollectorItem(
        id=item_id, job_id=uuid.uuid4(), tenant_id="owner", scope="personal", source_key="test-source",
        kind="document", status="held", stage="intake", url="https://docs.test/doc3", url_key="testkey3",
        envelope_id=uuid.uuid4(), envelope={"rights": {"permission": "allowed", "purposes": ["store"],
                                                        "retain_until": None}},
        payload_hash="hash123", text_sha256="textsha256",
    )

    async def policy_for_item(*_args):
        return MagicMock(allows=lambda permission: permission == "store"), None

    monkeypatch.setattr(store, "active_policy", policy_for_item)
    db = MagicMock()
    db.scalar = AsyncMock(return_value=item)
    db.add = MagicMock()

    with pytest.raises(ValueError, match="knowledge_permission_missing"):
        await missions.unhold_item(db, mission_id, item_id)
    assert item.status == "held"
    assert not db.add.called
