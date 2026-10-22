"""장면별 사용자 피드백 → 그 샷만 다시 만드는 흐름.

사용자의 글 피드백은 (1) 사용자가 직접 준 엔진·모델(프로필)·매개변수·seed 덮어쓰기 또는 (2) 창작 감독 LLM의 변경 제안으로 바뀐다.
어느 쪽이든 새 샷 revision 하나와 새 attempt 하나만 만들며, 다른 샷의 revision·출력 hash·타임라인·바이블은 건드리지 않는다.
피드백 원문과 결정 주체(user-overrides|director)와 결정 이유는 sidecar에 불변으로 남는다. 승인(approval)은 만들지 않는다.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ttakkari.creative.direction import CreativeDirector
from ttakkari.creative.director import ConflictError
from ttakkari.creative.pipeline import PipelineError, ProductionPipeline


def feedback_id(
    pid: str, shot_id: str, expected_revision: int, idempotency_key: str
) -> str:
    raw = f"{pid}|{shot_id}|{expected_revision}|{idempotency_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def submit_feedback(
    pipeline: ProductionPipeline,
    director: CreativeDirector | None,
    pid: str,
    shot_id: str,
    *,
    expected_revision: int,
    text: str,
    idempotency_key: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """피드백을 기록하고 새 샷 revision과 queued attempt를 만든다(실행은 호출 쪽이 이어서 한다)."""
    store = pipeline.store
    if not text.strip():
        raise PipelineError("피드백 내용이 비어 있습니다.")
    state = store.get_production(pid)
    if state["cancelled"]:
        raise PipelineError("취소된 제작에는 재제작을 시작할 수 없습니다.")
    if shot_id not in state["shots"]:
        raise PipelineError("샷이 없습니다.")
    fid = feedback_id(pid, shot_id, expected_revision, idempotency_key)
    prior = state.get("feedback", {}).get(fid)
    current = state["shots"][shot_id][-1]["revision"]
    if prior is None and current != expected_revision:
        raise ConflictError("expected_shot_revision이 현재 샷 revision과 다릅니다.")
    if prior is not None:
        changes, decided_by, reason = (
            prior["changes"],
            prior["decided_by"],
            prior["reason"],
        )
    else:
        if overrides:
            changes, decided_by, reason = (
                dict(overrides),
                "user-overrides",
                "사용자가 지정한 값",
            )
            selection = "explicit-lab"
        elif director is not None:
            proposal = await director.propose_revision(pid, shot_id, text)
            changes = proposal.model_dump(exclude={"reason"}, exclude_defaults=True)
            decided_by, reason = "director", proposal.reason
            selection = "auto"
        else:
            raise PipelineError("덮어쓸 값이 없고 감독도 연결되지 않았습니다.")
        prior = store.record_sidecar(
            pid,
            "feedback",
            fid,
            {
                "feedback_id": fid,
                "shot_id": shot_id,
                "from_revision": expected_revision,
                "text": text,
                "changes": changes,
                "decided_by": decided_by,
                "reason": reason,
                "approval": None,
            },
        )
    if current == expected_revision:
        selection = "explicit-lab" if decided_by == "user-overrides" else "auto"
        pipeline.revise_shot(
            pid,
            shot_id,
            expected_revision=expected_revision,
            engine=changes.get("engine"),
            profile=changes.get("profile"),
            params=changes.get("params"),
            drop_params=changes.get("drop_params"),
            seed=changes.get("seed"),
            intent=changes.get("intent"),
            selection=selection,
            reason=reason,
            source="user-feedback",
        )
    elif current != expected_revision + 1:
        raise ConflictError("피드백 처리 중 샷이 다른 변경으로 바뀌었습니다.")
    attempt = pipeline.submit_shot(pid, shot_id)
    return {
        "feedback_id": fid,
        "shot_id": shot_id,
        "from_revision": expected_revision,
        "to_revision": expected_revision + 1,
        "attempt_id": attempt["attempt_id"],
        "state": attempt["state"],
        "decided_by": decided_by,
        "reason": reason,
        "changes": changes,
    }
