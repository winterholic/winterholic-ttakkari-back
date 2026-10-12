"""분석 Run 이 돌려주는 구조화 출력의 스키마와 검증(ADR 0010). 검증을 통과한 것만 저장한다."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
    ValidationError,
)


class AnalysisError(ValueError):
    """분석 결과를 읽거나 검증하지 못했다. 메시지는 사용자에게 보인다."""


def _strip(v: object) -> object:
    return v.strip() if isinstance(v, str) else v


def _blank_to_none(v: object) -> object:
    return (v.strip() or None) if isinstance(v, str) else v


Text = Annotated[str, BeforeValidator(_strip), Field(min_length=1, max_length=1500)]
OptText = Annotated[Annotated[str, StringConstraints(max_length=1500)] | None, BeforeValidator(_blank_to_none)]
Short = Annotated[Annotated[str, StringConstraints(max_length=100)] | None, BeforeValidator(_blank_to_none)]


class Conflict(BaseModel):
    # 프롬프트에 후보로 준 짧은 참조. M=기존 기억, A=이전 회의의 열린 액션. 후보에 없으면 파이프라인이 버린다.
    ref: str = Field(pattern=r"^[MA]\d{1,3}$")
    reason: Text
    # M 은 supersede 만 뜻이 있다. A 는 cancel | revise. 비우면 파이프라인이 종류에 맞게 정한다.
    change: Literal["supersede", "cancel", "revise"] | None = None
    new_text: OptText = None


class Discussion(BaseModel):
    topic: Text
    summary: OptText = None
    evidence: OptText = None


class Decision(BaseModel):
    text: Text
    rationale: OptText = None
    evidence: OptText = None
    conflicts: list[Conflict] = Field(default_factory=list, max_length=5)


class ActionItem(BaseModel):
    text: Text
    owner: Short = None
    due: Short = None
    evidence: OptText = None
    conflicts: list[Conflict] = Field(default_factory=list, max_length=5)


class OpenIssue(BaseModel):
    text: Text
    detail: OptText = None
    evidence: OptText = None


class Analysis(BaseModel):
    summary: Annotated[str, BeforeValidator(_strip), Field(min_length=1, max_length=4000)]
    participants: list[Annotated[str, BeforeValidator(_strip), Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=50)
    discussions: list[Discussion] = Field(default_factory=list, max_length=60)
    decisions: list[Decision] = Field(default_factory=list, max_length=60)
    action_items: list[ActionItem] = Field(default_factory=list, max_length=80)
    open_issues: list[OpenIssue] = Field(default_factory=list, max_length=60)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> object:
    """엔진 최종 메시지에서 JSON 객체를 꺼낸다. 코드 펜스가 있으면 마지막 펜스부터, 없으면 첫 `{` 부터 마지막 `}` 까지."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)][::-1]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except ValueError:
            continue
    raise AnalysisError("분석 결과에서 JSON 을 찾지 못했어요.")


def parse_analysis(text: str | None) -> Analysis:
    if not text or not text.strip():
        raise AnalysisError("분석 Run 이 결과를 남기지 않았어요.")
    data = extract_json(text)
    try:
        return Analysis.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(x) for x in first["loc"])
        raise AnalysisError(f"분석 결과 형식이 맞지 않아요 ({where}: {first['msg']}).") from e
