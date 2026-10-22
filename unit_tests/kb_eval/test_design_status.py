import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / "docs/design/kb-v2/reviews"


def test_current_status_records_e0_logic_completion():
    paths = [
        ROOT / "docs/design/kb-v2/knowledge-library-design-v2.md",
        ROOT / "docs/adr/0063-knowledge-library.md",
        ROOT / "docs/design/knowledge-library.md",
        ROOT / "docs/kb/design-v1.md",
        REVIEW / "v2-review-response.md",
    ]
    for path in paths:
        assert "KB-E0 로직 완료 승인, 제품검색 미승인" in path.read_text()[:2000], str(
            path
        )


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
