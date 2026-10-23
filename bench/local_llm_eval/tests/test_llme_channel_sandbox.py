"""Trusted transport fault injection and isolation contracts; no candidate attack payloads."""

from pathlib import Path

import pytest
from local_llm_eval.runner import run_check


def test_stdout_is_never_an_observation(tmp_path):
    from local_llm_eval.tests.transport_driver import exercise_channel

    assert exercise_channel("stdout", tmp_path).status == "stub_exit0"
    run = run_check(tmp_path, "stdout-noise", "print('OBS\\t{}')\nrec('v', 3)")
    assert run.status == "OBS" and run.obs == {"v": 3}


def test_binary_frame_contract():
    from local_llm_eval.channel import ChannelError, decode_frame, encode_frame

    valid = encode_frame({"v": 3})
    assert decode_frame(valid) == {"v": 3}
    for bad in [b"", valid + valid, valid[:-1], b"wrong", valid + b"x"]:
        with pytest.raises(ChannelError):
            decode_frame(bad)


@pytest.mark.parametrize(
    "fault", ["close", "replace", "duplicate", "missing", "malformed", "nonzero"]
)
def test_trusted_driver_channel_faults_fail(tmp_path, fault):
    from local_llm_eval.tests.transport_driver import exercise_channel

    result = exercise_channel(fault, tmp_path)
    assert result.status != "OBS", result


def test_holdout_never_falls_back_without_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE", "measured test environment unavailable"
    )
    run = run_check(tmp_path, "holdout", "rec('v', 3)", mode="holdout")
    assert run.status == "isolation_required"
    assert "sandbox_unavailable" in run.flags
    assert not run.facts.get("launched", False)


def test_development_records_unavailable_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE", "measured test environment unavailable"
    )
    run = run_check(tmp_path, "dev", "rec('v', 3)")
    assert run.status == "OBS" and "sandbox_unavailable" in run.flags
    assert run.facts["isolation"]["reason"] == "measured test environment unavailable"


def test_runtime_does_not_admit_the_venv_checkout():
    from local_llm_eval.sandbox import profile_text, runtime_paths

    runtime = runtime_paths()
    assert ".venv" not in str(runtime.executable)
    profile = profile_text(runtime, [Path("/private/tmp/candidate")])
    assert "(deny default)" in profile and "(deny network*)" in profile
    assert "(deny process-fork)" in profile
    assert "winterholic-ttakkari/back" not in profile
    assert '(subpath "/Users/winterholic")' not in profile
    # macOS 27 aborts every process whose profile cannot data-read the root
    # directory (readiness exit -6 in 4.2 review). Only that one literal, never a subtree.
    assert '(allow file-read-data (literal "/"))' in profile
    assert '(subpath "/")' not in profile


def test_sandbox_deny_paths_and_positive_stdlib(tmp_path):
    from local_llm_eval.sandbox import availability, run_deny_checks

    status = availability()
    if not status.available:
        # A denied sandbox_apply (engine sandbox) or an operator-recorded denial is
        # an environment fact. Any other unavailability is a defect, never a skip.
        assert not status.defect, "isolation defect: " + status.reason
        pytest.skip("OS deny unverified in this engine: " + status.reason)
    results = run_deny_checks(tmp_path)
    assert results["positive"] == "ok"
    assert all(value == "PermissionError" for value in results["reads"].values()), (
        results
    )
    for key in (
        "dev_fd_path",
        "network",
        "fork",
        "outside_write",
        "home_listing",
        "tmp_listing",
    ):
        assert results[key] == "PermissionError", (key, results)


def test_unexpected_readiness_failure_is_a_defect_not_an_environment_denial(
    monkeypatch,
):
    from local_llm_eval import sandbox

    monkeypatch.delenv("LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE", raising=False)
    sandbox._probe.cache_clear()
    try:
        monkeypatch.setattr(
            sandbox,
            "profile_text",
            lambda runtime, writable: "(version 1)\n(deny default)",
        )
        status = sandbox.availability()
        if not sandbox.SANDBOX.is_file():
            pytest.skip("sandbox-exec absent")
        # Either sandbox_apply is denied here (environment) or the profile is broken (defect).
        assert not status.available
        assert status.defect == ("sandbox_apply" not in status.reason)
    finally:
        sandbox._probe.cache_clear()


def test_operator_recorded_denial_is_not_a_defect(monkeypatch):
    from local_llm_eval.sandbox import availability

    monkeypatch.setenv("LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE", "measured denial")
    status = availability()
    assert not status.available and not status.defect


def test_holdout_native_checks_never_run_without_isolation(tmp_path, monkeypatch):
    from local_llm_eval.evaluator import evaluate_attempt
    from local_llm_eval.model import Attempt
    from local_llm_eval.tasks import REGISTRY

    monkeypatch.setenv("LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE", "measured unavailable")
    task = REGISTRY["B4-TST-D"]()
    task.split = "holdout"
    result = evaluate_attempt(task, Attempt("test", task.id, "cpu", 0))
    assert result.verdict == "INFRA" and not result.evaluated
    assert result.fail_code == "INFRA_ISOLATION_UNAVAILABLE" and result.stages == []


def test_deny_targets_are_real_files_including_original_checkout():
    from local_llm_eval.sandbox import protected_sources

    targets = protected_sources()
    assert all(p.is_file() for p in targets.values())
    assert targets["original_checkout"].name == "pyproject.toml"


def test_ambiguous_dunder_is_a_flag_not_a_rejection(tmp_path):
    (tmp_path / "normal.py").write_text(
        "class Resource:\n    def __del__(self):\n        pass\nvalue = 3\n"
    )
    result = run_check(tmp_path, "signal", "import normal\nrec('v', normal.value)")
    assert result.status == "OBS" and result.obs == {"v": 3}
    assert "custom_runtime_hook_signal" in result.flags


def test_available_path_selects_allowlist_sandbox_before_launch(tmp_path, monkeypatch):
    from local_llm_eval import runner, sandbox
    from local_llm_eval.channel import TransportResult

    seen = {}
    monkeypatch.setattr(
        sandbox,
        "availability",
        lambda: sandbox.Availability(True, "mock readiness; OS behavior not tested"),
    )

    def capture(command, cwd, env, spec, timeout):
        seen["command"] = command
        seen["profile"] = Path(command[2]).read_text()
        assert "code" in spec and "expected" not in spec
        return TransportResult("OBS", {"v": 3}, 0)

    monkeypatch.setattr(runner, "collect", capture)
    result = run_check(tmp_path, "selection", "rec('v', 3)", mode="holdout")
    assert seen["command"][0] == str(sandbox.SANDBOX)
    assert str(tmp_path.resolve()) in seen["profile"]
    assert result.status == "OBS"


def test_selfcheck_reference_receipt_retains_isolation_flags(monkeypatch):
    from local_llm_eval.selfcheck import reference_rows
    from local_llm_eval.tasks import REGISTRY

    monkeypatch.setenv("LOCAL_LLM_EVAL_SANDBOX_UNAVAILABLE", "measured unavailable")
    rows = reference_rows(REGISTRY["B4-SGL-D"]())
    assert all("sandbox_unavailable" in row.get("flags", []) for row in rows)
