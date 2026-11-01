"""작업자 완료 계약과 상시 지침 크기 상한(하네스 점검 2026-11-02).

lead 지침이 길어질수록 정작 중요한 위임·완료 규칙이 묻힌다. 상한을 넘기면 먼저 도서관·도구(account_usage)로 옮긴다."""

from types import SimpleNamespace
from unittest.mock import patch

from ttakkari.orchestrator import runtime
from ttakkari.runs import instructions

LEAD_STATIC_CHARS_MAX = 14_000
AGENT_NOTE_CHARS_MAX = 3_200


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
