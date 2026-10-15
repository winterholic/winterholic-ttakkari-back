import pytest

from ttakkari.experiments.qwen35_feedback_export import (
    build_record,
    redact_message,
    scan_export_text,
    write_jsonl,
)


def test_redaction_masks_sensitive_patterns_before_export():
    text = (
        "문의는 alice@example.com, 010-1234-5678, https://example.com/x, /Users/alice/work, "
        "550e8400-e29b-41d4-a716-446655440000, 192.168.1.2, Bearer abcdefghijklmnop, "
        "api_key=sk-abcdefghijklmnop123456"
    )
    redacted = redact_message(text)

    assert "alice@example.com" not in redacted
    assert "010-1234-5678" not in redacted
    assert "https://example.com/x" not in redacted
    assert "/Users/alice/work" not in redacted
    assert "550e8400-e29b-41d4-a716-446655440000" not in redacted
    assert "192.168.1.2" not in redacted
    assert "Bearer abcdefghijklmnop" not in redacted
    assert "sk-abcdefghijklmnop123456" not in redacted
    assert scan_export_text(redacted) == []


def test_export_record_is_allowlisted_and_labels_feedback_provenance():
    row = {
        "message": "코드 변경을 구현해 줘. 담당자는 alice@example.com",
        "decided": "answer",
        "expected": "delegate",
        "note": "이 값은 export되면 안 됨",
        "run_id": "550e8400-e29b-41d4-a716-446655440000",
    }

    record = build_record(row, sample_id="real-0001")

    assert set(record) == {"sample_id", "message", "label", "label_status", "evidence"}
    assert record["label"] == "delegate"
    assert record["label_status"] == "automatic_user_correction_unreviewed"
    assert record["evidence"] == {
        "source": "desk_feedback",
        "decided": "answer",
        "expected": "delegate",
    }
    assert "note" not in record and "run_id" not in record
    assert scan_export_text(record["message"]) == []


def test_confirmation_is_reference_only_not_ground_truth():
    record = build_record({"message": "안녕", "decided": "answer", "expected": "answer"}, sample_id="real-0002")

    assert record["label_status"] == "user_confirmation_reference_only"


def test_export_fails_closed_on_non_allowlisted_fields(tmp_path):
    record = build_record({"message": "안녕하세요", "decided": "answer", "expected": "answer"}, sample_id="real-0001")
    record["note"] = "must never leave the process"

    with pytest.raises(ValueError, match="export_validation_failed"):
        write_jsonl([record], tmp_path / "dataset.jsonl")

    assert not (tmp_path / "dataset.jsonl").exists()
