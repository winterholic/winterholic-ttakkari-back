"""질의 결과를 지식 원장(ADR 0031)으로 넘길 수 있는 구조로 바꾼다. 적재는 하지 않는다(ADR 0045).

원장 P0 게이트(PMS 5.4.7.3.6) 전이라 이번 단계는 인터페이스와 테스트까지다. 네 가지를 섞지 않는다:
- source_units: NotebookLM이 돌려준 원문 인용 발췌(원본 출처별). 원문 전체가 아니다.
- analysis: NotebookLM이 만든 답변. 사용자 확인 사실이 아니라 생성된 분석이다.
- citations: 분석과 원문 발췌를 잇는 연결(출처 ID, 원문 좌표, 답변 좌표).
- assertions: 항상 비어 있다. 자동 추출·승격을 하지 않고, 별도 추출·검토를 거친 것만 채울 수 있다.
인용 텍스트나 좌표가 없으면 없는 대로 None 으로 둔다. 하이라이트를 꾸며내지 않는다.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from ttakkari.notebooklm.ports import QueryResult, SourceInfo

SCHEMA = "ttakkari.notebooklm.ledger_export/1"


def build_ledger_export(result: QueryResult, sources: list[SourceInfo] | None = None,
                        *, account_id: str = "primary") -> dict[str, Any]:
    by_id = {s.id: s for s in (sources or [])}
    for ref in result.sources:  # 질의 결과가 가진 제목·URL 도 쓴다
        if ref.id not in by_id and (ref.title or ref.url):
            by_id[ref.id] = SourceInfo(id=ref.id, title=ref.title, url=ref.url, kind=ref.kind)

    units: dict[str, dict[str, Any]] = {}
    for c in result.citations:
        s = by_id.get(c.source_id)
        unit = units.setdefault(c.source_id, {
            "source_id": c.source_id,
            "title": s.title if s else None,
            "url": s.url if s else None,
            "kind": s.kind if s else None,
            "excerpts": [],
        })
        if c.quoted_text is not None:
            unit["excerpts"].append({
                "citation_number": c.number,
                "text": c.quoted_text,
                "text_sha256": hashlib.sha256(c.quoted_text.encode()).hexdigest(),
                "span": c.source_span.model_dump() if c.source_span else None,
                "span_unit": "utf16_code_unit" if c.source_span else None,
            })
    # 인용 없이 출처만 있는 경우도 출처 단위는 남긴다(발췌 없음).
    for ref in result.sources:
        s = by_id.get(ref.id)
        units.setdefault(ref.id, {"source_id": ref.id, "title": s.title if s else None,
                                  "url": s.url if s else None, "kind": s.kind if s else None, "excerpts": []})

    return {
        "schema": SCHEMA,
        "origin": {
            "system": "notebooklm",
            "account_id": account_id,
            "notebook_id": result.notebook_id,
            "conversation_id": result.conversation_id,
            "turn_number": result.turn_number,
            "queried_at": result.created_at,
            "exported_at": datetime.now(UTC).isoformat(),
        },
        "source_units": list(units.values()),
        "analysis": {
            "text": result.answer,
            "question": result.query,
            "generated_by": "notebooklm",
            "review_status": "unreviewed",
            "promoted": False,
        },
        "citations": [
            {
                "number": c.number,
                "source_id": c.source_id,
                "quoted_text": c.quoted_text,
                "source_span": c.source_span.model_dump() if c.source_span else None,
                "answer_span": c.answer_span.model_dump() if c.answer_span else None,
                "chunk_id": c.chunk_id,
            }
            for c in result.citations
        ],
        "assertions": [],
        "promotion": {
            "auto_promoted": False,
            "note": "NotebookLM 답변은 생성된 분석이다. assertion 은 별도 추출·검토를 거치기 전까지 비어 있다.",
        },
    }
