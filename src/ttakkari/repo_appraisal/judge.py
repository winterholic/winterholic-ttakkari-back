"""LLM 판정 1회 호출(ADR 0073). desk 의 일회성 CLI 패턴을 재사용해 도구·전역 하네스 없이 부른다.

실패·출력 오류면 None 을 돌려주고 부른 쪽이 결정론 점수만으로 끝낸다. 실패 처리하지 않는다.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

SYSTEM_PROMPT = """너는 GitHub 저장소 품질 평가자다. 증거 패킷만 보고 아래 항목을 0~5점(정수)으로 매긴다.
엄격하게 매긴다. 5점은 기준을 넘어선 가치가 있을 때만 준다. 근거 없는 점수는 주지 않는다.
항목: code_quality.readability(표본 가독성·일관성), architecture.layering(계층·책임 분리),
ai_harness.coherence(지침 간 모순·역할 분담; 하네스 없으면 빈 citations 로 두어라),
originality.verdict(README 기반 독창성; 신뢰도 낮음).
반드시 JSON 한 개만 출력한다. 다른 말은 쓰지 않는다.
{"scores": [{"item": "축.항목", "score": 0~5, "citations": ["패킷 속 파일 경로 또는 evidence:xxx"], "confidence": "high|medium|low"}], "summary": "한 줄 총평"}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_prompt(packet_text: str, repo_type: str, pending_items: list[str]) -> str:
    return "\n".join([
        f"저장소 유형: {repo_type}",
        f"판정 항목: {', '.join(pending_items)}",
        "출력: JSON 한 개(항목·점수·인용·신뢰도·한 줄 총평).",
        "",
        "증거 패킷(인용은 아래 경로·evidence id만):",
        packet_text,
    ])


def parse_output(text: str | None) -> dict[str, Any] | None:
    """모델 출력에서 JSON 을 뽑는다. 계약에 안 맞으면 None."""
    if not text:
        return None
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("scores"), list):
        return None
    return obj


async def call_model(system: str, prompt: str, *, engine: str, model: str, timeout: float,
                     runner: Callable[[str, str], Awaitable[tuple[str | None, str | None]]] | None = None,
                     ) -> tuple[str | None, str | None]:
    """(본문, 오류). 기본 실행기는 desk 일회성 호출이다(상주·도구·전역 하네스 없음)."""
    if runner is not None:
        return await runner(system, prompt)
    from ttakkari.runs import desk as desk_mod
    from ttakkari.runs import profiles as profiles_mod

    prof = profiles_mod.Profile(id=uuid.uuid4(), name="repo-appraisal", engine=engine)
    res = await desk_mod._ask(prof, system, prompt, cfg_override={"model": model},
                              timeout_seconds=timeout, use_warm=False, prompt_via_stdin=True)
    return res.text, res.error
