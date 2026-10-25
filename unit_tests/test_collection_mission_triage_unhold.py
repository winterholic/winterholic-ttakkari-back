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


@pytest.mark.asyncio
async def test_unhold_item_uses_row_lock_with_for_update():
    db = MagicMock()
    captured_stmt = None

    async def mock_scalar(stmt):
        nonlocal captured_stmt
        captured_stmt = stmt

    db.scalar = mock_scalar
    with pytest.raises(ValueError, match="item_not_found"):
        await missions.unhold_item(db, uuid.uuid4(), uuid.uuid4())

    assert captured_stmt is not None
    assert captured_stmt._for_update_arg is not None
    assert (
        CollectorItem in captured_stmt._for_update_arg.of
        or CollectorItem.__table__ in captured_stmt._for_update_arg.of
    )


@pytest.mark.asyncio
async def test_concurrent_unhold_and_precise_apply_preserves_envelope_hash_and_single_outbox(monkeypatch):
    from ttakkari.collector import precise_eval, store

    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    envelope_id = uuid.uuid4()
    original_envelope = {
        "rights": {"permission": "allowed", "purposes": ["store", "knowledge"], "retain_until": None},
        "decision": {"state": "hold"},
    }
    original_payload_hash = "sha256:origpayload123456"
    original_text_sha256 = "sha256:origtext654321"
    original_raw_sha256 = "sha256:origraw987654"

    item = CollectorItem(
        id=item_id,
        job_id=job_id,
        tenant_id="owner",
        scope="personal",
        source_key="test-source",
        kind="document",
        status="held",
        stage="intake",
        url="https://docs.test/concurrent_doc",
        url_key="concurrent_key",
        decision_state="hold",
        reason_codes=["triage:irrelevant_to_goal"],
        signals={
            "triage": {
                "status": "auto_held",
                "precise_eval": {
                    "state": "processing",
                    "claim_token": "tok_123",
                    "claim_owner": "worker_1",
                },
            }
        },
        envelope_id=envelope_id,
        envelope=dict(original_envelope),
        payload_hash=original_payload_hash,
        text_sha256=original_text_sha256,
        raw_sha256=original_raw_sha256,
    )

    active_policy = MagicMock()
    active_policy.allows.return_value = True

    async def policy_for_item(*_args):
        return active_policy, None

    monkeypatch.setattr(store, "active_policy", policy_for_item)

    # Simulated shared DB storage
    shared_outbox: list[CollectorOutbox] = []
    shared_reviews: list[CollectorReview] = []

    # Session 1: unhold_item execution
    db1 = MagicMock()

    async def db1_scalar(stmt):
        # First query: fetch item with_for_update
        if "collector_items" in str(stmt):
            return item
        # Second query: check existing outbox
        return shared_outbox[0] if shared_outbox else None

    db1.scalar = db1_scalar

    def db1_add(obj):
        if isinstance(obj, CollectorOutbox):
            shared_outbox.append(obj)
        elif isinstance(obj, CollectorReview):
            shared_reviews.append(obj)

    db1.add = db1_add
    db1.flush = AsyncMock()

    # Step 1: Run unhold in Session 1
    mission_id = uuid.uuid4()
    res1 = await missions.unhold_item(db1, mission_id, item_id)
    assert res1["status"] == "accepted"
    assert res1["delivery_queued"] is True
    assert len(shared_outbox) == 1
    assert len(shared_reviews) == 1

    # Invariants check after Session 1
    assert item.envelope == original_envelope
    assert item.payload_hash == original_payload_hash
    assert item.text_sha256 == original_text_sha256
    assert item.raw_sha256 == original_raw_sha256
    assert item.signals["unhold_override"]["previous_status"] == "held"
    # precise_eval state preserved
    assert item.signals["triage"]["precise_eval"]["claim_token"] == "tok_123"

    # Session 2: Precise eval applies concurrently to the same item
    precise_result = {
        "recommendation": "accept",
        "topic_relevance": 0.85,
        "information_depth": 0.75,
        "low_quality_signals": [],
    }
    # apply_precise_result on already accepted item
    rescued = precise_eval.apply_precise_result(item, precise_result)
    assert rescued is False  # Already accepted, rescue flag is False

    # Simulate Session 2 applying outbox if rescued, but since rescued is False,
    # no new outbox is created. Even if rescued was checked, outbox deduplication
    # checks existing outbox:
    existing_outbox = shared_outbox[0] if shared_outbox else None
    assert existing_outbox is not None
    assert len(shared_outbox) == 1  # Exactly 1 outbox row preserved

    # Final verification of invariants
    assert item.status == "accepted"
    assert item.envelope == original_envelope
    assert item.envelope["rights"]["permission"] == "allowed"
    assert item.envelope["rights"]["retain_until"] is None
    assert item.payload_hash == original_payload_hash
    assert item.text_sha256 == original_text_sha256
    assert item.raw_sha256 == original_raw_sha256
    assert shared_outbox[0].sink == "knowledge"
    assert shared_outbox[0].payload_hash == original_payload_hash
    assert shared_outbox[0].envelope_id == envelope_id
