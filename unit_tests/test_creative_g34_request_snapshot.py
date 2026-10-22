"""G34 재검수 P2 두 건의 회귀 시험.

P2-1: `run_unattached`의 요청 identity, adapter 입력, sidecar·checkpoint는 호출 시점의 같은 요청 snapshot을 뜻해야 한다.
P2-2: 공개 `/direct`도 unknown 실행의 명시적 재시도 opt-in을 받아 `generate_speech`까지 전달한다.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading

import httpx
import pytest
from creative_support import (
    FakeTtsProvider,
    make_pipeline,
    new_production,
    real_ffmpeg_pipeline,
)
from fastapi import FastAPI

from ttakkari.api import creative as api
from ttakkari.creative.director import DirectorStore
from ttakkari.creative.engines.base import EngineError
from ttakkari.creative.engines.mock import MockEngineAdapter
from ttakkari.creative.pipeline import (
    PipelineError,
    ProductionPipeline,
    canonical,
    sha256_hex,
)
from ttakkari.security.auth import require_user

RECEIPT = {
    "outputs": [{"name": "audio.wav", "media_type": "audio/wav"}],
    "execution_mode": "real",
    "real_execution": True,
}


def _store(tmp_path):
    store = DirectorStore(tmp_path)
    pid = store.create_production("snapshot")["manifest"]["production_id"]
    return store, pid


def _spec():
    return {
        "engine": "tts",
        "profile": "speech",
        "params": {"lines": ["original"]},
        "input_assets": [{"asset_id": "line", "sha256": "a" * 64}],
    }


def _identity(key, spec, extra):
    return sha256_hex(
        canonical({"section": "engine_runs", "key": key, "spec": spec, "extra": extra})
    )


# ---------- P2-1: caller/adapter alias 분리


def test_adapter_receives_a_private_copy_and_the_sidecar_keeps_the_original_request(
    tmp_path,
):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    spec, extra = _spec(), {"voice": "A", "nested": {"tags": ["x"]}}
    original_spec, original_extra = copy.deepcopy(spec), copy.deepcopy(extra)
    seen: dict = {}

    def run(_pid, adapter_spec, *, attempt_id):
        seen["spec"] = adapter_spec
        assert adapter_spec == original_spec
        # 호출자 쪽 nested alias를 실행 도중 바꿔도 이미 정한 요청에는 영향이 없어야 한다.
        spec["params"]["lines"][0] = "caller changed"
        spec["input_assets"][0]["sha256"] = "c" * 64
        extra["voice"] = "caller changed"
        extra["nested"]["tags"].append("late")
        return [b"result", b"receipt"], RECEIPT

    pipeline._run_adapter = run
    record = pipeline.run_unattached(
        pid, section="engine_runs", key="k", spec=spec, extra=extra
    )

    assert seen["spec"] is not spec
    assert seen["spec"]["params"]["lines"] is not spec["params"]["lines"]
    assert record["spec"] == original_spec
    assert record["voice"] == "A" and record["nested"] == {"tags": ["x"]}
    assert record["request_identity"] == _identity("k", original_spec, original_extra)
    stored = store.get_production(pid)["engine_runs"]["k"]
    assert stored["spec"] == original_spec and stored["voice"] == "A"
    # 호출자가 반환 뒤에 다시 바꿔도 저장된 기록·반환값은 그대로다.
    spec["params"]["lines"][0] = "after return"
    extra["nested"]["tags"].append("after")
    assert record["spec"] == original_spec
    assert store.get_production(pid)["engine_runs"]["k"]["spec"] == original_spec
    again = pipeline.run_unattached(
        pid,
        section="engine_runs",
        key="k",
        spec=copy.deepcopy(original_spec),
        extra=copy.deepcopy(original_extra),
    )
    assert again["content_hash"] == record["content_hash"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["params"]["lines"].__setitem__(0, "mutated in adapter"),
        lambda s: s["input_assets"][0].__setitem__("sha256", "b" * 64),
        lambda s: s["params"].__setitem__("injected", True),
        lambda s: s.pop("profile"),
    ],
    ids=["nested-list", "input-asset-sha", "new-param", "top-level-key"],
)
def test_adapter_that_mutates_its_input_fails_closed_and_publishes_nothing(
    tmp_path, mutate
):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    calls = {"n": 0}

    def run(_pid, adapter_spec, *, attempt_id):
        calls["n"] += 1
        mutate(adapter_spec)
        return [b"result for B", b"receipt"], RECEIPT

    pipeline._run_adapter = run
    spec, extra = _spec(), {"voice": "A"}
    with pytest.raises(PipelineError, match="입력.*변경"):
        pipeline.run_unattached(
            pid, section="engine_runs", key="k", spec=spec, extra=extra
        )

    state = store.get_production(pid)
    assert "k" not in state.get("engine_runs", {})
    assert not state["artifacts"]
    checkpoint = store.get_unattached_execution(pid, "engine_runs", "k")
    assert checkpoint["status"] == "executing"
    assert checkpoint["request_identity"] == _identity("k", _spec(), extra)
    assert spec == _spec()  # 호출자 객체도 변하지 않았다

    # 원 요청 A를 다시 불러도 변조 실행의 결과를 조용히 재사용하지 않는다.
    with pytest.raises(PipelineError, match="완료 여부를 확인할 수 없습니다"):
        pipeline.run_unattached(
            pid, section="engine_runs", key="k", spec=_spec(), extra={"voice": "A"}
        )
    assert calls["n"] == 1


def test_adapter_mutation_cannot_change_a_later_explicit_retry_of_the_original_request(
    tmp_path,
):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    calls = {"n": 0}

    def run(_pid, adapter_spec, *, attempt_id):
        calls["n"] += 1
        if calls["n"] == 1:
            adapter_spec["params"]["lines"][0] = "mutated in adapter"
            return [b"result for B", b"receipt"], RECEIPT
        assert adapter_spec == _spec()  # 재시도도 원 요청 A를 받는다
        return [b"result for A", b"receipt"], RECEIPT

    pipeline._run_adapter = run
    with pytest.raises(PipelineError, match="입력.*변경"):
        pipeline.run_unattached(pid, section="engine_runs", key="k", spec=_spec())
    record = pipeline.run_unattached(
        pid,
        section="engine_runs",
        key="k",
        spec=_spec(),
        retry_unknown_execution=True,
    )
    assert store.read_artifact(pid, record["output_sha256"]) == b"result for A"
    assert record["spec"] == _spec()
    assert record["request_identity"] == _identity("k", _spec(), {})


def test_caller_mutation_while_the_request_is_being_snapshotted_cannot_split_identity(
    tmp_path,
):
    store, pid = _store(tmp_path)
    pipeline = ProductionPipeline(store, registry=None)
    entered, release = threading.Event(), threading.Event()
    spec = _spec()

    def run(_pid, adapter_spec, *, attempt_id):
        entered.set()
        release.wait(5)
        return [b"result", b"receipt"], RECEIPT

    pipeline._run_adapter = run
    results: list = []
    worker = threading.Thread(
        target=lambda: results.append(
            pipeline.run_unattached(
                pid, section="engine_runs", key="k", spec=spec, extra={"voice": "A"}
            )
        )
    )
    worker.start()
    assert entered.wait(5)
    spec["params"]["lines"][0] = "other thread changed"
    release.set()
    worker.join(5)

    record = results[0]
    assert record["spec"] == _spec()
    assert record["request_identity"] == _identity("k", _spec(), {"voice": "A"})


class _MutatingMock(MockEngineAdapter):
    """내장 계약을 따르는 adapter가 ctx.spec을 바꾸면 receipt/결과를 발행하지 않아야 한다."""

    def execute(self, ctx):
        ctx.spec["params"]["label"] = "mutated"
        return super().execute(ctx)


def _mock_spec(pid, timeline):
    return {
        "engine": "mock",
        "profile": "clip",
        "shot_id": "x",
        "params": {"label": "orig"},
        "seed": 1,
        "range": {"start_f": 0, "end_f": 24, "frames": 24},
        "production_id": pid,
        "timeline_revision": timeline["revision"],
    }


def test_real_adapter_path_rejects_ctx_spec_mutation_before_building_a_receipt(
    tmp_path,
):
    pipeline, store, _registry = make_pipeline(tmp_path, _MutatingMock())
    pid, timeline = new_production(store, shots=1)
    spec = _mock_spec(pid, timeline)
    snapshot = copy.deepcopy(spec)

    with pytest.raises(EngineError, match="입력.*변경") as caught:
        pipeline.run_unattached(pid, section="engine_runs", key="mut", spec=spec)

    assert caught.value.kind == "input_mutated"
    assert spec == snapshot
    state = store.get_production(pid)
    assert "mut" not in state.get("engine_runs", {})
    assert not any(name.startswith("unattached-") for name in state["artifacts"])


def test_receipt_spec_hash_is_the_hash_of_the_requested_spec(tmp_path):
    pipeline, store, _registry = make_pipeline(tmp_path, MockEngineAdapter())
    pid, timeline = new_production(store, shots=1)
    spec = _mock_spec(pid, timeline)
    record = pipeline.run_unattached(pid, section="engine_runs", key="ok", spec=spec)
    receipt = json.loads(store.read_artifact(pid, record["receipt_sha256"]))
    assert receipt["spec_sha256"] == hashlib.sha256(canonical(spec)).hexdigest()
    assert record["spec"] == spec


# ---------- P2-2: /direct → direct → make_timeline → generate_speech


def test_direct_input_accepts_the_explicit_retry_flag_with_a_false_default():
    from ttakkari.api.creative import DirectInput

    assert DirectInput().retry_unknown_execution is False
    assert DirectInput(retry_unknown_execution=True).retry_unknown_execution is True
    with pytest.raises(ValueError):
        DirectInput(retry_unknown_execution="maybe")
    with pytest.raises(ValueError):
        DirectInput(retry_unknown="x")


class _RecordingDirector:
    def __init__(self):
        self.calls: list[tuple] = []

    async def direct(self, pid, brief, **kwargs):
        self.calls.append((pid, brief, kwargs))
        return {}


@pytest.fixture
def direct_env(tmp_path, monkeypatch):
    pipeline, store, _registry = make_pipeline(tmp_path, MockEngineAdapter())
    director = _RecordingDirector()
    monkeypatch.setattr(api, "default_pipeline", lambda: pipeline)
    monkeypatch.setattr(api, "default_director", lambda: director)
    monkeypatch.setattr(api, "default_director_store", lambda: store)
    app = FastAPI()
    app.include_router(api.router)
    pid = store.create_production("direct")["manifest"]["production_id"]
    return app, store, director, pid


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _settle():
    while api._tasks:
        await asyncio.gather(*list(api._tasks))


async def test_direct_route_authentication_unknown_field_and_flag_forwarding(
    direct_env,
):
    app, _unused_store, director, pid = direct_env
    url = f"/api/creative/productions/{pid}/direct"
    body = {"speech_lines": ["안녕"], "retry_unknown_execution": True}
    async with _client(app) as client:
        # 인증 없음: 의존성 override가 없으면 401이고 실행 예약도 없다.
        assert (await client.post(url, json=body)).status_code == 401
        assert director.calls == []

        app.dependency_overrides[require_user] = lambda: "owner"
        bad = await client.post(url, json={**body, "unexpected": 1})
        assert bad.status_code == 422
        wrong_type = await client.post(url, json={"retry_unknown_execution": "maybe"})
        assert wrong_type.status_code == 422
        assert director.calls == []

        ok = await client.post(url, json=body)
        assert ok.status_code == 202 and ok.json()["status"] == "started"
        await _settle()
        default = await client.post(url, json={"speech_lines": ["안녕"]})
        assert default.status_code == 202
        await _settle()

    assert director.calls[0][2]["retry_unknown_execution"] is True
    assert director.calls[1][2].get("retry_unknown_execution", False) is False


BIBLE = {
    "palette": ["#101418", "#f4f4ed", "#00f0b5"],
    "typography": {"font_family": "sans-serif"},
    "composition": {"size": [320, 180], "aspect": "16:9"},
    "camera": {"language": ["정적"], "default_move": "orbit"},
    "lut": {"name": "none", "intent": "중립"},
    "negative_rules": [],
}
TIMELINE = {
    "sections": [],
    "shots": [
        {
            "shot_id": "shot-1",
            "start_f": 0,
            "end_f": 14,
            "cut": "speech",
            "reason": "전체",
        }
    ],
}
SHOTS = {
    "shots": [
        {
            "shot_id": "shot-1",
            "intent": "낭독",
            "engine": "ffmpeg",
            "profile": "test-pattern",
            "params": {},
            "seed": 3,
            "reason": "모션",
        }
    ]
}


class _CountingTts(FakeTtsProvider):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.synthesized = 0

    async def synthesize(self, chunks, **kwargs):
        self.synthesized += 1
        return await super().synthesize(chunks, **kwargs)


def _speech_director(tmp_path):
    from ttakkari.creative.direction import CreativeDirector
    from ttakkari.creative.director_llm import FixedResponseLlm
    from ttakkari.creative.engines.ffmpeg_engine import FfmpegAdapter
    from ttakkari.creative.engines.registry import EngineRegistry
    from ttakkari.creative.engines.tts_engine import TtsAdapter

    pipeline, store, _registry = real_ffmpeg_pipeline(tmp_path)
    provider = _CountingTts(tmp_path)
    registry = EngineRegistry(
        tmp_path / "r2.json",
        [FfmpegAdapter(), TtsAdapter(provider=provider, mock=True)],
        allow_mock_selection=False,
    )
    registry.run_smoke("ffmpeg")
    registry.set_policy("ffmpeg", "auto_eligible", "시험")
    pipeline = ProductionPipeline(store, registry)
    pid = store.create_production("낭독 영상")["manifest"]["production_id"]
    llm = FixedResponseLlm(
        {
            "bible": json.dumps(BIBLE),
            "timeline": json.dumps(TIMELINE),
            "shots": json.dumps(SHOTS),
        }
    )
    return CreativeDirector(pipeline, llm), pipeline, store, pid, provider


def _crash_after_external_speech(store, monkeypatch):
    original = store.write_unattached_execution

    def crash_on_completion(production_id, section, key, value):
        if value.get("status") == "completed":
            raise KeyboardInterrupt("crash after external speech run")
        return original(production_id, section, key, value)

    monkeypatch.setattr(store, "write_unattached_execution", crash_on_completion)
    return lambda: monkeypatch.setattr(store, "write_unattached_execution", original)


LINES = ["하나", "둘"]


async def test_make_timeline_recovers_unknown_speech_only_with_the_explicit_opt_in(
    tmp_path, monkeypatch
):
    director, _pipeline, store, pid, provider = _speech_director(tmp_path)
    await director.make_bible(pid, "b", size=(320, 180))
    restore = _crash_after_external_speech(store, monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        await director.make_timeline(pid, "b", speech_lines=LINES)
    restore()
    assert provider.synthesized == 1

    with pytest.raises(PipelineError, match="완료 여부를 확인할 수 없습니다"):
        await director.make_timeline(pid, "b", speech_lines=LINES)
    assert provider.synthesized == 1

    keys_before = set(store.get_production(pid)["unattached_executions"])
    timeline = await director.make_timeline(
        pid, "b", speech_lines=LINES, retry_unknown_execution=True
    )
    assert provider.synthesized == 2 and timeline["duration_frames"] == 14
    state = store.get_production(pid)
    # 새 key 우회가 아니라 같은 요청 checkpoint가 두 번째 시도로 완료된다.
    assert set(state["unattached_executions"]) == keys_before
    (execution,) = state["unattached_executions"].values()
    assert execution["status"] == "completed" and execution["execution_attempts"] == 2


async def test_direct_route_flag_recovers_unknown_tts_through_the_background_task(
    tmp_path, monkeypatch
):
    director, pipeline, store, pid, provider = _speech_director(tmp_path)
    await director.make_bible(pid, "b", size=(320, 180))
    restore = _crash_after_external_speech(store, monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        await director.make_timeline(pid, "b", speech_lines=LINES)
    restore()
    assert provider.synthesized == 1

    monkeypatch.setattr(api, "default_pipeline", lambda: pipeline)
    monkeypatch.setattr(api, "default_director", lambda: director)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    url = f"/api/creative/productions/{pid}/direct"
    body = {"brief": "b", "speech_lines": LINES, "size": [320, 180]}
    async with _client(app) as client:
        # 기본값(flag 없음)은 unknown 실행을 다시 돌리지 않고 실패를 제작 기록에 남긴다.
        assert (await client.post(url, json=body)).status_code == 202
        await _settle()
        assert provider.synthesized == 1
        failed = [
            d
            for d in store.get_production(pid).get("direction", {}).values()
            if d.get("status") == "failed"
        ]
        assert failed and "완료 여부를 확인할 수 없습니다" in failed[0]["error"]
        assert store.get_production(pid)["timelines"][-1]["revision"] == 1

        assert (
            await client.post(url, json={**body, "retry_unknown_execution": True})
        ).status_code == 202
        await _settle()

    assert provider.synthesized == 2
    state = store.get_production(pid)
    assert state["timelines"][-1]["revision"] == 2
    assert state["shots"]["shot-1"][-1]["engine"] == "ffmpeg"
    (execution,) = state["unattached_executions"].values()
    assert execution["status"] == "completed" and execution["execution_attempts"] == 2


async def test_direct_route_forwards_the_flag_to_generate_speech(tmp_path, monkeypatch):
    director, pipeline, _unused_store, pid, _provider = _speech_director(tmp_path)
    monkeypatch.setattr(api, "default_pipeline", lambda: pipeline)
    monkeypatch.setattr(api, "default_director", lambda: director)
    seen: list[dict] = []

    def fake_generate_speech(production_id, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop after observing the forwarded arguments")

    monkeypatch.setattr(pipeline, "generate_speech", fake_generate_speech)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    url = f"/api/creative/productions/{pid}/direct"
    async with _client(app) as client:
        for flag in (True, False, None):
            body = {"brief": "b", "speech_lines": LINES, "size": [320, 180]}
            if flag is not None:
                body["retry_unknown_execution"] = flag
            assert (await client.post(url, json=body)).status_code == 202
            await _settle()

    assert [call["retry_unknown_execution"] for call in seen] == [True, False, False]
