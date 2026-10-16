from typing import get_args

import pytest
from pydantic import ValidationError

from ttakkari.api.accounts import AccountIn
from ttakkari.api.orchestrator import WorkerIn
from ttakkari.runs.manager import _looks_like_agent
from ttakkari.runs.profiles import model_engine
from ttakkari.schemas import EngineName, SessionEngine


def test_antigravity_is_an_explicit_engine_and_model_family():
    assert model_engine("gemini-3.8-flash-high") == "antigravity"
    assert model_engine("claude-sonnet-5-5-high") == "antigravity"
    assert model_engine("sonnet") == "claude"
    assert model_engine("gpt-oss-120b-medium") == "antigravity"
    assert "antigravity" in get_args(EngineName)
    assert "antigravity" in get_args(SessionEngine)


def test_account_uses_the_logged_in_single_cli_without_account_folder():
    account = AccountIn(name="ag", engine="antigravity")
    assert account.config_dir is None
    with pytest.raises(ValidationError):
        AccountIn(name="ag", engine="antigravity", config_dir="/tmp/other-profile")


def test_worker_accepts_antigravity_gemini_and_rejects_unknown_model():
    worker = WorkerIn(title="design assist", prompt="implement", engine="antigravity",
                      model="gemini-3.8-flash-high", effort="high")
    assert worker.engine == "antigravity"
    with pytest.raises(ValidationError):
        WorkerIn(title="wrong model", prompt="x", engine="antigravity", model="not-a-cli-model")


def test_antigravity_effort_is_limited_to_supported_levels():
    WorkerIn(title="design", prompt="x", engine="antigravity", model="gemini-3.8-flash-high", effort="high")
    with pytest.raises(ValidationError):
        WorkerIn(title="design", prompt="x", engine="antigravity", model="gemini-3.8-flash-high", effort="xhigh")


def test_orphan_process_detection_recognizes_official_agy_binary(monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        args=a[0], returncode=0, stdout="/opt/homebrew/bin/agy -p prompt", stderr=""))
    assert _looks_like_agent(9876)
