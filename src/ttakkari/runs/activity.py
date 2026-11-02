"""작업자 활동 상태. herdr(터미널 에이전트 멀티플렉서)의 working/blocked/idle 상태 모델을 따까리의 이벤트 로그에 맞게 다시 쓴 것이다.

herdr 는 터미널 화면 패턴으로 상태를 읽지만 따까리 엔진은 JSON 이벤트 스트림으로 도니 이벤트 나이와 마지막 이벤트 종류로 판단한다.
오케스트레이터가 폴링 없이 "지금 일하는지, 막혔는지, 멈춘 것 같은지"를 한 필드로 읽게 하는 것이 목적이다.

상태
- working: 실행 중이고 최근에 진행 이벤트가 있다. 도구 호출이 결과를 기다리는 중이면 Run 시간 제한까지 일하는 것으로 본다.
- stalled: 실행 중인데 진행 이벤트가 오래 없다(좀비·무응답 의심). 죽이지 않고 알리기만 한다.
- blocked: 한도·혼잡 대기 중이거나 작업자가 `상태: 막힘`으로 끝냈다.
- queued: 슬롯이나 이전 Run 을 기다린다.
- done / partial: 끝났다. 상태 줄이 `부분`이면 partial.
- failed / cancelled / idle: 그대로.
"""

from __future__ import annotations

TOOL_CALL_MAX_SECONDS = 2 * 60 * 60  # run_timeout_seconds 와 같은 크기. 이보다 오래 결과가 없으면 멈춘 것으로 본다.


def classify(*, active_status: str | None, waiting_limit: bool, waiting_reason: str | None, is_stuck: bool,
             last_progress_type: str | None, last_progress_age_s: float | None, done_status: str | None,
             completion_status: str | None, stall_seconds: int) -> tuple[str, str | None]:
    """(상태, 설명). 입력은 모두 이미 읽은 값이라 DB 를 건드리지 않는다."""
    if active_status is not None:
        if active_status == "queued":
            if waiting_limit:
                return "blocked", waiting_reason or "한도 대기"
            return "queued", None
        if is_stuck:
            return "stalled", "실행 프로세스가 없다"
        if last_progress_age_s is None:
            return "working", None
        limit = TOOL_CALL_MAX_SECONDS if last_progress_type == "tool.call" else stall_seconds
        if last_progress_age_s >= limit:
            return "stalled", f"진행 이벤트가 {int(last_progress_age_s // 60)}분째 없다"
        return "working", None
    if done_status == "succeeded":
        if completion_status == "막힘":
            return "blocked", "작업자가 막힘으로 보고했다"
        if completion_status == "부분":
            return "partial", "작업자가 부분 완료로 보고했다"
        return "done", None
    if done_status in ("failed", "cancelled"):
        return done_status, None
    return "idle", None
