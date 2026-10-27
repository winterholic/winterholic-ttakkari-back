"""Terminal empty missions must never imply that any material was collected."""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ttakkari.collector import missions


@pytest.mark.parametrize(('job_status', 'acquired', 'mission_status', 'gaps', 'pending', 'expected'), [
    ('succeeded', 0, 'succeeded', [], 0, 'failed'),
    ('partial', 0, 'partial', [{'reason': 'search_provider_failed'}], 0, 'failed'),
    ('failed', 0, 'partial', [], 0, 'failed'),
    ('succeeded', 1, 'running', [], 0, 'succeeded'),
    ('succeeded', 1, 'partial', [{'reason': 'missing_topic'}], 0, 'partial'),
    ('retry_wait', 0, 'running', [], 0, 'running'),
    ('succeeded', 0, 'paused', [], 0, 'paused'),
    ('cancelled', 0, 'cancelled', [], 0, 'cancelled'),
    ('succeeded', 0, 'running', [], 1, 'running'),
])
async def test_mission_view_reports_empty_completion_as_failed_and_preserves_active_control_states(
    monkeypatch, job_status, acquired, mission_status, gaps, pending, expected,
):
    now = datetime.now(UTC)
    job = SimpleNamespace(id=uuid.uuid4(), status=job_status, stage='mission_prepare', source_key='prepare:test',
                          cursor={}, created_at=now, last_error=None)
    mission = SimpleNamespace(id=uuid.uuid4(), tenant_id='owner', status=mission_status,
                              plan={'title': 'Test collection'}, goal='Test collection', gaps=gaps,
                              current_run_seq=1, created_at=now, updated_at=now)
    job_rows, item_totals, precise_rows, ml_rows = (MagicMock() for _ in range(4))
    job_rows.all.return_value = [(job, 1)]
    item_totals.one.return_value = SimpleNamespace(_mapping={
        'acquired': acquired, 'accepted': acquired, 'failed': 0, 'blocked': 0, 'held': 0,
        'duplicate': 0, 'review_needed': 0,
    })
    precise_rows.all.return_value = []
    ml_rows.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[job_rows, item_totals, precise_rows, ml_rows])
    db.scalar = AsyncMock(side_effect=[acquired, pending])
    monkeypatch.setattr(missions, 'run_views', AsyncMock(return_value=[]))
    monkeypatch.setattr(missions.precise_eval, 'view_engine_status', lambda: {'status': 'unavailable'})
    monkeypatch.setattr(missions, '_ml_summary', lambda _: {})
    result = await missions.view(db, mission, detailed=False)
    assert result['counts']['acquired'] == acquired
    assert result['status'] == expected
    db.commit.assert_not_called()
