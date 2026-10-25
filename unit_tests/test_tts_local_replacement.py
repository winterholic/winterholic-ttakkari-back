"""local→local 교체는 전송 완료 확인을 요구하지 않고, remote 가능성은 계속 거절한다. DB·프로세스·Caddy 없음."""
import uuid
from pathlib import Path

import pytest
from test_tts_first_transition_failure_recovery import Slots as BaseSlots

from ttakkari import generation_deployment as gd
from ttakkari.deployment import JobStore

REPO = Path(__file__).resolve().parents[1]  # 실제 계약(activation=disabled)을 가진 릴리스 루트


class Slots(BaseSlots):
    def __init__(self, candidate_mode="local"):
        super().__init__()
        self.candidate_id = uuid.uuid4()
        self.candidate_mode = candidate_mode
        self.transport_waits = 0

    async def wait_tts_transport_drained(self, slot):
        self.transport_waits += 1
        raise gd.StageError("tts-drain", None, None, "TTS transport completion unavailable")

    async def snapshot(self, slot):
        if slot == 1:
            return (None if self.candidate_mode is None else
                    {"generation": str(self.candidate_id), "execution_mode": self.candidate_mode})
        return {"generation": str(self.old_id), "execution_mode": "local", "http_inflight": 0}

    def job(self, **extra):
        return {"previous_slot": 0, "previous_generation": str(self.old_id), "candidate_slot": 1,
                "candidate_generation": str(self.candidate_id), "release_path": str(REPO), **extra}


def controller(tmp_path, slots):
    control = gd.GenerationController(JobStore(tmp_path), slots=slots)

    async def idle(_):
        return 0

    control._previous_tts_queued = idle
    return control


async def test_local_to_local_replacement_needs_no_transport_ack(tmp_path):
    slots = Slots("local")
    await controller(tmp_path, slots).drain_previous_tts(slots.job(), poll=0)
    assert slots.transport_waits == 0
    assert slots.closed is True  # 2fde901 이전과 같이 접수를 닫고 큐를 확인한다. 성공 시 cutover 의 switch 가 다시 연다.


async def test_local_replacement_still_refuses_when_previous_is_busy(tmp_path):
    slots = Slots("local")
    control = controller(tmp_path, slots)

    async def busy(_):
        return 1

    control._previous_tts_queued = busy
    with pytest.raises(gd.StageError, match="not idle"):
        await control.drain_previous_tts(slots.job(), timeout=0.05, poll=0.01)
    assert slots.restored == 1 and slots.closed is False


async def test_first_local_to_remote_transition_is_still_refused_and_route_restored(tmp_path):
    slots = Slots("remote")
    with pytest.raises(gd.StageError, match="transport completion"):
        await controller(tmp_path, slots).drain_previous_tts(slots.job(), poll=0)
    assert slots.transport_waits == 1
    assert slots.closed is False and slots.restored == 1


async def test_stock_caddy_guard_is_unchanged(tmp_path):
    with pytest.raises(gd.StageError, match="transport completion"):
        await gd.Slots(tmp_path).wait_tts_transport_drained(0)


@pytest.mark.parametrize("name", ["missing-mode", "unreachable", "other-generation", "unknown-mode",
                                  "no-candidate-record", "no-release", "unreadable-contract",
                                  "activation-enabled"])
async def test_unprovable_candidate_mode_is_treated_as_remote(tmp_path, monkeypatch, name):
    slots, job = Slots("local"), None
    job = slots.job()
    if name == "missing-mode":
        base = slots.snapshot

        async def snap(slot):
            row = await base(slot)
            return {k: v for k, v in row.items() if k != "execution_mode"} if slot == 1 else row

        slots.snapshot = snap
    elif name == "unreachable":
        slots.candidate_mode = None
    elif name == "other-generation":
        job["candidate_generation"] = str(uuid.uuid4())
    elif name == "unknown-mode":
        slots.candidate_mode = "LOCAL "
    elif name == "no-candidate-record":
        del job["candidate_generation"]
    elif name == "no-release":
        del job["release_path"]
    elif name == "unreadable-contract":
        job["release_path"] = str(tmp_path / "missing-release")
    else:
        monkeypatch.setattr(gd.Releases, "contract",
                            staticmethod(lambda _p: {"run_executor": {"activation": "enabled"}}))
    with pytest.raises(gd.StageError, match="transport completion"):
        await controller(tmp_path, slots).drain_previous_tts(job, poll=0)
    assert slots.transport_waits == 1 and slots.closed is False and slots.restored == 1


async def test_remote_previous_never_takes_the_local_replacement_path(tmp_path):
    slots = Slots("local")
    base = slots.snapshot

    async def snap(slot):
        row = await base(slot)
        return {**row, "execution_mode": "remote", "executor_protocol": 1} if slot == 0 else row

    slots.snapshot = snap
    with pytest.raises(gd.StageError, match="transport completion"):
        await controller(tmp_path, slots).drain_previous_tts(slots.job(), poll=0)
    assert slots.transport_waits == 1
