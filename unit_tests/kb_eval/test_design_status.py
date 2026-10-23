import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / "docs/design/kb-v2/reviews"


def test_current_status_records_e0_logic_completion():
    paths = [
        ROOT / "docs/design/kb-v2/knowledge-library-design-v2.md",
        ROOT / "docs/design/knowledge-library.md",
        ROOT / "docs/kb/design-v1.md",
        REVIEW / "v2-review-response.md",
    ]
    for path in paths:
        assert "KB-E0 로직 완료 승인, 제품검색 미승인" in path.read_text()[:2000], str(
            path
        )


def test_adr_records_user_directed_minimal_productization_without_claiming_quality_gate():
    # 2026-10-23 사용자 명시 지시로 '제품검색 미승인'을 해제했다. 해제 사실과 남은 품질 gate가 함께 적혀야 한다.
    head = (ROOT / "docs/adr/0063-knowledge-library.md").read_text()[:2500]
    assert "KB-E0 로직 완료 승인" in head
    assert "2026-10-23 사용자의 명시 지시" in head
    assert "KB7 품질 gate" in head and "통과한 것이 아니다" in head


def test_delivery_checker_rejects_missing_current_state_even_if_history_mentions_approval():
    spec = importlib.util.spec_from_file_location(
        "delivery", REVIEW / "check_design_delivery.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    design = (REVIEW.parent / "knowledge-library-design-v2.md").read_text()
    response = (REVIEW / "v2-review-response.md").read_text()
    module.inventory(design, response)
    with pytest.raises(AssertionError, match="current E0 scope"):
        module.inventory(
            design.replace("KB-E0 로직 완료 승인, 제품검색 미승인", "approval removed"),
            response,
        )
