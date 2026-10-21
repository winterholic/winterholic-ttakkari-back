"""Project backed validation for immutable delivery candidates."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def collected_test_count(output: str) -> int:
    """Return pytest's explicit collected count; missing/empty collection fails closed."""
    matches = re.findall(r"collected\s+(\d+)\s+items?", output)
    if matches:
        return int(matches[-1])
    matches = re.findall(r"(?:^|\n)(\d+) tests? collected(?:\s|$)", output)
    if matches:
        return int(matches[-1])
    matches = re.findall(r"(?:^|\n)(\d+) items? collected(?:\s|$)", output)
    return int(matches[-1]) if matches else 0


def passed_test_count(output: str) -> int:
    matches = re.findall(r"\b(\d+) passed\b", output)
    return int(matches[-1]) if matches else 0


async def project_stage_validator(stage: str, candidate: Path, sha: str):
    """Run the repository's real lint/pytest gate inside the detached candidate checkout."""

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=candidate, capture_output=True, text=True, check=False)

    if not candidate.is_dir() or len(sha) != 40:
        return False, "Candidate checkout or SHA is invalid.", ("candidate", "ValidationError")
    git = run(["git", "rev-parse", "HEAD"])
    if git.returncode != 0 or git.stdout.strip() != sha:
        return False, "Candidate HEAD does not match requested tested SHA.", ("candidate_binding", "ValidationError")

    if stage == "stage1":
        lint = run([sys.executable, "-m", "ruff", "check", "src/ttakkari/delivery", "tests/delivery"])
        if lint.returncode != 0:
            return False, (lint.stdout + lint.stderr)[-2000:] or "ruff failed without output", ("ruff", "LintFailure")
        pytest_args = [sys.executable, "-m", "pytest", "-q", "--timeout=120", "-m", "contract"]
    elif stage == "stage2":
        pytest_args = [sys.executable, "-m", "pytest", "-q", "--timeout=120"]
    else:
        return False, f"Unknown validation stage: {stage}", ("stage", "ValidationError")

    result = run(pytest_args)
    output = (result.stdout + result.stderr)[-4000:]
    if result.returncode != 0:
        return False, output or f"pytest exited with status {result.returncode} without output", ("pytest", "TestFailure")
    if passed_test_count(result.stdout + result.stderr) == 0:
        return False, f"{stage} produced no explicit passing-test summary.", ("pytest_summary", "EmptyTestSuite")
    return True, f"tested_sha={sha}\n{output}", None
