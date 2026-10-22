"""G34 독립 검수가 재현한 결함 3건(G0 출력 재개, unattached 직렬화, sidecar 예약 필드)의 회귀 시험.

I2V 업로드 TOCTOU(네 번째 결함)는 컨트롤러 fixture를 쓰는 `test_comfy_i2v_input.py`에 있다.
"""

from __future__ import annotations

import threading

import pytest

from ttakkari.creative.director import DirectorStore, _hash
from ttakkari.creative.pipeline import ProductionPipeline


def _store(tmp_path):
    store = DirectorStore(tmp_path)
    pid = store.create_production("g34 fixes")["manifest"]["production_id"]
    return store, pid


# ---------- P1: 미디어 출력 저장 뒤 receipt 저장 전에 죽은 attempt의 재개


def _crashed_before_receipt(store, pid, monkeypatch):
    source = store.put_artifact(
        pid, "input.bin", b"input", media_type="application/octet-stream"
    )
    attempt = store.submit_attempt(
        pid,
        "shot-1",
        input_sha256=source["sha256"],
        idempotency_key="shot-1:receipt-crash",
        resolved_graph={"engine": "test", "profile": "test"},
    )
    real_put = store.put_artifact
    receipt_name = f"{attempt['attempt_id']}-1.bin"

    def die_before_receipt(production_id, name, content, *, media_type):
        if name == receipt_name:
            raise KeyboardInterrupt("simulated process death before receipt")
        return real_put(production_id, name, content, media_type=media_type)

    with monkeypatch.context() as patch:
        patch.setattr(store, "put_artifact", die_before_receipt)
        with pytest.raises(KeyboardInterrupt):
            store.execute_attempt(
                pid,
                attempt["attempt_id"],
                renderer=lambda _data: [b"video version A", b'{"receipt":"A"}'],
            )
    return attempt["attempt_id"]


def test_resume_after_crash_between_media_and_receipt_with_other_bytes_recovers(
    tmp_path, monkeypatch
):
    store, pid = _store(tmp_path)
    attempt_id = _crashed_before_receipt(store, pid, monkeypatch)
    crashed = next(
        a
        for a in store.get_production(pid)["attempts"]
        if a["attempt_id"] == attempt_id
    )
    # 사전 조건: 미디어만 있고 receipt는 없는 반쪽 상태가 실제로 만들어졌다.
    assert crashed["state"] == "collecting" and len(crashed["outputs"]) == 1

    resumed = store.execute_attempt(
        pid,
        attempt_id,
        renderer=lambda _data: [b"video version B", b'{"receipt":"B"}'],
    )

    assert resumed["state"] == "review_pending" and resumed["error_kind"] is None
    assert len(resumed["outputs"]) == 2
    assert [store.read_artifact(pid, o["sha256"]) for o in resumed["outputs"]] == [
        b"video version B",
        b'{"receipt":"B"}',
    ]
    state = store.get_production(pid)
    assert any(
        e["kind"] == "attempt.stale_outputs_discarded" and e["attempt_id"] == attempt_id
        for e in state["events"]
    )
    # 현재 manifest는 새 출력만 가리킨다(버린 A 항목이 남지 않는다).
    names = {n: a["sha256"] for n, a in state["artifacts"].items()}
    assert names[f"{attempt_id}-0.bin"] == resumed["outputs"][0]["sha256"]


def test_resume_with_identical_bytes_is_idempotent_and_leaves_a_clean_receipt(
    tmp_path, monkeypatch
):
    store, pid = _store(tmp_path)
    attempt_id = _crashed_before_receipt(store, pid, monkeypatch)
    resumed = store.execute_attempt(
        pid,
        attempt_id,
        renderer=lambda _data: [b"video version A", b'{"receipt":"A"}'],
    )
    assert resumed["state"] == "review_pending" and len(resumed["outputs"]) == 2


def test_completed_attempt_outputs_are_never_discarded_by_a_later_call(tmp_path):
    store, pid = _store(tmp_path)
    source = store.put_artifact(
        pid, "input.bin", b"input", media_type="application/octet-stream"
    )
    attempt = store.submit_attempt(
        pid, "shot-1", input_sha256=source["sha256"], idempotency_key="done"
    )
    done = store.execute_attempt(
        pid, attempt["attempt_id"], renderer=lambda _d: [b"clip", b"receipt"]
    )
    again = store.execute_attempt(
        pid, attempt["attempt_id"], renderer=lambda _d: [b"other", b"other receipt"]
    )
    assert again["outputs"] == done["outputs"] and again["state"] == "review_pending"


# ---------- P2: run_unattached의 같은 키 동시 호출


def _fake_adapter(calls: dict, barrier: threading.Barrier | None):
    lock = threading.Lock()

    def run(_pid, _spec, *, attempt_id):
        assert attempt_id is None
        with lock:
            calls["n"] += 1
        if barrier is not None:
            try:
                barrier.wait(
                    timeout=1
                )  # 직렬화되면 두 번째 호출이 없어 시간 초과로 풀린다
            except threading.BrokenBarrierError:
                pass
        return [b"identical external result", b'{"kind":"engine-receipt"}'], {
            "outputs": [{"name": "audio.wav", "media_type": "audio/wav"}],
            "execution_mode": "real",
            "real_execution": True,
        }

    return run


def test_concurrent_calls_with_one_key_execute_the_engine_exactly_once(tmp_path):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    calls = {"n": 0}
    pipeline._run_adapter = _fake_adapter(calls, threading.Barrier(2))
    spec = {"engine": "tts", "profile": "speech", "idempotency_key": "speech:same"}
    results: list[dict] = []
    errors: list[BaseException] = []

    def work():
        try:
            results.append(
                pipeline.run_unattached(
                    pid, section="engine_runs", key="speech:same", spec=spec
                )
            )
        except BaseException as exc:  # noqa: BLE001 - 시험이 실패 원인을 그대로 보인다
            errors.append(exc)

    workers = [threading.Thread(target=work) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)

    assert errors == []
    assert calls["n"] == 1
    assert len(results) == 4 and len({r["content_hash"] for r in results}) == 1


def test_different_keys_do_not_block_each_other(tmp_path):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    calls = {"n": 0}
    barrier = threading.Barrier(2)  # 두 키가 동시에 어댑터에 들어와야 풀린다
    pipeline._run_adapter = _fake_adapter(calls, barrier)
    done = []

    def work(key):
        spec = {"engine": "tts", "profile": "speech", "idempotency_key": key}
        done.append(
            pipeline.run_unattached(pid, section="engine_runs", key=key, spec=spec)
        )

    workers = [
        threading.Thread(target=work, args=(k,)) for k in ("speech:a", "speech:b")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert calls["n"] == 2 and len(done) == 2 and not barrier.broken


def test_a_failed_first_execution_does_not_leave_the_key_locked_or_recorded(tmp_path):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    attempts = {"n": 0}

    def flaky(_pid, _spec, *, attempt_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("engine died")
        return [b"result", b"receipt"], {
            "outputs": [{"name": "a.wav", "media_type": "audio/wav"}],
            "execution_mode": "real",
            "real_execution": True,
        }

    pipeline._run_adapter = flaky
    spec = {"engine": "tts", "profile": "speech", "idempotency_key": "speech:flaky"}
    with pytest.raises(RuntimeError):
        pipeline.run_unattached(
            pid, section="engine_runs", key="speech:flaky", spec=spec
        )
    record = pipeline.run_unattached(
        pid, section="engine_runs", key="speech:flaky", spec=spec
    )
    assert attempts["n"] == 2 and record["key"] == "speech:flaky"


# ---------- P2: sidecar 예약 필드


def test_same_payload_with_a_caller_supplied_content_hash_is_idempotent(tmp_path):
    store, pid = _store(tmp_path)
    payload = {"result": "same payload"}
    first = store.record_sidecar(pid, "engine_runs", "k", payload)
    assert first["content_hash"] == _hash(payload)
    # 저장된 계산값을 그대로 돌려주는 호출자(같은 payload)는 멱등이다.
    again = store.record_sidecar(
        pid, "engine_runs", "k", {**payload, "content_hash": first["content_hash"]}
    )
    assert again == first


def test_caller_content_hash_that_disagrees_with_the_computed_value_is_a_clear_error(
    tmp_path,
):
    store, pid = _store(tmp_path)
    with pytest.raises(ValueError, match="content_hash.*계산값"):
        store.record_sidecar(
            pid,
            "engine_runs",
            "k",
            {"result": "same payload", "content_hash": "caller-value"},
        )
    assert "k" not in store.get_production(pid).get("engine_runs", {})
    store.record_sidecar(pid, "engine_runs", "k", {"result": "same payload"})
    # 같은 키에 다른 내용은 여전히 ConflictError(G0 append-only 규칙 유지)
    from ttakkari.creative.director import ConflictError

    with pytest.raises(ConflictError):
        store.record_sidecar(pid, "engine_runs", "k", {"result": "different"})


def test_reserved_key_and_at_fields_cannot_override_storage_values(tmp_path):
    store, pid = _store(tmp_path)
    with pytest.raises(ValueError, match="key"):
        store.record_sidecar(pid, "engine_runs", "k", {"key": "other", "x": 1})
    with pytest.raises(ValueError, match="at"):
        store.record_sidecar(pid, "engine_runs", "k", {"at": "1999-01-01", "x": 1})
    ok = store.record_sidecar(pid, "engine_runs", "k", {"key": "k", "x": 1})
    assert ok["key"] == "k" and ok["at"] != "1999-01-01"
