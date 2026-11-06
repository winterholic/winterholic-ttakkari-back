import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from ttakkari.reverse_compare import run_comparison
from ttakkari.reverse_tasks import verify_comparisons


def test_run_comparison_emits_recomputable_cli_evidence(tmp_path):
    original = [sys.executable, "-c", "import sys; print(sys.argv[1].replace(' ', '-'))"]
    replica = [sys.executable, "-c", "import sys; print(sys.argv[1].replace(' ', '_'))"]

    comparison = run_comparison(
        tmp_path,
        comparison_id="slug-space",
        behavior="공백 포함 이름 정규화",
        target_type="cli",
        input_data={"argv": ["two words.txt"]},
        original_command=original,
        replica_command=replica,
        run_id="run-1",
        verified_at="2026-11-07T00:00:00Z",
    )

    assert set(comparison) == {
        "id", "behavior", "target_type", "input", "pre_state", "original", "replica",
        "environment", "evidence_level", "evidence", "verified_at", "run_id", "result", "difference",
    }
    assert {key: comparison["original"][key] for key in ("stdout", "stderr", "exit_code")} == {
        "stdout": "two-words.txt\n", "stderr": "", "exit_code": 0
    }
    assert {key: comparison["replica"][key] for key in ("stdout", "stderr", "exit_code")} == {
        "stdout": "two_words.txt\n", "stderr": "", "exit_code": 0
    }
    assert comparison["result"] == "mismatch"
    assert comparison["difference"] == {
        "field": "stdout", "original": "two-words.txt\n", "replica": "two_words.txt\n"
    }

    evidence = comparison["evidence"][0]
    evidence_path = tmp_path / evidence["path"]
    raw_bytes = evidence_path.read_bytes()
    raw = json.loads(raw_bytes)
    assert evidence["path"] == ".reverse/comparisons/slug-space-run-1.json"
    assert evidence["sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert raw["input"] == comparison["input"]
    assert raw["pre_state"] == comparison["pre_state"]
    assert raw["environment"] == comparison["environment"]
    assert raw["original"] == comparison["original"]
    assert raw["replica"] == comparison["replica"]
    assert raw["complete"] is True
    assert raw["execution_log"][0]["exit_code"] == 0
    recomputed = verify_comparisons(tmp_path, [comparison], expected_run_id="run-1")[0]
    assert (recomputed["evidence_level"], recomputed["result"]) == ("direct", "mismatch")


def test_run_comparison_marks_unavailable_original_unverified(tmp_path):
    replica = [sys.executable, "-c", "print('replica ran')"]

    comparison = run_comparison(
        tmp_path,
        comparison_id="original-missing",
        behavior="실행 불가 원본",
        target_type="cli",
        input_data={"argv": []},
        original_command=[str(tmp_path / "missing-command")],
        replica_command=replica,
        run_id="run-2",
        verified_at="2026-11-07T00:00:00Z",
    )

    assert comparison["original"] is None
    assert {key: comparison["replica"][key] for key in ("stdout", "stderr", "exit_code")} == {
        "stdout": "replica ran\n", "stderr": "", "exit_code": 0
    }
    assert comparison["result"] == "unverified"
    assert comparison["evidence_level"] == "observed"
    assert "original" in comparison["difference"]["reason"].lower()
    raw = json.loads((tmp_path / comparison["evidence"][0]["path"]).read_text("utf-8"))
    assert raw["original"] is None
    assert raw["execution_log"][0]["error"]


def test_incomplete_execution_is_not_reported_as_direct(tmp_path):
    comparison = run_comparison(
        tmp_path,
        comparison_id="timeout-case",
        behavior="시간 초과 원본",
        target_type="cli",
        input_data={"argv": []},
        original_command=[sys.executable, "-c", "import time; time.sleep(1)"],
        replica_command=[sys.executable, "-c", "print('ok')"],
        run_id="run-timeout",
        timeout_seconds=1,
    )

    assert comparison["result"] == "unverified"
    assert comparison["evidence_level"] == "observed"
    raw = json.loads((tmp_path / comparison["evidence"][0]["path"]).read_text("utf-8"))
    assert raw["complete"] is False
    assert raw["execution_log"][0]["error"].startswith("TimeoutExpired")


def test_evidence_name_collision_fails_without_overwriting(tmp_path):
    args = {
        "comparison_id": "stable-case", "behavior": "충돌", "target_type": "cli", "input_data": {},
        "original_command": [sys.executable, "-c", "print('x')"],
        "replica_command": [sys.executable, "-c", "print('x')"], "run_id": "same-run",
    }
    first = run_comparison(tmp_path, **args)
    evidence = tmp_path / first["evidence"][0]["path"]
    original = evidence.read_bytes()
    import pytest
    with pytest.raises(FileExistsError):
        run_comparison(tmp_path, **args)
    assert evidence.read_bytes() == original


def test_module_cli_reads_spec_and_prints_contract_comparison(tmp_path):
    spec_path = tmp_path / "case.json"
    spec_path.write_text(json.dumps({
        "id": "cli-smoke",
        "behavior": "고정 출력",
        "target_type": "cli",
        "input": {"argv": []},
        "original_command": [sys.executable, "-c", "print('same')"],
        "replica_command": [sys.executable, "-c", "print('same')"],
        "run_id": "run-cli",
    }), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}

    completed = subprocess.run(
        [sys.executable, "-m", "ttakkari.reverse_compare", "--workspace", str(tmp_path), "--spec", str(spec_path)],
        capture_output=True, text=True, encoding="utf-8", env=env, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    comparison = json.loads(completed.stdout)
    assert comparison["result"] == "match"
    assert comparison["original"]["exit_code"] == comparison["replica"]["exit_code"] == 0


def test_timed_out_original_is_null_and_comparison_stays_unverified(tmp_path):
    comparison = run_comparison(
        tmp_path,
        comparison_id="slow-original",
        behavior="시간 제한 초과",
        target_type="cli",
        input_data={"argv": []},
        original_command=[sys.executable, "-c", "import time; time.sleep(1)"],
        replica_command=[sys.executable, "-c", "print('replica')"],
        run_id="run-timeout",
        timeout_seconds=0.05,
    )

    assert comparison["original"] is None
    assert comparison["result"] == "unverified"
    assert comparison["difference"]["reason"]
    capture = json.loads((tmp_path / comparison["evidence"][0]["path"]).read_text("utf-8"))
    assert capture["original"] is None
    assert "timeout" in capture["execution_log"][0]["error"]
