import asyncio
from types import SimpleNamespace

import pytest

from ttakkari import kb_retrieval
from ttakkari.models import EMBED_DIM


@pytest.mark.asyncio
async def test_embedding_workers_never_run_concurrently(monkeypatch, tmp_path):
    running = peak = 0

    async def fake_worker(**kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {"coverage": "full", "vectors": [[0.0] * EMBED_DIM for _ in kwargs["request"]["texts"]]}

    settings = SimpleNamespace(context_embedder="e5", context_model="m", models_dir="/tmp",
                               context_embedding_cpu_threads=2, kb_embedding_snapshot="s",
                               context_embedding_timeout_seconds=5, home=tmp_path)
    monkeypatch.setattr(kb_retrieval, "get_settings", lambda: settings)
    monkeypatch.setattr(kb_retrieval.embedder, "enabled", lambda: True)
    monkeypatch.setattr(kb_retrieval, "run_json_worker", fake_worker)

    await asyncio.gather(*(kb_retrieval.embed_complete([f"t{i}"]) for i in range(6)))
    assert peak == 1


@pytest.mark.asyncio
async def test_embedding_gate_blocks_other_process_holder(monkeypatch, tmp_path):
    settings = SimpleNamespace(home=tmp_path)
    monkeypatch.setattr(kb_retrieval, "get_settings", lambda: settings)
    monkeypatch.setattr(kb_retrieval, "_GATE_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(kb_retrieval, "_GATE_POLL_SECONDS", 0.05)

    held = kb_retrieval._gate_try_lock(tmp_path / "run" / kb_retrieval._GATE_NAME)
    assert held is not None
    with pytest.raises(RuntimeError, match="kb_embedding_gate_busy"):
        await kb_retrieval._gate_acquire()
    kb_retrieval._gate_release(held)
    kb_retrieval._gate_release(await kb_retrieval._gate_acquire())
