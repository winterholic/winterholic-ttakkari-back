"""G0 계약에 3·4단계가 더한 메서드(sidecar·샷 계획·stale 해제·검수 확장)의 양성/음성 대조.

기존 G0 규칙(CAS, expected revision, 승인 null)을 약화하지 않는지도 함께 확인한다.
"""

from __future__ import annotations

import pytest

from ttakkari.creative.director import ConflictError, DirectorStore


def _store(tmp_path):
    store = DirectorStore(tmp_path)
    pid = store.create_production("확장 시험")["manifest"]["production_id"]
    return store, pid


def _asset(store, pid, data=b"input"):
    return store.put_artifact(
        pid, f"in-{len(data)}.bin", data, media_type="application/octet-stream"
    )["sha256"]


def test_sidecar_is_append_only_per_key_and_restricted_to_known_sections(tmp_path):
    store, pid = _store(tmp_path)
    first = store.record_sidecar(pid, "feedback", "k1", {"text": "a"})
    again = store.record_sidecar(pid, "feedback", "k1", {"text": "a"})
    assert again == first
    with pytest.raises(ConflictError):
        store.record_sidecar(pid, "feedback", "k1", {"text": "다른 내용"})
    with pytest.raises(ValueError, match="허용되지 않은"):
        store.record_sidecar(pid, "manifest", "k1", {"x": 1})
    with pytest.raises(ValueError, match="키"):
        store.record_sidecar(pid, "feedback", "", {"x": 1})
    assert store.get_production(pid)["feedback"]["k1"]["text"] == "a"


def test_shot_plan_must_match_timeline_and_unchanged_plan_keeps_every_hash(tmp_path):
    store, pid = _store(tmp_path)
    timeline = store.update_timeline(
        pid,
        expected_revision=1,
        values={
            "shots": [
                {"shot_id": "a", "start_f": 0, "end_f": 24},
                {"shot_id": "b", "start_f": 24, "end_f": 48},
            ],
            "duration_frames": 48,
        },
    )
    plan = [
        {
            "shot_id": "a",
            "start_f": 0,
            "end_f": 24,
            "intent": "A",
            "engine": "x",
            "profile": "p",
        },
        {
            "shot_id": "b",
            "start_f": 24,
            "end_f": 48,
            "intent": "B",
            "engine": "x",
            "profile": "p",
        },
    ]
    with pytest.raises(ValueError, match="정확히 같아야"):
        store.set_shot_plan(
            pid,
            expected_timeline_revision=timeline["revision"],
            shots=[plan[0], {**plan[1], "end_f": 47}],
        )
    with pytest.raises(ValueError, match="수정할 수 없는 필드"):
        store.set_shot_plan(
            pid,
            expected_timeline_revision=timeline["revision"],
            shots=[{**plan[0], "revision": 9}, plan[1]],
        )
    with pytest.raises(ConflictError):
        store.set_shot_plan(
            pid, expected_timeline_revision=timeline["revision"] - 1, shots=plan
        )
    planned = store.set_shot_plan(
        pid, expected_timeline_revision=timeline["revision"], shots=plan
    )
    hashes = {s["shot_id"]: (s["revision"], s["content_hash"]) for s in planned}
    again = store.set_shot_plan(
        pid, expected_timeline_revision=timeline["revision"], shots=plan
    )
    assert {
        s["shot_id"]: (s["revision"], s["content_hash"]) for s in again
    } == hashes  # 같은 계획은 새 revision을 만들지 않는다
    changed = store.set_shot_plan(
        pid,
        expected_timeline_revision=timeline["revision"],
        shots=[plan[0], {**plan[1], "intent": "B2"}],
    )
    assert (
        changed[0]["revision"] == planned[0]["revision"]
        and changed[1]["revision"] == planned[1]["revision"] + 1
    )
    state = store.get_production(pid)
    assert state["manifest"]["active_shot_revisions"] == {
        "a": changed[0]["revision"],
        "b": changed[1]["revision"],
    }
    assert (
        "b" in state["stale"]["shots"] and "a" in state["stale"]["transition_neighbors"]
    )


def test_shot_with_attempts_cannot_be_dropped_from_a_new_plan(tmp_path):
    store, pid = _store(tmp_path)
    sha = _asset(store, pid)
    store.submit_attempt(pid, "shot-1", input_sha256=sha, idempotency_key="k")
    timeline = store.update_timeline(
        pid,
        expected_revision=1,
        values={"shots": [{"shot_id": "shot-2", "start_f": 0, "end_f": 48}]},
    )
    with pytest.raises(ValueError, match="실행 기록이 있는 샷"):
        store.set_shot_plan(
            pid,
            expected_timeline_revision=timeline["revision"],
            shots=[{"shot_id": "shot-2", "start_f": 0, "end_f": 48}],
        )


def test_resolve_shot_stale_only_for_a_review_pending_attempt_of_the_current_revisions(
    tmp_path,
):
    store, pid = _store(tmp_path)
    sha = _asset(store, pid)
    store.update_shot(pid, "shot-1", expected_revision=1, values={"intent": "바뀜"})
    assert "shot-1" in store.get_production(pid)["stale"]["shots"]
    assert store.resolve_shot_stale(pid, "shot-1") is False  # 출력이 없다
    attempt = store.submit_attempt(pid, "shot-1", input_sha256=sha, idempotency_key="k")
    assert store.resolve_shot_stale(pid, "shot-1") is False  # queued는 출력이 아니다
    store.execute_attempt(pid, attempt["attempt_id"], renderer=lambda source: [source])
    store.update_bible(
        pid, expected_revision=1, values={"palette": ["#000000"]}
    )  # 시도 이후 바이블이 바뀜
    assert store.resolve_shot_stale(pid, "shot-1") is False
    with pytest.raises(ValueError, match="샷이 없습니다"):
        store.resolve_shot_stale(pid, "nope")


def test_review_evidence_must_be_known_artifacts_and_attempt_binding_is_explicit(
    tmp_path,
):
    store, pid = _store(tmp_path)
    sha = _asset(store, pid)
    first = store.submit_attempt(pid, "shot-1", input_sha256=sha, idempotency_key="k1")
    second = store.submit_attempt(pid, "shot-2", input_sha256=sha, idempotency_key="k2")
    for attempt in (first, second):
        store.execute_attempt(
            pid, attempt["attempt_id"], renderer=lambda source: [source]
        )
    with pytest.raises(ValueError, match="증거 URI"):
        store.create_review(
            pid,
            output_sha256=sha,
            approval_event_id=None,
            evidence_uris=["artifact:" + "0" * 64],
        )
    with pytest.raises(ValueError, match="증거 URI"):
        store.create_review(
            pid,
            output_sha256=sha,
            approval_event_id=None,
            evidence_uris=["file:///etc/passwd"],
        )
    # 두 샷이 같은 바이트를 냈을 때 attempt_id로 대상 시도를 지정하면 그 시도의 revision에 묶인다.
    review = store.create_review(
        pid,
        output_sha256=sha,
        approval_event_id=None,
        attempt_id=second["attempt_id"],
        reviewer="machine-check/v1",
    )
    assert (
        review["shot_revision"] == second["shot_revision"]
        and review["reviewer"] == "machine-check/v1"
    )
    with pytest.raises(ValueError, match="실행 receipt"):
        store.create_review(
            pid,
            output_sha256=sha,
            approval_event_id=None,
            attempt_id="00000000-0000-4000-8000-000000000000",
        )
    with pytest.raises(ValueError, match="승인"):
        store.create_review(pid, output_sha256=sha, approval_event_id="forged")


def test_final_stale_is_resolved_only_when_composition_matches_current_outputs_and_has_a_review(
    tmp_path,
):
    store, pid = _store(tmp_path)
    assert store.resolve_final_stale(pid, "missing") is False
    sha = _asset(store, pid)
    outputs = {}
    for shot_id in ("shot-1", "shot-2"):
        attempt = store.submit_attempt(
            pid, shot_id, input_sha256=sha, idempotency_key=shot_id
        )
        done = store.execute_attempt(
            pid, attempt["attempt_id"], renderer=lambda source: [source]
        )
        outputs[shot_id] = done["outputs"][0]["sha256"]
        store.resolve_shot_stale(pid, shot_id)
    final_sha = store.put_artifact(
        pid, "final.bin", b"final-bytes", media_type="video/mp4"
    )["sha256"]
    store.record_sidecar(
        pid,
        "compositions",
        "c1",
        {"output_sha256": final_sha, "shot_outputs": {**outputs, "shot-2": "0" * 64}},
    )
    assert store.resolve_final_stale(pid, "c1") is False  # 출력 매핑이 다르다
    store.record_sidecar(
        pid, "compositions", "c2", {"output_sha256": final_sha, "shot_outputs": outputs}
    )
    assert store.resolve_final_stale(pid, "c2") is False  # 합성물에 대한 검수가 없다
    with pytest.raises(ValueError, match="합성 기록"):
        store.create_review(
            pid, output_sha256=sha, approval_event_id=None, composition_key="c2"
        )
    store.create_review(
        pid, output_sha256=final_sha, approval_event_id=None, composition_key="c2"
    )
    assert store.resolve_final_stale(pid, "c2") is True
    state = store.get_production(pid)
    assert (
        state["stale"]["transition_neighbors"] == []
        and "production" not in state["stale"]["reviews"]
    )
    assert (
        state["reviews"][-1]["shot_revision"] is None
        and state["reviews"][-1]["approval_event_id"] is None
    )
