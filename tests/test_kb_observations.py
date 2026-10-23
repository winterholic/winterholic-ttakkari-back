import uuid
from datetime import UTC, datetime, timedelta

from ttakkari import kb_library as lib
from ttakkari.collector.control_models import KbObservation
from ttakkari.db import sessionmaker
from ttakkari.kb_store import ingest_source, segment_revision


async def test_observation_time_handles_a_b_a_and_late_old_snapshot():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    async with sessionmaker()() as db:
        async def ingest(body, day):
            result = await ingest_source(db, source_key="observation:test", data=body.encode(),
                                         title="観測", normalized_body=body)
            await segment_revision(db, result.revision_id)
            db.add(KbObservation(document_id=result.document_id, revision_id=result.revision_id,
                                  envelope_id=uuid.uuid4(), event_time=start + timedelta(days=day)))
            await db.flush()
            return result
        a = await ingest("A snapshot alpha", 1)
        b = await ingest("B snapshot beta", 2)
        again = await ingest("A snapshot alpha", 3)
        assert again.revision_id == a.revision_id and again.rev_no == 1
        assert (await lib._latest_revision(db, a.document_id)).id == a.revision_id
        await ingest("B snapshot beta", 0)  # received later, observed earlier
        assert (await lib._latest_revision(db, a.document_id)).id == a.revision_id
        assert b.rev_no == 2
        hits = await lib.search_passages(db, query="alpha", limit=10, offset=0,
                                         document_id=None, current_only=True)
        assert len(hits["items"]) == 1 and hits["items"][0]["revision"]["current"]
        assert (await lib.search_passages(db, query="beta", limit=10, offset=0,
                                          document_id=None, current_only=True))["items"] == []
