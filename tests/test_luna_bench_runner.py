"""Fail-fast behavior for nested Codex CLI benchmark calls."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = ROOT / "experiments" / "qwen35-router" / "common30-v2"
sys.path.insert(0, str(BENCH_DIR))

import run_luna_codex as benchmark  # noqa: I001


INIT_EPERM = (
    "Error: failed to initialize in-process app-server client: "
    "Operation not permitted (os error 1)"
)


def test_seatbelt_parent_blocks_nested_cli_before_any_case_call():
    calls = []
    assert callable(getattr(benchmark, "run_cases", None)), "benchmark must expose fail-fast runner"
    result = benchmark.run_cases(
        [{"case_id": "case-1"}],
        lambda case: calls.append(case),
        environ={"CODEX_SANDBOX": "seatbelt"},
    )

    assert result["status"] == "blocked_preflight"
    assert result["failure_category"] == "nested_codex_sandbox"
    assert result["rows"] == []
    assert calls == []


def test_known_app_server_eperm_stops_after_one_and_is_not_scored():
    assert callable(getattr(benchmark, "run_cases", None)), "benchmark must expose fail-fast runner"
    cases = [{"case_id": f"case-{n}"} for n in range(1, 4)]
    calls = []

    def attempt(case):
        calls.append(case["case_id"])
        return {
            "case_id": case["case_id"],
            "stderr": INIT_EPERM,
            "valid": False,
            "exact_correct": False,
            "execution_status": "model_error",
        }

    result = benchmark.run_cases(cases, attempt, environ={})

    assert result["status"] == "blocked_initialization"
    assert result["failure_category"] == "codex_app_server_init_eperm"
    assert calls == ["case-1"]
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["execution_status"] == "blocked_initialization"
    assert row["scored"] is False
    assert row["exact_correct"] is None


def test_benchmark_expands_fixed_cases_to_three_repetitions():
    cases = [{"case_id": f"case-{n}"} for n in range(30)]
    expanded = benchmark.expand_cases(cases)

    assert len(expanded) == 90
    assert [expanded[0]["repetition"], expanded[29]["repetition"], expanded[30]["repetition"]] == [1, 1, 2]


def test_instability_counts_case_actions_that_change_between_repetitions():
    rows = [
        {"case_id": "stable", "repetition": n, "valid": True, "output": {"action": "answer"}}
        for n in range(1, 4)
    ] + [
        {"case_id": "unstable", "repetition": n, "valid": True, "output": {"action": action}}
        for n, action in enumerate(("answer", "abstain", "answer"), 1)
    ]

    summary = benchmark.summarize_instability(rows)

    assert summary == {"analyzed_case_count": 2, "unstable_case_count": 1, "unstable_case_ids": ["unstable"]}
