"""작업 카드: 오케스트레이터가 작업자에게 주는 지시를 항목 필드로 받아 항상 같은 읽기 쉬운 틀로 조립한다.

긴 자유 서술(평균 2~3천 자, 해시·"앞 지시와 같다" 참조, 여러 주제가 한 덩어리) 대신 구조를 도구 스키마에 둔다. 항목마다 한 가지만
적게 되어 사람이 읽기 쉽고, 모델은 필요한 항목을 찾아 읽는다. 보고 형식(`상태:` 줄)은 틀에 고정되어 매번 다시 쓰지 않는다."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

REPORT_FORMAT = (
    "마지막 메시지의 첫 줄에 `상태: 완료 | 부분 | 막힘` 중 하나를 쓰고, 완료 기준 항목마다 결과 한 줄과 실행 증거(명령과 출력 1~3줄)를 적는다. "
    "하지 않은 항목은 빼지 말고 '하지 않음'과 이유를 적는다. 막혔으면 시도한 것, 관찰한 오류, 다음 후보를 적는다."
)


class TaskCard(BaseModel):
    goal: str = Field(min_length=1, max_length=1_000)
    location: str | None = Field(default=None, max_length=500)
    read_first: list[str] = Field(default_factory=list, max_length=20)
    limits: list[str] = Field(default_factory=list, max_length=15)
    done_criteria: list[str] = Field(min_length=1, max_length=15)
    verification: list[str] = Field(default_factory=list, max_length=10)
    context: str | None = Field(default=None, max_length=8_000)


def _clean(items: list[str]) -> list[str]:
    return [item.strip()[:600] for item in items if item and item.strip()]


def render(card: TaskCard, extra: str = "") -> str:
    """카드를 작업자에게 보낼 지시문으로 만든다. extra 는 카드 밖 추가 지시(후속 지시의 한두 문장 등)다."""
    parts = ["[작업 카드]", f"목표: {card.goal.strip()}"]
    if card.location and card.location.strip():
        parts.append(f"작업 위치: {card.location.strip()}")
    for title, items, numbered in (
        ("먼저 읽을 것", _clean(card.read_first), False),
        ("범위와 금지", _clean(card.limits), False),
        ("완료 기준", _clean(card.done_criteria), True),
        ("검증 방법", _clean(card.verification), False),
    ):
        if items:
            parts.append(f"{title}:")
            parts.extend(f"{i}. {t}" if numbered else f"- {t}" for i, t in enumerate(items, 1))
    if card.context and card.context.strip():
        parts.append("배경과 결정된 것:\n" + card.context.strip())
    if extra.strip():
        parts.append("추가 지시:\n" + extra.strip())
    parts.append("보고 형식: " + REPORT_FORMAT)
    return "\n".join(parts)


def compose(prompt: str, card: TaskCard | None) -> str:
    """카드가 있으면 카드로 조립하고 prompt 는 추가 지시로 붙인다. 없으면 prompt 그대로."""
    return render(card, prompt) if card is not None else prompt


# 먼저 읽을 것에 큰 파일이 범위 없이 들어가면 시작 때마다 컨텍스트의 큰 몫을 쓴다(실측: 세션 도구 출력의 62%가 처음 12번 호출).
# 한글 위주 문서는 바이트당 약 0.3토큰이다(RESEARCH.md 337KB가 약 9.9만 토큰).
LARGE_READ_BYTES = 80_000
_TOKENS_PER_BYTE = 0.3
_RANGE_HINT = re.compile(r"(:\d+(-\d+)?\s*$|#L?\d+|\d+\s*[-~]\s*\d+\s*(줄|행|line)|(줄|행|line)s?\s*\d+|섹션|절|부분|항목|§|헤딩)", re.IGNORECASE)
_PATH_TOKEN = re.compile(r"[^\s()（）\[\]'\"`,;]+")


def read_budget_warnings(card: TaskCard, base_dirs: list[Path]) -> list[str]:
    """먼저 읽을 것의 큰 파일을 범위 지정 없이 적은 항목을 찾아 오케스트레이터에게 알린다. 거절하지 않고 알리기만 한다."""
    out: list[str] = []
    for item in card.read_first:
        m = _PATH_TOKEN.search(item.strip().lstrip("-* ").strip())
        if not m or _RANGE_HINT.search(item):
            continue
        raw = m.group(0).rstrip(":")
        path = Path(raw).expanduser()
        candidates = [path] if path.is_absolute() else [b / path for b in base_dirs]
        for cand in candidates:
            try:
                size = cand.stat().st_size if cand.is_file() else 0
            except OSError:
                continue
            if size >= LARGE_READ_BYTES:
                tokens = int(size * _TOKENS_PER_BYTE)
                out.append(
                    f"먼저 읽을 것 `{raw}` 은 {size // 1024}KB(약 {tokens // 1000}K토큰 추정)입니다. 통째로 읽히면 컨텍스트의 큰 몫을 씁니다. "
                    "필요한 섹션이나 줄 범위를 적거나(예: `파일:120-260`), 요약 파일로 바꿔 주세요."
                )
                break
    return out
