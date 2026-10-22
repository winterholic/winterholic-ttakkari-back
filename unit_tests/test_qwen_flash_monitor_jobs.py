import pytest

from ttakkari import generation_service
from ttakkari.config import get_settings
from ttakkari.runs import qwen_flash_monitor


@pytest.mark.asyncio
async def test_flash_counter_collection_is_a_periodic_job_and_can_be_disabled(monkeypatch):
    entered = []

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(qwen_flash_monitor, "sessionmaker", lambda: SessionContext)

    async def snapshot_with_history(db):
        entered.append(db)
        return {"today": {"total_tokens": 9}}

    monkeypatch.setattr(qwen_flash_monitor, "snapshot_with_history", snapshot_with_history)
    settings = get_settings().model_copy(update={"qwen_flash_poll_seconds": 60})
    jobs = {name: (callback, interval) for name, callback, interval in generation_service.maintenance_jobs(settings)}

    callback, interval = jobs["qwen-flash-usage"]
    assert interval == 60
    await callback()
    assert len(entered) == 1

    disabled = {name for name, _, _ in generation_service.maintenance_jobs(
        settings.model_copy(update={"qwen_flash_poll_seconds": 0}))}
    assert "qwen-flash-usage" not in disabled
