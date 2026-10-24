"""TTS transition error recovery; no live DB, processes, Caddy, or model."""
import asyncio
import uuid

import pytest

from ttakkari import generation_deployment as deployment
from ttakkari.deployment import JobStore


class Slots:
    def __init__(self):
        self.old_id = uuid.uuid4()
        self.closed = False
        self.restored = 0

    async def close_tts_ingress(self, slot):
        self.closed = True

    async def wait_tts_transport_drained(self, slot):
        # This DB-free fixture has no proxy requests. It models an explicit ack;
        # it does not establish that stock Caddy can supply one.
        return True

    async def switch(self, slot):
        self.restored += 1
        self.closed = False

    async def snapshot(self, slot):
        return {"generation": str(self.old_id), "http_inflight": 0}

    def job(self):
        return {"previous_slot": 0, "previous_generation": str(self.old_id)}


@pytest.mark.parametrize("error", [OSError("persist failed"), asyncio.CancelledError()])
async def test_partial_close_error_or_cancellation_restores_ingress(tmp_path, error):
    slots = Slots()

    async def partial_close(_slot):
        slots.closed = True
        raise error

    slots.close_tts_ingress = partial_close
    controller = deployment.GenerationController(JobStore(tmp_path), slots=slots)
    with pytest.raises(type(error)) as raised:
        await controller.drain_previous_tts(slots.job(), poll=0)
    assert raised.value is error
    assert slots.restored == 1
    assert slots.closed is False


async def test_restore_error_is_logged_without_masking_close_error(tmp_path, caplog):
    slots = Slots()
    original = OSError("persist failed")

    async def partial_close(_slot):
        slots.closed = True
        raise original

    async def failed_restore(_slot):
        raise RuntimeError("Caddy reload failed")

    slots.close_tts_ingress = partial_close
    slots.switch = failed_restore
    controller = deployment.GenerationController(JobStore(tmp_path), slots=slots)
    with pytest.raises(OSError) as raised:
        await controller.drain_previous_tts(slots.job(), poll=0)
    assert raised.value is original
    assert "TTS ingress restoration failed" in caplog.text


async def test_running_legacy_tts_job_refuses_activation(tmp_path, monkeypatch):
    slots = Slots()

    class Database:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def scalar(self, statement):
            # Represent a previous-owner running row; run the real count builder.
            sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
            assert str(slots.old_id).replace("-", "") in sql.replace("-", "")
            return int("'running'" in sql)

    monkeypatch.setattr(deployment, "sessionmaker", lambda: Database)
    controller = deployment.GenerationController(JobStore(tmp_path), slots=slots)
    with pytest.raises(deployment.StageError, match="queued_tts=1"):
        await controller.drain_previous_tts(slots.job(), timeout=0, poll=0)
    assert slots.restored == 1
    assert slots.closed is False
