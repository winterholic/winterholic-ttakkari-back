from types import SimpleNamespace
from unittest.mock import AsyncMock


async def test_remote_api_keeps_request_qwen_but_skips_durable_consumers_including_tts(monkeypatch):
    from ttakkari import background_consumers, generation_service
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs.desk_qwen_service import service as qwen
    from ttakkari.tts import jobs as tts_jobs

    calls = []
    monkeypatch.setattr(preview, "start_worker", AsyncMock(side_effect=lambda: calls.append("preview")))
    monkeypatch.setattr(stt_jobs, "start", AsyncMock(side_effect=lambda: calls.append("stt")))
    monkeypatch.setattr(tts_jobs, "start", AsyncMock(side_effect=lambda: calls.append("tts")))
    monkeypatch.setattr(qwen, "start", AsyncMock(side_effect=lambda **kwargs: calls.append("qwen")))
    monkeypatch.setattr(background_consumers, "start", AsyncMock())

    await generation_service.start_api_services("remote")

    assert calls == ["qwen"]
    qwen.start.assert_awaited_once_with(owner=False)
    background_consumers.start.assert_not_awaited()


async def test_local_api_preserves_existing_worker_startup(monkeypatch):
    from ttakkari import background_consumers, generation_service
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs.desk_qwen_service import service as qwen
    from ttakkari.tts import jobs as tts_jobs

    calls = []
    for service, name in ((preview, "preview"), (stt_jobs, "stt"),
                          (tts_jobs, "tts"), (qwen, "qwen")):
        monkeypatch.setattr(service, "start_worker" if name == "preview" else "start",
                            AsyncMock(side_effect=lambda n=name, **kwargs: calls.append(n)))
    monkeypatch.setattr(background_consumers, "start", AsyncMock(side_effect=lambda: calls.append("background")))

    await generation_service.start_api_services("local")

    assert calls == ["preview", "stt", "tts", "background", "qwen"]
    qwen.start.assert_awaited_once_with(owner=True)


async def test_executor_starts_durable_consumers_and_maintenance(monkeypatch):
    from ttakkari import background_consumers, executor, generation_service
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.tts import jobs as tts_jobs

    calls = []
    monkeypatch.setattr(preview, "start_worker", AsyncMock(side_effect=lambda: calls.append("preview")))
    monkeypatch.setattr(stt_jobs, "start", AsyncMock(side_effect=lambda: calls.append("stt")))
    monkeypatch.setattr(tts_jobs, "start", AsyncMock(side_effect=lambda: calls.append("tts")))
    monkeypatch.setattr(background_consumers, "start", AsyncMock(side_effect=lambda: calls.append("background")))
    monkeypatch.setattr(generation_service, "maintenance_jobs", lambda _s: [("one", object(), 60)])
    def fake_create_task(coro, name=None):
        calls.append(name)
        coro.close()
        return object()
    monkeypatch.setattr(generation_service.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(executor, "get_settings", lambda: SimpleNamespace())

    tasks = await executor.start_background_services()

    assert calls == ["preview", "stt", "tts", "background", "periodic:one"]
    assert len(tasks) == 1
