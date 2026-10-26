"""Internal launch UUID handoff before subprocess creation; no model/server execution."""
import json
from pathlib import Path

import pytest
import test_host_gpu

from ttakkari import host_gpu
from ttakkari.host_gpu import StartRequest

# Reuse only the temporary files/settings fixture; these tests never launch its server.
env = test_host_gpu.env


def test_start_uses_precommitted_launch_id_before_spawn(env, monkeypatch):
    import uuid

    launch_id = uuid.uuid4().hex
    observed = []
    monkeypatch.setattr(env.gpu, "_current", lambda: None)
    monkeypatch.setattr(host_gpu, "_lock_held", lambda path: False)

    def stop_before_spawn(argv, **kwargs):
        spec = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        observed.append(spec["launch_id"])
        raise RuntimeError("synthetic spawn refusal")

    monkeypatch.setattr(host_gpu.subprocess, "Popen", stop_before_spawn)
    with pytest.raises(RuntimeError, match="synthetic spawn refusal"):
        env.gpu.start(StartRequest(model="tiny"), launch_id=launch_id)
    assert observed == [launch_id]


@pytest.mark.parametrize("launch_id", ["../untrusted", "", True, 1])
def test_start_rejects_invalid_internal_launch_identity_before_io(env, monkeypatch, launch_id):
    def forbidden(req):
        pytest.fail("invalid launch identity reached filesystem validation")
    monkeypatch.setattr(env.gpu, "_paths", forbidden)
    with pytest.raises((ValueError, TypeError)):
        env.gpu.start(StartRequest(model="tiny"), launch_id=launch_id)
