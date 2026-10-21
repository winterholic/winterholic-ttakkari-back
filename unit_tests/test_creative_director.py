from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


def _service(tmp_path):
    from ttakkari.creative.director import DirectorStore

    return DirectorStore(tmp_path)


def _production(store):
    return store.create_production("밤에서 새벽으로")


def test_versioned_contracts_rational_fps_and_half_open_sample_conversion(tmp_path):
    from ttakkari.creative.director import TimelineRevision

    assert (
        TimelineRevision.sample_to_frame(
            48_000, sample_rate=48_000, fps_num=24_000, fps_den=1_001
        )
        == 24
    )
    assert (
        TimelineRevision.sample_to_frame(
            24_000, sample_rate=48_000, fps_num=24_000, fps_den=1_001
        )
        == 12
    )
    with pytest.raises(ValueError):
        TimelineRevision(fps_num=0, fps_den=1, duration_frames=10, sample_rate=48_000)
    with pytest.raises(ValueError, match="반열림"):
        TimelineRevision(
            fps_num=24,
            fps_den=1,
            duration_frames=10,
            sample_rate=48_000,
            shots=[{"shot_id": "s1", "start_f": 0, "end_f": 11}],
        )


def test_expected_revision_is_cas_and_stale_scope_is_bounded(tmp_path):
    store = _service(tmp_path)
    production = _production(store)
    pid = production["manifest"]["production_id"]
    for record in [
        production["manifest"],
        production["bibles"][0],
        production["timelines"][0],
        production["shots"]["shot-1"][0],
        production["workflows"][0],
    ]:
        assert record["schema_version"] == 1 and len(record["content_hash"]) == 64

    updated = store.update_bible(pid, expected_revision=1, values={"palette": ["blue"]})
    assert updated["revision"] == 2
    with pytest.raises(ConflictError):
        store.update_bible(pid, expected_revision=1, values={"palette": ["red"]})
    with pytest.raises(ValueError, match="승인"):
        store.update_bible(
            pid, expected_revision=2, values={"approval_event_id": "forged"}
        )
    with pytest.raises(ValueError, match="수정할 수 없는 필드"):
        store.update_bible(pid, expected_revision=2, values={"revision": 40})

    shot = store.update_shot(
        pid, "shot-1", expected_revision=1, values={"intent": "wide"}
    )
    assert shot["revision"] == 2
    state = store.get_production(pid)
    assert state["stale"]["shots"] == ["shot-1"]
    assert state["stale"]["transition_neighbors"] == ["shot-2"]
    assert state["stale"]["reviews"] == ["bible", "production"]

    store.update_timeline(
        pid,
        expected_revision=1,
        values={
            "duration_frames": 96,
            "shots": [
                {"shot_id": "shot-1", "start_f": 0, "end_f": 48},
                {"shot_id": "shot-2", "start_f": 48, "end_f": 96},
            ],
        },
    )
    state = store.get_production(pid)
    assert "timeline" in state["stale"]["reviews"]
    assert state["stale"]["shots"] == ["shot-1", "shot-2"]
    manifest_revision = state["manifest"]["manifest_revision"]
    changed_manifest = store.update_manifest(
        pid, expected_revision=manifest_revision, values={"policy_ref": "research"}
    )
    assert changed_manifest["manifest_revision"] == manifest_revision + 1
    with pytest.raises(ConflictError):
        store.update_manifest(
            pid, expected_revision=manifest_revision, values={"policy_ref": "old"}
        )
    with pytest.raises(ValueError, match="수정할 수 없는 필드"):
        store.update_manifest(
            pid,
            expected_revision=changed_manifest["manifest_revision"],
            values={"unexpected": "value"},
        )


def test_manifest_artifacts_are_cas_atomic_and_reject_symlink_escape_and_name_collision(
    tmp_path,
):
    store = _service(tmp_path)
    production = _production(store)
    pid = production["manifest"]["production_id"]
    source = b"existing-media-bytes"
    artifact = store.put_artifact(pid, "reference.mp4", source, media_type="video/mp4")
    assert artifact["sha256"] == hashlib.sha256(source).hexdigest()
    assert store.read_artifact(pid, artifact["sha256"]) == source
    with pytest.raises(ValueError, match="동명"):
        store.put_artifact(pid, "reference.mp4", b"different", media_type="video/mp4")

    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    (store.production_path(pid) / "artifacts" / ("f" * 64)).symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        store.read_artifact(pid, "f" * 64)
    with pytest.raises(ValueError):
        store.read_artifact(pid, "../outside")


def test_attempt_is_persisted_before_execute_recovered_after_restart_and_empty_output_fails(
    tmp_path,
):
    store = _service(tmp_path)
    production = _production(store)
    pid = production["manifest"]["production_id"]
    media = store.put_artifact(
        pid, "input.mp4", b"existing-media", media_type="video/mp4"
    )
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=media["sha256"], idempotency_key="idem-1"
    )
    assert attempt["state"] == "queued"
    reopened = _service(tmp_path)
    assert (
        reopened.get_production(pid)["attempts"][0]["attempt_id"]
        == attempt["attempt_id"]
    )
    with reopened._locked_state(pid) as state:
        state["attempts"][0].update(
            state="running",
            execution_claim={"claim_id": "process-that-crashed"},
            updated_at="crash-boundary",
        )
        reopened._seal_attempt(state["attempts"][0])
    resumed = reopened.resume_pending(pid, renderer=lambda _input: [b"rendered-media"])
    assert resumed[0]["state"] == "review_pending"
    assert (
        resumed[0]["outputs"][0]["sha256"]
        == hashlib.sha256(b"rendered-media").hexdigest()
    )
    assert resumed[0]["execution_claim"] is None
    assert (
        reopened.read_artifact(pid, resumed[0]["outputs"][0]["sha256"])
        == b"rendered-media"
    )

    empty = reopened.submit_attempt(
        pid, "shot-2", input_sha256=media["sha256"], idempotency_key="idem-empty"
    )
    failed = reopened.execute_attempt(
        pid, empty["attempt_id"], renderer=lambda _input: []
    )
    assert failed["state"] == "failed"
    assert failed["error_kind"] == "empty_output"
    assert failed["outputs"] == []


def test_collection_failure_keeps_prior_success_and_idempotency_key_cannot_change_inputs(
    tmp_path, monkeypatch
):
    store = _service(tmp_path)
    pid = _production(store)["manifest"]["production_id"]
    source = store.put_artifact(pid, "source.mp4", b"source", media_type="video/mp4")
    prior = store.put_artifact(
        pid,
        "prior.bin",
        b"complete-prior-output",
        media_type="application/octet-stream",
    )
    first = store.submit_attempt(
        pid, "shot-1", input_sha256=source["sha256"], idempotency_key="stable"
    )
    with pytest.raises(ValueError, match="다른 입력"):
        store.submit_attempt(
            pid, "shot-2", input_sha256=prior["sha256"], idempotency_key="stable"
        )
    collecting = store.submit_attempt(
        pid, "shot-2", input_sha256=source["sha256"], idempotency_key="collect-fails"
    )
    monkeypatch.setattr(
        store,
        "put_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    failed = store.execute_attempt(
        pid, collecting["attempt_id"], renderer=lambda _input: [b"new-output"]
    )
    assert failed["state"] == "failed" and failed["error_kind"] == "collect_failed"
    assert store.read_artifact(pid, prior["sha256"]) == b"complete-prior-output"
    assert store.get_production(pid)["attempts"][0]["attempt_id"] == first["attempt_id"]


def test_partial_collection_failure_preserves_recovered_files_without_marking_success(
    tmp_path,
):
    store = _service(tmp_path)
    pid = _production(store)["manifest"]["production_id"]
    source = store.put_artifact(pid, "source.mp4", b"source", media_type="video/mp4")
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=source["sha256"], idempotency_key="partial"
    )
    result = store.execute_attempt(
        pid, attempt["attempt_id"], renderer=lambda _input: [b"recovered", b""]
    )
    assert result["state"] == "failed"
    assert result["error_kind"] == "empty_output"
    assert len(result["outputs"]) == 1
    assert store.read_artifact(pid, result["outputs"][0]["sha256"]) == b"recovered"
    with pytest.raises(ValueError, match="검수 대상"):
        store.create_review(
            pid,
            output_sha256=result["outputs"][0]["sha256"],
            approval_event_id=None,
        )


def test_duplicate_and_late_events_are_idempotent_and_cancel_blocks_resubmission(
    tmp_path,
):
    store = _service(tmp_path)
    pid = _production(store)["manifest"]["production_id"]
    media = store.put_artifact(pid, "input.mp4", b"existing", media_type="video/mp4")
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=media["sha256"], idempotency_key="same"
    )
    assert (
        store.submit_attempt(
            pid, "shot-1", input_sha256=media["sha256"], idempotency_key="same"
        )["attempt_id"]
        == attempt["attempt_id"]
    )
    finished = store.execute_attempt(
        pid, attempt["attempt_id"], renderer=lambda _input: [b"clip"]
    )
    repeated = store.ingest_event(
        pid, attempt["attempt_id"], event_id="done-1", sequence=1, state="succeeded"
    )
    repeated_again = store.ingest_event(
        pid, attempt["attempt_id"], event_id="done-1", sequence=1, state="succeeded"
    )
    late = store.ingest_event(
        pid, attempt["attempt_id"], event_id="late-1", sequence=2, state="running"
    )
    assert repeated["state"] == "review_pending"
    assert repeated_again["state"] == "review_pending"
    assert late["state"] == "review_pending"
    assert finished["state"] == "review_pending"
    done_events = [
        event
        for event in store.get_production(pid)["events"]
        if event["event_id"] == "done-1"
    ]
    assert len(done_events) == 1

    empty = store.submit_attempt(
        pid, "shot-2", input_sha256=media["sha256"], idempotency_key="cancel-me"
    )
    assert (
        store.ingest_event(
            pid,
            empty["attempt_id"],
            event_id="empty-done",
            sequence=1,
            state="succeeded",
        )["error_kind"]
        == "empty_output"
    )
    ordered = store.submit_attempt(
        pid, "shot-2", input_sha256=media["sha256"], idempotency_key="ordered-events"
    )
    assert (
        store.ingest_event(
            pid, ordered["attempt_id"], event_id="event-5", sequence=5, state="running"
        )["state"]
        == "running"
    )
    assert (
        store.ingest_event(
            pid, ordered["attempt_id"], event_id="event-4", sequence=4, state="queued"
        )["state"]
        == "running"
    )
    pending = store.submit_attempt(
        pid, "shot-1", input_sha256=media["sha256"], idempotency_key="cancel-me-too"
    )
    cancelled = store.cancel_attempt(pid, pending["attempt_id"])
    assert cancelled["state"] == "cancelled"
    with pytest.raises(ValueError, match="취소"):
        store.submit_attempt(
            pid, "shot-1", input_sha256=media["sha256"], idempotency_key="after-cancel"
        )
    event_ids = [e["event_id"] for e in store.get_production(pid)["events"]]
    assert len(event_ids) == len(set(event_ids))


def test_concurrent_execute_has_one_renderer_and_releases_waiting_duplicate(tmp_path):
    store = _service(tmp_path)
    pid = _production(store)["manifest"]["production_id"]
    media = store.put_artifact(
        pid, "input.bin", b"input", media_type="application/octet-stream"
    )
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=media["sha256"], idempotency_key="concurrent"
    )
    renderer_entered = threading.Event()
    second_renderer_entered = threading.Event()
    release_renderer = threading.Event()
    second_call_entered = threading.Event()
    calls = []

    def renderer(data):
        calls.append(data)
        if len(calls) == 1:
            renderer_entered.set()
        else:
            second_renderer_entered.set()
        assert release_renderer.wait(timeout=3)
        return [b"output"]

    original_execute = store.execute_attempt

    def second_call():
        second_call_entered.set()
        return original_execute(pid, attempt["attempt_id"], renderer=renderer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            original_execute, pid, attempt["attempt_id"], renderer=renderer
        )
        assert renderer_entered.wait(timeout=3)
        second = pool.submit(second_call)
        assert second_call_entered.wait(timeout=3)
        assert not second_renderer_entered.wait(timeout=0.1)
        release_renderer.set()
        results = [first.result(timeout=3), second.result(timeout=3)]

    assert len(calls) == 1
    assert [result["state"] for result in results] == ["review_pending"] * 2


def test_higher_sequence_regressive_event_does_not_move_state_backwards(tmp_path):
    store = _service(tmp_path)
    pid = _production(store)["manifest"]["production_id"]
    media = store.put_artifact(
        pid, "input.bin", b"input", media_type="application/octet-stream"
    )
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=media["sha256"], idempotency_key="monotonic"
    )
    running = store.ingest_event(
        pid, attempt["attempt_id"], event_id="running-1", sequence=1, state="running"
    )

    regressive = store.ingest_event(
        pid, attempt["attempt_id"], event_id="queued-2", sequence=2, state="queued"
    )

    assert running["state"] == regressive["state"] == "running"
    assert regressive["last_event_sequence"] == 2
    events = store.get_production(pid)["events"]
    assert any(event["kind"] == "engine.invalid_transition_ignored" for event in events)


@pytest.mark.parametrize("damage", ["missing", "tampered", "partial"])
def test_succeeded_event_requires_every_cas_output_to_exist_and_match_hash(
    tmp_path, damage
):
    store = _service(tmp_path)
    pid = _production(store)["manifest"]["production_id"]
    media = store.put_artifact(
        pid, "input.bin", b"input", media_type="application/octet-stream"
    )
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=media["sha256"], idempotency_key=f"receipt-{damage}"
    )
    output = store.put_artifact(
        pid, "output.bin", b"collected", media_type="application/octet-stream"
    )
    with store._locked_state(pid) as state:
        receipt = next(
            row
            for row in state["attempts"]
            if row["attempt_id"] == attempt["attempt_id"]
        )
        receipt.update(
            state="collecting",
            expected_output_count=2 if damage == "partial" else 1,
            outputs=[output],
        )
        store._seal_attempt(receipt)
    output_path = store.production_path(pid) / output["path"]
    if damage == "missing":
        output_path.unlink()
    elif damage == "tampered":
        output_path.write_bytes(b"changed")

    result = store.ingest_event(
        pid,
        attempt["attempt_id"],
        event_id=f"succeeded-{damage}",
        sequence=1,
        state="succeeded",
    )

    assert result["state"] == "failed"
    assert result["error_kind"] in {
        "missing_output",
        "output_integrity_failed",
        "incomplete_collection",
    }
    assert result["error_details"]


def test_review_acceptance_requires_a_real_user_approval_event(tmp_path):
    store = _service(tmp_path)
    pid = _production(store)["manifest"]["production_id"]
    with pytest.raises(ValueError, match="승인"):
        store.create_review(pid, output_sha256="0" * 64, approval_event_id="forged")
    media = store.put_artifact(pid, "input.mp4", b"existing", media_type="video/mp4")
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=media["sha256"], idempotency_key="review"
    )
    attempt = store.execute_attempt(
        pid, attempt["attempt_id"], renderer=lambda _input: [b"review-output"]
    )
    review = store.create_review(
        pid, output_sha256=attempt["outputs"][0]["sha256"], approval_event_id=None
    )
    assert review["approval_event_id"] is None


async def test_authenticated_router_connects_cpu_service_without_http_or_gpu(
    tmp_path, monkeypatch
):
    import httpx
    from fastapi import FastAPI

    from ttakkari.api import creative as api
    from ttakkari.creative.director import DirectorStore
    from ttakkari.security.auth import require_user

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    monkeypatch.setattr(api, "default_director_store", lambda: DirectorStore(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/creative/productions", json={"brief": "CPU route"}
        )
        assert created.status_code == 201
        pid = created.json()["manifest"]["production_id"]
        uploaded = await client.post(
            f"/api/creative/productions/{pid}/artifacts",
            files={"file": ("existing.mp4", b"existing-media", "video/mp4")},
        )
        assert uploaded.status_code == 201
        response = await client.post(
            f"/api/creative/productions/{pid}/attempts",
            json={
                "shot_id": "shot-1",
                "input_sha256": uploaded.json()["sha256"],
                "idempotency_key": "cpu-route",
            },
        )
        assert (
            response.status_code == 202 and response.json()["state"] == "review_pending"
        )
        state = (await client.get(f"/api/creative/productions/{pid}")).json()
        assert state["attempts"][0]["outputs"][0]["sha256"] == uploaded.json()["sha256"]


from ttakkari.creative.director import ConflictError
