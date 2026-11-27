"""작업자 완료 계약 미들웨어(finish-hook).

작업자는 마지막 메시지 첫 줄에 `상태: 완료|부분|막힘`을 쓴다(instructions.WORKER_DEFAULT). 지침 문구만으로는 지켜지지
않으므로 Run 이 끝나는 자리에서 구조로 확인한다.

- 상태 줄을 파싱해 Run.usage["completion"]에 남기고 오케스트레이터 보고에 붙인다.
- 상태 줄이 없으면(계약 위반) 같은 작업 세션에 완료 확인 Run 을 한 번 자동으로 넣는다. 항목별 수행 여부와 증거를 닫게 하는
  finish-hook 이다. 세션당 상한이 있고, 완료 확인 Run 자체는 다시 확인하지 않는다.
- `부분`·`막힘`은 자동으로 다시 돌리지 않는다. 진짜 막힌 일을 반복 실행하지 않도록 판단은 보고를 받은 기획 세션이 한다.
- `부분`·`막힘`에는 `남은 일: 대기|환경|범위`가 따라온다. 같은 작업자를 또 돌려도 풀리지 않는 부분(바깥 결과 대기, 이 환경에서
  못 하는 확인)과 더 돌리면 되는 부분을 구조로 가른다. 2026-11-10 이후 작업자 Run 에서 부분이 43%였고 그 뒤 Run 의 53%가 다시
  부분이었다. 한 가지 경고문("좁혀서 나누라")은 대기·환경에는 맞지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.models import ChatSession, Run, RunOrigin, RunStatus, SessionKind
from ttakkari.runs import events

STATUSES = ("완료", "부분", "막힘")
# 마크다운 장식(`**`, `>`, 목록 기호)이 붙어도 읽는다. 첫 몇 줄 안에 있어야 계약을 지킨 것으로 본다.
_STATUS_RE = re.compile(r"^[\s*_#>`\-]*상태\s*[:：]\s*[*_`]*\s*(완료|부분|막힘)", re.MULTILINE)
HEAD_LINES = 6
REMAINING_KINDS = ("대기", "환경", "범위")
_REMAINING_RE = re.compile(r"^[\s*_#>`\-]*남은\s*일\s*[:：]\s*[*_`]*\s*(대기|환경|범위)", re.MULTILINE)
REMAINING_HEAD_LINES = HEAD_LINES + 3
MAX_CLOSURES_PER_SESSION = 2
_TASK_ORIGINS = (RunOrigin.orchestrator, RunOrigin.user)

CLOSURE_PROMPT = (
    "[완료 확인] 직전 Run 의 마지막 메시지에 `상태:` 줄이 없다. 처음 받은 지시를 항목별로 다시 대조하라.\n"
    "- 실제로 수행하고 실행 증거(명령·출력 1~3줄)가 있는 항목만 완료로 적는다. 증거가 없으면 지금 실행해서 만든다.\n"
    "- 하지 않은 항목은 이번 턴에서 이어서 수행한다. 이미 끝낸 작업과 검증은 반복하지 않는다.\n"
    "- 마지막 메시지 첫 줄에 `상태: 완료 | 부분 | 막힘` 중 하나를 쓰고, 지시 항목마다 결과 한 줄을 적는다.\n"
    "- 막혔으면 시도한 것, 관찰한 오류, 다음 후보를 적는다."
)

WEAK_CLOSURE_PROMPT = (
    "[완료 확인] 직전 Run 에서 `상태: 완료`를 선언했으나 실행 증거(명령·출력 1~3줄, 커밋, 검증 결과)가 불충분하다.\n"
    "- 실제로 수행하고 실행 증거가 있는 항목만 완료로 적는다. 증거가 없으면 지금 실행해서 만든다.\n"
    "- 하지 않은 항목은 이번 턴에서 이어서 수행한다. 이미 끝낸 작업과 검증은 반복하지 않는다.\n"
    "- 마지막 메시지 첫 줄에 `상태: 완료 | 부분 | 막힘` 중 하나를 쓰고, 지시 항목마다 결과 한 줄과 실행 증거를 적는다.\n"
    "- 막혔으면 시도한 것, 관찰한 오류, 다음 후보를 적는다."
)


def enabled() -> bool:
    return bool(get_settings().worker_completion_contract)


def parse(text: str | None) -> str | None:
    """결과 첫머리의 `상태:` 값. 없으면 None."""
    head = "\n".join((text or "").strip().splitlines()[:HEAD_LINES])
    found = _STATUS_RE.search(head)
    return found.group(1) if found else None


def parse_remaining(text: str | None) -> str | None:
    """`남은 일:` 값. 상태 줄 바로 뒤에 오므로 첫머리를 조금 넓게 본다."""
    head = "\n".join((text or "").strip().splitlines()[:REMAINING_HEAD_LINES])
    found = _REMAINING_RE.search(head)
    return found.group(1) if found else None


# 같은 지시를 또 보내도 풀리지 않는 쪽은 그렇게 말해준다. 기획 보고와 작업자 상태의 advisory 가 같은 문구를 쓴다.
REMAINING_ADVICE = {
    "대기": "바깥 결과(CI·큐·배포)를 기다리는 중이다. 같은 작업자에게 폴링을 다시 시키지 말고 결과가 나올 때쯤 확인 한 번만 지시하라.",
    "환경": "이 작업자 환경에서는 할 수 없는 확인이다. 같은 항목을 다시 지시하지 말고 가능한 환경으로 옮기거나 사용자 확인 항목으로 분리하라.",
    "범위": "더 할 수 있는 일이 남았다. 미충족 항목을 좁힌 단위로 나눠 후속 지시를 내려라.",
}


def applies(sess: ChatSession, run: Run) -> bool:
    return (enabled() and sess.kind == SessionKind.worker and sess.parent_id is not None
            and run.status == RunStatus.succeeded and bool((run.result_text or "").strip())
            and run.origin in _TASK_ORIGINS)


def annotate(sess: ChatSession, run: Run) -> dict[str, Any] | None:
    """Run.usage 에 계약 이행 여부를 기록하고 돌려준다. 적용 대상이 아니면 None."""
    if not applies(sess, run):
        return None
    from ttakkari.orchestrator.goal_signals import weak_evidence
    status = parse(run.result_text)
    is_closure = bool((run.usage or {}).get("closure"))
    is_weak = False
    if status == "완료" and not is_closure:
        lines = [ln.strip() for ln in (run.result_text or "").strip().splitlines()]
        body_lines = [ln for ln in lines if not _STATUS_RE.match(ln) and not _REMAINING_RE.match(ln)]
        body = "\n".join(body_lines).strip()
        is_weak = not body or weak_evidence(body)
    info: dict[str, Any] = {"status": status, "violation": (status is None) or is_weak}
    if is_weak:
        info["weak_evidence"] = True
    if status in ("부분", "막힘") and (remaining := parse_remaining(run.result_text)):
        info["remaining"] = remaining
    run.usage = {**(run.usage or {}), "completion": info}
    return info


async def closure_run(db: AsyncSession, sess: ChatSession, run: Run) -> Run | None:
    """계약 위반 Run 뒤에 완료 확인 Run 을 한 번 넣는다. 호출자가 commit 후 sess 를 dispatch 한다."""
    info = (run.usage or {}).get("completion") or {}
    if not info.get("violation") or (run.usage or {}).get("closure"):
        return None
    recent = list(await db.scalars(
        select(Run).where(Run.session_id == sess.id).order_by(Run.created_at.desc()).limit(30)))
    if sum(1 for r in recent if (r.usage or {}).get("closure")) >= MAX_CLOSURES_PER_SESSION:
        return None
    # 같은 세션에 이미 대기 중인 지시가 있으면 그 지시가 다음 단계의 소유자다.
    if any(r.id != run.id and r.status in (RunStatus.queued, RunStatus.running) for r in recent):
        return None
    prompt = WEAK_CLOSURE_PROMPT if info.get("weak_evidence") else CLOSURE_PROMPT
    closure = Run(session_id=sess.id, workspace_id=sess.workspace_id, engine=sess.engine, model=sess.model,
                  effort=sess.effort, origin=RunOrigin.orchestrator, prompt=prompt,
                  status=RunStatus.queued, usage={"closure": {"source_run_id": str(run.id)}})
    db.add(closure)
    await db.flush()
    run.usage = {**(run.usage or {}), "completion": {**info, "closure_run_id": str(closure.id)}}
    events.append(db, closure, "run.status", {"status": RunStatus.queued, "closure": True})
    events.append(db, run, "completion.closure", {"run_id": str(closure.id)})
    return closure


def report_line(info: dict[str, Any] | None) -> str | None:
    """오케스트레이터 보고에 붙일 한 줄."""
    if not info:
        return None
    if info.get("violation"):
        if info.get("weak_evidence"):
            if info.get("closure_run_id"):
                return ("완료 계약 위반: 상태=완료로 선언했으나 실행 증거가 없다. 자동 완료 확인 Run 이 이어서 실행 중이다. "
                        "이 보고만으로 완료로 판단하지 말고 다음 보고를 기다려라.")
            return "완료 계약 위반: 상태=완료로 선언했으나 실행 증거가 없다. 완료로 보지 말고 지시 항목별 증거를 확인한 뒤 후속 지시를 내려라."
        if info.get("closure_run_id"):
            return ("완료 계약 위반: 상태 줄이 없다. 자동 완료 확인 Run 이 이어서 실행 중이다. "
                    "이 보고만으로 완료로 판단하지 말고 다음 보고를 기다려라.")
        return "완료 계약 위반: 상태 줄이 없다. 완료로 보지 말고 지시 항목별 증거를 확인한 뒤 후속 지시를 내려라."
    if info["status"] == "완료":
        return "완료 계약: 상태=완료. 보고의 실행 증거가 완료 기준을 덮는지 확인하라."
    if advice := REMAINING_ADVICE.get(info.get("remaining") or ""):
        return f"완료 계약: 상태={info['status']}, 남은 일={info['remaining']}. {advice}"
    return (f"완료 계약: 상태={info['status']}. 미충족 완료 기준을 지목해 후속 지시를 내리고, "
            "두 번 진전이 없으면 재계획하라.")

