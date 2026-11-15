"""컨텍스트 초기화 후 첫 Run에 직전 Run의 구조화 기록만 다시 싣는다.

초기화하면 엔진 대화가 비어 직전 결정·발견이 사라진다. 자동 교대의 원문 발췌(context_handoff)는
70% 이상일 때만 생기고 고정 기억·recall은 상시·요청 기반이라, 명시적 초기화 직후 첫 Run의
구조화 재료(완료 계약·검증 기록·오류·변경 경로)만 1500자 이내로 카드에 붙인다.
원문 출력(도구 출력·전체 응답)은 싣지 않는다. 재료가 없으면 빈 문자열(생략)이다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

MAX_CHARS = 1500
MAX_RUNS = 3
MAX_NOTE_CHARS = 300  # 한 Run의 결정·오류가 메모 전체를 잠식하지 않게 자른다.
MAX_CHANGED_PATHS = 10


def first_after_reset(fresh_reset: bool, reset_at: datetime | None,
                      latest_prior_at: datetime | None, has_prior: bool) -> bool:
    """이번에 줄 지시가 초기화 후 첫 Run인지. 이전 Run이 없으면 거짓이다."""
    if not has_prior:
        return False
    if fresh_reset:
        return True
    return reset_at is not None and (latest_prior_at is None or latest_prior_at < reset_at)


def from_run(run: Any, changed: list[str]) -> dict[str, Any]:
    """요약 재료가 될 구조화 필드만 뽑는다. 결과 원문·도구 출력은 담지 않는다."""
    usage = run.usage or {}
    return {"run_id": str(run.id)[:8], "status": run.status, "completion": usage.get("completion"),
            "verification_status": usage.get("verification_status"),
            "verification_note": usage.get("verification_note"), "error": run.error, "changed": changed}


def _has_material(digest: dict[str, Any]) -> bool:
    return bool(digest.get("completion") or digest.get("verification_status")
                or digest.get("verification_note") or digest.get("error") or digest.get("changed"))


def build(digests: list[dict[str, Any]], max_chars: int = MAX_CHARS) -> str:
    """최신순 최대 3개 Run을 한 메모로 만든다. 재료가 없으면 빈 문자열이다."""
    picked = [d for d in digests[:MAX_RUNS] if _has_material(d)]
    if not picked:
        return ""
    lines = ["[이전 결정·발견 요약 — 직전 Run 구조화 기록만]"]
    for digest in picked:
        comp = digest.get("completion") or {}
        contract = "위반" if comp.get("violation") else comp.get("status") or "없음"
        verify = digest.get("verification_status") or "없음"
        lines.append(f"Run {digest['run_id']} {digest['status']} 완료계약:{contract} 검증:{verify}")
        if note := (digest.get("verification_note") or "").strip():
            lines.append(f"결정: {note[:MAX_NOTE_CHARS]}")
        if error := (digest.get("error") or "").strip():
            lines.append(f"오류: {error[:MAX_NOTE_CHARS]}")
        changed = digest.get("changed") or []
        if changed:
            lines.append(f"변경: {', '.join(changed[:MAX_CHANGED_PATHS])}"
                         + (f" 외 {len(changed) - MAX_CHANGED_PATHS}개" if len(changed) > MAX_CHANGED_PATHS else ""))
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    note = f"\n+ 나머지 생략(상한 {max_chars}자)"
    cut = text[:max_chars - len(note)]
    return cut[:cut.rfind("\n")] + note
