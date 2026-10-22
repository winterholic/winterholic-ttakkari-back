"""Record KB-E0 logic completion as the current state; keep earlier states as history."""

import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OLD = "KB-E0 착수 조건부 승인, 제품검색 미승인"
NEW = "KB-E0 로직 완료 승인, 제품검색 미승인"
REVIEWS = ROOT / "docs/design/kb-v2/reviews"


def edit(rel, pairs):
    path = ROOT / rel
    text = path.read_text()
    for old, new in pairs:
        assert text.count(old) == 1, (rel, old[:60])
        text = text.replace(old, new)
    path.write_text(text)


for version in ("v3", "v4"):
    src = Path(
        f"/tmp/kb-e0-{version}-verify/docs/design/kb-v2/reviews/kb-e0-independent-review-{version}.md"
    )
    dst = REVIEWS / src.name
    shutil.copyfile(src, dst)
    assert (
        hashlib.sha256(src.read_bytes()).digest()
        == hashlib.sha256(dst.read_bytes()).digest()
    )

design = "docs/design/kb-v2/knowledge-library-design-v2.md"
banner_old = "> **상태: " + OLD + ".** 활성 설계 정본은 이 파일이다."
banner_new = (
    "> **상태: " + NEW + ".** 활성 설계 정본은 이 파일이다. "
    "KB-E0 하네스는 main `1e9bb1a` 기준으로 독립 확인 4회차에서 passed를 받았다"
    "([3회차](reviews/kb-e0-independent-review-v3.md), [4회차](reviews/kb-e0-independent-review-v4.md), "
    "[E0 완료 기록](reviews/kb-e0-completion.md)). 승인 범위는 DB·UI·추론을 제외한 E0 로직이다. "
    "실제 DB export adapter·모바일 UI·실추론·실서버 계측은 미실행 잔여다. "
    "검색/수집 제품 구현·엔진/모델 선택·최종 품질·운영배포는 미승인이다. "
    "다음 단위는 9.2 순서를 따르되 배정 전에는 시작하지 않는다.\n\n"
    "> 역사적 상태(E0 착수 시점): **상태: " + OLD + ".** 활성 설계 정본은 이 파일이다."
)
edit(
    design,
    [
        (banner_old, banner_new),
        (
            "현재 상태는 **"
            + OLD
            + "**이다. P1–13·F1–24 반영 독립 확인은 위 원문에 보존하며, 이번 단계에서는 KB-E0 하네스만 구현한다.",
            "현재 상태는 **"
            + NEW
            + "**이다(2026-10-22, DB·UI·추론 제외). E0 착수 시점의 상태는 **"
            + OLD
            + "**였다. P1–13·F1–24 반영 독립 확인은 위 원문에 보존하며, E0 단계에서는 KB-E0 하네스만 구현했다.",
        ),
        (
            "| 착수 조건부 승인, 하네스 독립 검증 대기. 평가 담당(핵심) |",
            "| 로직 완료 승인(main `1e9bb1a`, 독립 확인 4회차 passed, DB·UI·추론 제외). 실제 DB export·모바일 UI·실추론은 잔여. 평가 담당(핵심) |",
        ),
    ],
)
edit(
    "docs/adr/0063-knowledge-library.md",
    [
        (
            OLD + ". E0 하네스 독립 검증·실제 DB·최종 품질 gate는 별도.",
            NEW
            + "(2026-10-22, DB·UI·추론 제외). 실제 DB·최종 품질 gate는 별도. 이전 상태: "
            + OLD
            + ".",
        )
    ],
)
for rel in ("docs/design/knowledge-library.md", "docs/kb/design-v1.md"):
    edit(rel, [("(" + OLD + ")", "(" + NEW + ")")])
edit(
    "docs/design/kb-v2/reviews/v2-review-response.md",
    [
        ("# v2 독립 검토 대응표 — " + OLD, "# v2 독립 검토 대응표 — " + NEW),
        (
            "현재: [수정 반영 독립 확인](amendment-independent-review.md)에서 KB-E0 착수만 조건부 승인했다.",
            "현재: KB-E0 로직이 [독립 확인 4회차](kb-e0-independent-review-v4.md)에서 완료 승인됐다(DB·UI·추론 제외, [E0 완료 기록](kb-e0-completion.md)). "
            "그 전에는 [수정 반영 독립 확인](amendment-independent-review.md)에서 KB-E0 착수만 조건부 승인했다("
            + OLD
            + ").",
        ),
    ],
)
edit(
    "docs/design/kb-v2/reviews/README.md",
    [
        (
            "현재: **" + OLD + "**.",
            "현재: **"
            + NEW
            + "**([E0 완료 기록](kb-e0-completion.md)). 이전 상태는 **"
            + OLD
            + "**였다.",
        )
    ],
)
edit(
    "docs/design/kb-v2/reviews/check_design_delivery.py",
    [
        ('"' + OLD + '" in design[:2000]', '"' + NEW + '" in design[:2000]'),
        ('"' + OLD + '" in response[:2000]', '"' + NEW + '" in response[:2000]'),
    ],
)
edit(
    "unit_tests/kb_eval/test_design_status.py",
    [
        (
            "def test_current_status_matches_conditional_e0_approval():",
            "def test_current_status_records_e0_logic_completion():",
        ),
        (
            'assert "' + OLD + '" in path.read_text()[:2000]',
            'assert "' + NEW + '" in path.read_text()[:2000]',
        ),
        ('"' + OLD + '", "approval removed"', '"' + NEW + '", "approval removed"'),
    ],
)
print("recorded")
