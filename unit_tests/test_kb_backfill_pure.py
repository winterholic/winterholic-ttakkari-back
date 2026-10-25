"""Pure unit tests for KB v3 job backfill (KB3-103)."""
import argparse
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ttakkari.cli import build_parser
from ttakkari.kb_backfill import (
    BackfillAlreadyRunning,
    JobBackfillSummary,
    run_enqueue_jobs_backfill,
)
from ttakkari.kb_models import KbDocument, KbRevision
from ttakkari.models import utcnow


def test_job_backfill_summary_as_dict():
    s = JobBackfillSummary(run_id="test-123", apply=True, examined=10, enqueued=5, skipped_has_job=3, skipped_hidden=2)
    d = s.as_dict()
    assert d["run_id"] == "test-123"
    assert d["apply"] is True
    assert d["examined"] == 10
    assert d["enqueued"] == 5
    assert d["skipped_has_job"] == 3
    assert d["skipped_hidden"] == 2
    assert d["status"] == "completed"


def test_cli_parser_kb_enqueue_jobs_and_flag():
    parser = build_parser()

    # kb-enqueue-jobs
    args = parser.parse_args(["kb-enqueue-jobs", "--apply", "--batch-size", "25", "--include-hidden"])
    assert args.apply is True
    assert args.batch_size == 25
    assert args.include_hidden is True

    # kb-backfill --enqueue-jobs-only
    args2 = parser.parse_args(["kb-backfill", "--apply", "--enqueue-jobs-only"])
    assert args2.apply is True
    assert args2.enqueue_jobs_only is True


@pytest.mark.asyncio
async def test_run_enqueue_jobs_backfill_mock_db():
    doc1 = KbDocument(id=uuid.uuid4(), doc_key="doc1", title="문서1", lang="ko", created_at=utcnow())
    doc2 = KbDocument(id=uuid.uuid4(), doc_key="doc2", title="문서2", lang="ko", created_at=utcnow())
    rev1 = KbRevision(id=uuid.uuid4(), document_id=doc1.id, rev_no=1, body="내용1", char_count=5)
    rev2 = KbRevision(id=uuid.uuid4(), document_id=doc2.id, rev_no=1, body="내용2", char_count=5)

    # doc1 은 잡 없음, doc2 는 이미 잡 있음
    mock_session = AsyncMock()
    mock_session.scalars.side_effect = [
        [doc1, doc2],  # first batch
        [],            # second batch (empty -> done)
    ]

    async def mock_get(model, pk):
        return None  # no access row -> not hidden

    mock_session.get.side_effect = mock_get

    async def mock_scalar(stmt):
        s = str(stmt)
        if "pg_try_advisory_lock" in s:
            return True
        if "kb_pipeline_jobs" in s:
            # If doc2 revision, return a job id, else None
            if str(rev2.id) in s or "params" in str(stmt):
                pass
        return None

    # We patch _latest_revision and enqueue_pipeline_job
    with patch("ttakkari.kb_backfill._latest_revision") as mock_latest, \
         patch("ttakkari.kb_backfill.enqueue_pipeline_job") as mock_enqueue:

        mock_latest.side_effect = lambda db, doc_id: rev1 if doc_id == doc1.id else rev2

        # 1) Dry run
        call_count = 0
        def fake_scalar(stmt, *args, **kwargs):
            nonlocal call_count
            s = str(stmt)
            if "pg_try_advisory_lock" in s:
                return True
            if "kb_pipeline_jobs" in s:
                call_count += 1
                return uuid.uuid4() if call_count == 2 else None
            return None
        mock_session.scalar.side_effect = fake_scalar

        factory = MagicMock(return_value=mock_session)
        mock_session.__aenter__.return_value = mock_session

        summary_dry = await run_enqueue_jobs_backfill(factory, apply=False, batch_size=10)
        assert summary_dry.examined == 2
        assert summary_dry.enqueued == 1
        assert summary_dry.skipped_has_job == 1
        mock_enqueue.assert_not_called()

        # 2) Apply run
        mock_session.scalars.side_effect = [[doc1, doc2], []]
        call_count = 0
        summary_apply = await run_enqueue_jobs_backfill(factory, apply=True, batch_size=10)
        assert summary_apply.examined == 2
        assert summary_apply.enqueued == 1
        assert summary_apply.skipped_has_job == 1
        mock_enqueue.assert_called_once_with(mock_session, document_id=doc1.id, revision_id=rev1.id)
