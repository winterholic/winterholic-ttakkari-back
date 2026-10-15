import asyncio
import sys
from types import SimpleNamespace

import pytest

from ttakkari.context import embedder


async def test_e5_runs_only_on_request_in_serial_disposable_workers(
    monkeypatch, tmp_path
):
    calls, active = [], 0

    async def run(**kw):
        nonlocal active
        active += 1
        assert active == 1
        calls.append(kw)
        await asyncio.sleep(0.01)
        active -= 1
        return {"vectors": [[1.0] + [0.0] * 1023], "metrics": {"process_exited": True}}

    monkeypatch.setattr(embedder, "run_json_worker", run, raising=False)
    monkeypatch.setattr(
        embedder,
        "get_settings",
        lambda: SimpleNamespace(
            context_model="intfloat/multilingual-e5-large",
            models_dir=tmp_path,
            context_embedding_timeout_seconds=300,
            context_embedding_cpu_threads=2,
        ),
    )
    import inspect

    assert inspect.iscoroutinefunction(embedder.E5Embedder.embed), (
        "embedding must use cancellable async worker"
    )
    engine = embedder.E5Embedder()
    assert not calls
    assert "fastembed" not in sys.modules
    vectors = await asyncio.gather(
        engine.embed(["질문"], query=True), engine.embed(["본문"], query=False)
    )
    assert len(calls) == 2
    assert calls[0]["request"]["query"] is True
    assert calls[1]["request"]["query"] is False
    assert calls[0]["request"]["threads"] == 2
    assert len(vectors[0][0]) == 1024
    assert not hasattr(engine, "_model")


@pytest.mark.parametrize("vectors", [[], [[0.0]], [[float("nan")] * 1024]])
async def test_e5_rejects_incomplete_or_invalid_worker_output(monkeypatch, vectors):
    async def run(**kw):
        return {"vectors": vectors}

    monkeypatch.setattr(embedder, "run_json_worker", run, raising=False)
    import inspect

    assert inspect.iscoroutinefunction(embedder.E5Embedder.embed), (
        "embedding must use cancellable async worker"
    )
    with pytest.raises(ValueError, match="embedding"):
        await embedder.E5Embedder().embed(["본문"], query=False)


def test_embedding_worker_uses_offline_cache_prefixes_and_reports_measured_lifetime(
    monkeypatch, tmp_path
):
    from ttakkari.context import embedder_worker

    records = []

    class Model:
        def __init__(self, name, **options):
            records.append((name, options))

        def embed(self, texts, **options):
            records.append((texts, options))
            return [[3.0, 4.0]]

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=Model))
    r = embedder_worker.compute(
        {
            "model": "e5",
            "cache_dir": str(tmp_path),
            "threads": 2,
            "texts": ["문장"],
            "query": False,
        }
    )
    assert records == [
        ("e5", {"cache_dir": str(tmp_path), "threads": 2, "local_files_only": True}),
        (["passage: 문장"], {"batch_size": 16}),
    ]
    assert r["vectors"] == [[0.6, 0.8]]
    assert r["metrics"]["load_seconds"] >= 0
    assert r["metrics"]["inference_seconds"] >= 0
    assert r["metrics"]["peak_rss_bytes"] > 0
