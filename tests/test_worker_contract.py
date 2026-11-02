"""작업자 완료 계약과 상시 지침 크기 상한(하네스 점검 2026-11-02).

lead 지침이 길어질수록 정작 중요한 위임·완료 규칙이 묻힌다. 상한을 넘기면 먼저 도서관·도구(account_usage)로 옮긴다."""

from types import SimpleNamespace
from unittest.mock import patch

from ttakkari.orchestrator import runtime
from ttakkari.runs import instructions

LEAD_STATIC_CHARS_MAX = 14_000
AGENT_NOTE_CHARS_MAX = 3_200
AGENT_NOTE_WORKER_CHARS_MAX = 2_000


def test_worker_gets_completion_contract_when_no_file(tmp_path):
    fake = SimpleNamespace(harness_dir=tmp_path, instructions_max_chars=6000)
    with patch.object(instructions, "get_settings", return_value=fake):
        text = instructions.read("worker")
        assert instructions.is_default("worker")
    assert "상태: 완료 | 부분 | 막힘" in text
    assert "미실행: 이유" in text
    assert "토론이 기본" in text  # 전역 Codex/Claude 규칙을 무인 작업용으로 덮는다


def test_saved_worker_file_overrides_default(tmp_path):
    (tmp_path / "worker.md").write_text("사용자 지침", encoding="utf-8")
    fake = SimpleNamespace(harness_dir=tmp_path, instructions_max_chars=6000)
    with patch.object(instructions, "get_settings", return_value=fake):
        assert instructions.read("worker") == "사용자 지침"


def test_lead_delegation_spec_requires_checkable_done_criteria():
    note = " ".join(runtime.LEAD_NOTE.split())
    assert "done criteria as checkable items" in note
    assert "After two follow-ups without progress" in note
    assert "unevidenced" in note


def test_static_prompt_budget():
    lead = runtime.LEAD_NOTE + runtime.AGENT_NOTE + runtime.model_routing_note()
    assert len(lead) <= LEAD_STATIC_CHARS_MAX, len(lead)
    assert len(runtime.AGENT_NOTE) <= AGENT_NOTE_CHARS_MAX, len(runtime.AGENT_NOTE)


def test_worker_agent_note_is_smaller_and_keeps_core_rules():
    note = " ".join(runtime.AGENT_NOTE_WORKER.split())
    assert len(runtime.AGENT_NOTE_WORKER) <= AGENT_NOTE_WORKER_CHARS_MAX, len(runtime.AGENT_NOTE_WORKER)
    for needle in ("recall", "share_file", "request_backend_deploy", "sandbox denial", "unverified"):
        assert needle in note, needle
    # 기획·대화 세션 전용 규칙은 작업자 상시 지침에 넣지 않는다.
    for needle in ("knowledge_assertion", "knowledge_sources", "list_meetings"):
        assert needle not in note, needle


async def test_worker_session_gets_worker_note_and_lead_keeps_full_note(client, auth, make_dir):
    from importlib import import_module
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from ttakkari.models import SessionKind
    m = import_module("ttakkari.runs.manager")
    with patch.object(m, "pinned_brief", AsyncMock(return_value="")):
        worker = await m._system_note(None, Path("/tmp/o"), SimpleNamespace(kind=SessionKind.worker, parent_id="p"))
        lead = await m._system_note(None, Path("/tmp/o"), SimpleNamespace(kind=SessionKind.orchestrator, parent_id="d"))
    assert runtime.AGENT_NOTE_WORKER in worker and runtime.AGENT_NOTE not in worker
    assert runtime.AGENT_NOTE in lead and runtime.AGENT_NOTE_WORKER not in lead
