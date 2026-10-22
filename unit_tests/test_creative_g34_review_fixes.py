"""G34 독립 검수가 재현한 결함 3건(G0 출력 재개, unattached 직렬화, sidecar 예약 필드)의 회귀 시험.

I2V 업로드 TOCTOU(네 번째 결함)는 컨트롤러 fixture를 쓰는 `test_comfy_i2v_input.py`에 있다.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time

import pytest

from ttakkari.creative.director import DirectorStore, _hash
from ttakkari.creative.pipeline import PipelineError, ProductionPipeline


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


def _multiprocess_unattached_worker(root, pid, key, calls_path, results):
    store = DirectorStore(root)
    pipeline = ProductionPipeline(store, registry=None)

    def run(_pid, _spec, *, attempt_id):
        assert attempt_id is None
        with open(calls_path, "a") as stream:
            stream.write(f"{os.getpid()}\n")
        time.sleep(0.2)
        return [b"process-shared output", b"process-shared receipt"], {
            "outputs": [{"name": "out.wav", "media_type": "audio/wav"}],
            "execution_mode": "real",
            "real_execution": True,
        }

    pipeline._run_adapter = run
    try:
        result = pipeline.run_unattached(
            pid,
            section="engine_runs",
            key=key,
            spec={"engine": "tts", "profile": "same", "input_refs": ["asset:one"]},
            extra={"voice": "A"},
        )
        results.put(("ok", result["content_hash"]))
    except Exception as exc:  # noqa: BLE001 - subprocess must report its error
        results.put(("error", repr(exc)))


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


def test_multiprocess_same_request_executes_once_and_returns_one_sidecar(tmp_path):
    store, pid = _store(tmp_path)
    calls_path = tmp_path / "adapter-calls.txt"
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    workers = [
        context.Process(
            target=_multiprocess_unattached_worker,
            args=(str(store.root), pid, "process-shared", str(calls_path), results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)

    rows = [results.get(timeout=2) for _ in workers]
    assert [worker.exitcode for worker in workers] == [0, 0]
    assert all(row[0] == "ok" for row in rows)
    assert len({row[1] for row in rows}) == 1
    assert len(calls_path.read_text().splitlines()) == 1


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
        pid,
        section="engine_runs",
        key="speech:flaky",
        spec=spec,
        retry_unknown_execution=True,
    )
    assert attempts["n"] == 2 and record["key"] == "speech:flaky"


def test_same_key_rejects_different_spec_extra_and_input_references(tmp_path):
    from ttakkari.creative.director import ConflictError

    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    calls = {"n": 0}

    def run(_pid, _spec, *, attempt_id):
        assert attempt_id is None
        calls["n"] += 1
        return [b"audio", b"receipt"], {
            "outputs": [{"name": "audio.wav", "media_type": "audio/wav"}],
            "execution_mode": "real",
            "real_execution": True,
        }

    pipeline._run_adapter = run
    request = {
        "engine": "tts",
        "profile": "speech",
        "input_assets": [{"asset_id": "line-1", "sha256": "a" * 64}],
    }
    pipeline.run_unattached(
        pid, section="engine_runs", key="same", spec=request, extra={"voice": "A"}
    )

    for changed_spec, changed_extra in (
        ({**request, "profile": "other"}, {"voice": "A"}),
        (request, {"voice": "B"}),
        (
            {**request, "input_assets": [{"asset_id": "line-1", "sha256": "b" * 64}]},
            {"voice": "A"},
        ),
    ):
        with pytest.raises(ConflictError, match="요청 내용"):
            pipeline.run_unattached(
                pid,
                section="engine_runs",
                key="same",
                spec=changed_spec,
                extra=changed_extra,
            )
    assert calls["n"] == 1


def test_concurrent_different_requests_with_one_key_execute_only_the_winner(tmp_path):
    from ttakkari.creative.director import ConflictError

    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    calls = {"n": 0}
    lock = threading.Lock()

    def run(_pid, _spec, *, attempt_id):
        assert attempt_id is None
        with lock:
            calls["n"] += 1
        time.sleep(0.1)
        return [b"winner", b"receipt"], {
            "outputs": [{"name": "out.wav", "media_type": "audio/wav"}],
            "execution_mode": "real",
            "real_execution": True,
        }

    pipeline._run_adapter = run
    requests = [
        {"engine": "tts", "profile": profile, "input_refs": [asset]}
        for profile, asset in (("one", "asset:a"), ("two", "asset:b"))
    ]
    results, errors = [], []

    def work(spec):
        try:
            results.append(
                pipeline.run_unattached(
                    pid,
                    section="engine_runs",
                    key="race",
                    spec=spec,
                    extra={"voice": "A"},
                )
            )
        except BaseException as exc:  # noqa: BLE001 - capture the losing request
            errors.append(exc)

    workers = [threading.Thread(target=work, args=(spec,)) for spec in requests]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert calls["n"] == 1 and len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], ConflictError)
    assert store.get_production(pid)["engine_runs"]["race"]["spec"] in requests


def test_checkpoint_recovers_same_request_after_sidecar_crash_without_rerun(
    tmp_path, monkeypatch
):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    outputs = iter((b"nondeterministic A", b"nondeterministic B"))
    calls = {"n": 0}

    def run(_pid, _spec, *, attempt_id):
        assert attempt_id is None
        calls["n"] += 1
        return [next(outputs), b'{"receipt":true}'], {
            "outputs": [{"name": "audio.wav", "media_type": "audio/wav"}],
            "execution_mode": "real",
            "real_execution": True,
        }

    pipeline._run_adapter = run
    spec = {"engine": "tts", "profile": "speech", "version": "same"}
    sibling = store.record_sidecar(
        pid, "engine_runs", "sibling", {"engine": "tts", "profile": "other"}
    )
    source = store.put_artifact(
        pid, "protected-input.bin", b"protected", media_type="application/octet-stream"
    )
    terminal = store.submit_attempt(
        pid,
        "shot-1",
        input_sha256=source["sha256"],
        idempotency_key="terminal-sibling",
        resolved_graph={"engine": "test", "profile": "test"},
    )
    terminal = store.execute_attempt(
        pid,
        terminal["attempt_id"],
        renderer=lambda _data: [b"terminal video", b"receipt"],
    )
    original = store.record_sidecar

    def crash_final_sidecar(production_id, section, key, value):
        if section == "engine_runs":
            raise KeyboardInterrupt("crash after durable output checkpoint")
        return original(production_id, section, key, value)

    monkeypatch.setattr(store, "record_sidecar", crash_final_sidecar)
    with pytest.raises(KeyboardInterrupt, match="durable output checkpoint"):
        pipeline.run_unattached(pid, section="engine_runs", key="checkpoint", spec=spec)

    monkeypatch.setattr(store, "record_sidecar", original)
    recovered = pipeline.run_unattached(
        pid, section="engine_runs", key="checkpoint", spec=spec
    )
    assert calls["n"] == 1
    assert store.read_artifact(pid, recovered["output_sha256"]) == b"nondeterministic A"
    final_state = store.get_production(pid)
    assert final_state["engine_runs"]["sibling"] == sibling
    final_terminal = next(
        attempt
        for attempt in final_state["attempts"]
        if attempt["attempt_id"] == terminal["attempt_id"]
    )
    assert final_terminal["state"] == "review_pending"
    assert final_terminal["outputs"] == terminal["outputs"]
    assert store.read_artifact(pid, source["sha256"]) == b"protected"


@pytest.mark.parametrize(
    "crash_boundary", ["before_cas", "after_cas_before_checkpoint"]
)
def test_unknown_external_execution_requires_explicit_retry_and_names_do_not_collide(
    tmp_path, monkeypatch, crash_boundary
):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    outputs = iter((b"first CAS bytes", b"different retry bytes"))
    calls = {"n": 0}

    def run(_pid, _spec, *, attempt_id):
        assert attempt_id is None
        calls["n"] += 1
        return [next(outputs), b"receipt"], {
            "outputs": [{"name": "audio.wav", "media_type": "audio/wav"}],
            "execution_mode": "real",
            "real_execution": True,
        }

    pipeline._run_adapter = run
    spec = {"engine": "tts", "profile": "speech", "version": "same"}
    if crash_boundary == "before_cas":
        original = store.put_artifact
        crashed = {"done": False}

        def crash_before_cas(production_id, name, content, *, media_type):
            if not crashed["done"]:
                crashed["done"] = True
                raise KeyboardInterrupt("crash before output CAS")
            return original(production_id, name, content, media_type=media_type)

        monkeypatch.setattr(store, "put_artifact", crash_before_cas)
        crash_message = "before output CAS"
    else:
        original = store.write_unattached_execution
        crashed = {"done": False}

        def crash_before_checkpoint(production_id, section, key, value):
            if value.get("status") == "completed" and not crashed["done"]:
                crashed["done"] = True
                raise KeyboardInterrupt("crash after output CAS before checkpoint")
            return original(production_id, section, key, value)

        monkeypatch.setattr(
            store, "write_unattached_execution", crash_before_checkpoint
        )
        crash_message = "after output CAS before checkpoint"

    with pytest.raises(KeyboardInterrupt, match=crash_message):
        pipeline.run_unattached(pid, section="engine_runs", key="unknown", spec=spec)

    monkeypatch.setattr(
        store,
        "put_artifact"
        if crash_boundary == "before_cas"
        else "write_unattached_execution",
        original,
    )
    with pytest.raises(PipelineError, match="완료 여부를 확인할 수 없습니다"):
        pipeline.run_unattached(pid, section="engine_runs", key="unknown", spec=spec)
    recovered = pipeline.run_unattached(
        pid,
        section="engine_runs",
        key="unknown",
        spec=spec,
        retry_unknown_execution=True,
    )
    assert calls["n"] == 2
    assert (
        store.read_artifact(pid, recovered["output_sha256"]) == b"different retry bytes"
    )
    names = list(store.get_production(pid)["artifacts"])
    assert len(names) == len(set(names))
    assert len(names) == (2 if crash_boundary == "before_cas" else 3)


def test_legacy_unattached_sidecar_is_not_silently_reused(tmp_path):
    from ttakkari.creative.director import ConflictError

    store, pid = _store(tmp_path)
    store.record_sidecar(
        pid,
        "engine_runs",
        "legacy",
        {"engine": "tts", "profile": "speech", "spec": {"legacy": True}},
    )
    pipeline = ProductionPipeline(store, registry=None)
    pipeline._run_adapter = _fake_adapter({"n": 0}, None)
    with pytest.raises(ConflictError, match="이전 실행 기록"):
        pipeline.run_unattached(
            pid,
            section="engine_runs",
            key="legacy",
            spec={"engine": "tts", "profile": "speech", "legacy": True},
        )


def test_final_api_requires_explicit_opt_in_to_retry_unknown_execution():
    from ttakkari.api.creative import FinalInput

    assert FinalInput().retry_unknown_execution is False
    assert FinalInput(retry_unknown_execution=True).retry_unknown_execution is True


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
