import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ttakkari.models import RunStatus, ScheduledAction
from ttakkari.orchestrator.work import after_run, is_contract_resume_schedule


def test_is_contract_resume_schedule_distinguishes_contract_and_user_actions():
    # 1. Not send_message -> False
    assert is_contract_resume_schedule(ScheduledAction(action_type="other_action")) is False

    # 2. work_continuation flag -> True
    s1 = ScheduledAction(action_type="send_message", payload={"work_continuation": True})
    assert is_contract_resume_schedule(s1) is True

    # 3. origin orchestrator/work_contract -> True
    s2 = ScheduledAction(action_type="send_message", payload={"origin": "orchestrator"})
    assert is_contract_resume_schedule(s2) is True
    s3 = ScheduledAction(action_type="send_message", payload={"origin": "work_contract"})
    assert is_contract_resume_schedule(s3) is True

    # 4. prompt keywords -> True
    for kw in ("resume", "continue", "재개", "이어가기", "[미완료 목표 자동 이어가기]", "[영속 목표 계약]"):
        s = ScheduledAction(action_type="send_message", payload={"prompt": kw})
        assert is_contract_resume_schedule(s) is True

    # 5. description keywords -> True
    s_desc = ScheduledAction(action_type="send_message", description="목표 계약 자동 재개")
    assert is_contract_resume_schedule(s_desc) is True

    # 6. Unrelated user actions -> False
    u1 = ScheduledAction(action_type="send_message", payload={"message": "점심 약속 리마인더"}, description="개인 알림")
    assert is_contract_resume_schedule(u1) is False

    u2 = ScheduledAction(action_type="send_message", payload={"prompt": "회계 보고서 요약해줘"}, description="사용자 요청")
    assert is_contract_resume_schedule(u2) is False


@pytest.mark.asyncio
async def test_after_run_on_cancelled_pauses_goal_and_cancels_contract_resumes():
    from ttakkari.models import AppSetting, ChatSession, Run, SessionKind

    sess_id = uuid.uuid4()
    sess = MagicMock(spec=ChatSession)
    sess.id = sess_id
    sess.kind = SessionKind.orchestrator
    sess.archived_at = None

    run = MagicMock(spec=Run)
    run.id = uuid.uuid4()
    run.engine = "codex"
    run.status = RunStatus.cancelled
    run.error = "cancelled by user"

    contract_row = MagicMock(spec=AppSetting)
    contract_row.value = {
        "state": {"status": "active", "goal": "test goal", "requirements": []},
        "revision": 1,
    }

    resume_sched = ScheduledAction(
        action_type="send_message",
        payload={"session_id": str(sess_id), "prompt": "resume", "work_continuation": True},
        status="pending",
        description="목표 계약 자동 재개",
    )
    user_sched = ScheduledAction(
        action_type="send_message",
        payload={"session_id": str(sess_id), "message": "사용자 알림"},
        status="pending",
        description="사용자 개인 알림",
    )

    db = AsyncMock()
    # db.get(AppSetting, ...) returns contract_row
    db.get.return_value = contract_row
    # scalars().all() returns [resume_sched, user_sched]
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [resume_sched, user_sched]
    db.scalars.return_value = scalars_mock

    with patch("ttakkari.orchestrator.work.load", new=AsyncMock(return_value=contract_row.value)), \
         patch("ttakkari.orchestrator.work.events") as mock_events:
        res = await after_run(db, sess, run)

    # after_run should return None on cancel
    assert res is None

    # Goal state should be transitioned to 'paused'
    assert contract_row.value["state"]["status"] == "paused"
    assert "cancelled by user" in contract_row.value["state"]["blocker"]
    assert contract_row.value["revision"] == 2

    # Events should record work.paused
    mock_events.append.assert_called_once()
    event_call = mock_events.append.call_args
    assert event_call[0][2] == "work.paused"

    # Contract resume schedule should be cancelled
    assert resume_sched.status == "cancelled"
    assert "work pause on run cancel" in resume_sched.last_error

    # User schedule should remain pending
    assert user_sched.status == "pending"


@pytest.mark.asyncio
async def test_execute_action_skips_contract_resume_when_goal_paused_or_blocked():
    from ttakkari.models import ChatSession
    from ttakkari.schedules.service import execute_action

    sess_id = uuid.uuid4()
    sess = MagicMock(spec=ChatSession)
    sess.id = sess_id

    resume_sched = ScheduledAction(
        id=uuid.uuid4(),
        action_type="send_message",
        payload={"session_id": str(sess_id), "prompt": "resume", "work_continuation": True},
        status="pending",
        description="목표 계약 자동 재개",
    )

    user_sched = ScheduledAction(
        id=uuid.uuid4(),
        action_type="send_message",
        payload={"session_id": str(sess_id), "message": "사용자 리마인더"},
        status="pending",
        description="사용자 개인 알림",
    )

    mock_db = AsyncMock()
    mock_db.get.return_value = sess

    class MockContextManager:
        async def __aenter__(self):
            return mock_db
        async def __aexit__(self, *args):
            pass

    mock_sm_instance = MagicMock(return_value=MockContextManager())
    mock_sessionmaker = MagicMock(return_value=mock_sm_instance)

    # 1. 계약 상태가 paused 일 때 -> resume_sched 는 skipped
    paused_contract = {"state": {"status": "paused", "goal": "g"}}
    with patch("ttakkari.schedules.service.sessionmaker", return_value=mock_sessionmaker), \
         patch("ttakkari.orchestrator.work.load", new=AsyncMock(return_value=paused_contract)), \
         patch("ttakkari.api.sessions.queue_run") as mock_queue_run:
        res = await execute_action(resume_sched)
        assert res["status"] == "skipped"
        assert res["reason"] == "goal contract is paused"
        mock_queue_run.assert_not_called()

    # 2. 계약 상태가 blocked 일 때 -> resume_sched 는 skipped
    blocked_contract = {"state": {"status": "blocked", "goal": "g"}}
    with patch("ttakkari.schedules.service.sessionmaker", return_value=mock_sessionmaker), \
         patch("ttakkari.orchestrator.work.load", new=AsyncMock(return_value=blocked_contract)), \
         patch("ttakkari.api.sessions.queue_run") as mock_queue_run:
        res = await execute_action(resume_sched)
        assert res["status"] == "skipped"
        assert res["reason"] == "goal contract is blocked"
        mock_queue_run.assert_not_called()

    # 3. 계약 상태가 paused 여도 일반 사용자 예약은 queue_run 정상 호출
    dummy_run = MagicMock()
    dummy_run.id = uuid.uuid4()
    with patch("ttakkari.schedules.service.sessionmaker", return_value=mock_sessionmaker), \
         patch("ttakkari.orchestrator.work.load", new=AsyncMock(return_value=paused_contract)), \
         patch("ttakkari.api.sessions.queue_run", new=AsyncMock(return_value=dummy_run)) as mock_queue_run:
        res = await execute_action(user_sched)
        assert "queued_run_id" in res
        assert res["queued_run_id"] == str(dummy_run.id)
        mock_queue_run.assert_called_once()

