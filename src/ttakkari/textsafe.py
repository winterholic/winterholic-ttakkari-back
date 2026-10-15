"""DB 저장 직전 문자열 정화. Postgres text/jsonb 는 NUL(\\u0000)을 받지 않는다.

에이전트 도구 출력(바이너리 cat 등)에 NUL 이 섞이면 flush 가 `unsupported Unicode escape sequence` 로 터져
Run 전체가 실패한다. 모든 ORM 저장은 이 모듈의 before_flush 훅 한 곳을 지난다.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

NUL = "\u0000"
REPLACEMENT = "�"


def strip_nul(value: Any) -> Any:
    """문자열과 중첩 dict/list 의 NUL 을 U+FFFD 로 바꾼다. NUL 이 없으면 같은 객체를 돌려준다."""
    if isinstance(value, str):
        return value.replace(NUL, REPLACEMENT) if NUL in value else value
    if isinstance(value, dict):
        cleaned = {strip_nul(k): strip_nul(v) for k, v in value.items()}
        return cleaned if cleaned != value else value
    if isinstance(value, list):
        cleaned_list = [strip_nul(v) for v in value]
        return cleaned_list if cleaned_list != value else value
    return value


def _sanitize_instance(obj: Any, *, only_changed: bool) -> None:
    state = inspect(obj)
    for attr in state.mapper.column_attrs:
        hist = state.attrs[attr.key].history
        if only_changed and not hist.has_changes():
            continue
        current = state.dict.get(attr.key)
        if not isinstance(current, str | dict | list):
            continue
        cleaned = strip_nul(current)
        if cleaned is not current:
            setattr(obj, attr.key, cleaned)


def _before_flush(session: Session, _ctx: Any, _instances: Any) -> None:
    for obj in session.new:
        _sanitize_instance(obj, only_changed=False)
    for obj in session.dirty:
        _sanitize_instance(obj, only_changed=True)


event.listen(Session, "before_flush", _before_flush)
