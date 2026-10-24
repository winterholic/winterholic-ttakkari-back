from pathlib import Path

import pytest

from ttakkari.delivery.validation import (
    collected_test_count,
    passed_test_count,
    project_stage_validator,
)
from ttakkari.delivery.worktree import run_git


def test_pytest_summaries_require_an_explicit_nonempty_suite():
    assert collected_test_count("collected 12 items / 8 deselected / 4 selected") == 12
    assert collected_test_count("0 tests collected") == 0
    assert collected_test_count("no tests ran") == 0
    assert passed_test_count("12 passed, 3 deselected in 2.4s") == 12
    assert passed_test_count("0 passed, 12 deselected") == 0
    assert passed_test_count("no tests ran") == 0


@pytest.mark.asyncio
async def test_stage_validator_runs_tests_from_candidate_checkout_source(
    tmp_path: Path,
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "src" / "ttakkari" / "delivery").mkdir(parents=True)
    (candidate / "src" / "ttakkari" / "__init__.py").write_text(
        'REVISION = "candidate"\n'
    )
    (candidate / "src" / "ttakkari" / "delivery" / "__init__.py").write_text("")
    (candidate / "tests").mkdir()
    (candidate / "tests" / "test_candidate_source.py").write_text(
        "import ttakkari\n\n\ndef test_imports_the_candidate_source():\n"
        "    assert ttakkari.REVISION == 'candidate'\n"
    )
    (candidate / "tests" / "delivery").mkdir()
    (candidate / "tests" / "delivery" / "test_candidate_contract.py").write_text(
        "import pytest\n\nimport ttakkari\n\n\n@pytest.mark.contract\n"
        "def test_contract_imports_the_candidate_source():\n"
        "    assert ttakkari.REVISION == 'candidate'\n"
    )
    (candidate / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    contract: candidate contract gate\n"
    )
    run_git(["init", "-b", "main"], candidate)
    run_git(["config", "user.name", "Validation test"], candidate)
    run_git(["config", "user.email", "validation@example.invalid"], candidate)
    run_git(["add", "."], candidate)
    run_git(["commit", "-m", "candidate source"], candidate)
    sha = run_git(["rev-parse", "HEAD"], candidate).stdout.strip()

    stage1_passed, stage1_log, _ = await project_stage_validator(
        "stage1", candidate, sha
    )
    assert stage1_passed, stage1_log
    assert "1 passed" in stage1_log
    assert f"tested_sha={sha}" in stage1_log

    passed, log, _ = await project_stage_validator("stage2", candidate, sha)

    assert passed, log
    assert "2 passed" in log
    assert f"tested_sha={sha}" in log

    (candidate / "src/ttakkari/__init__.py").write_text('REVISION = "edited"\n')
    passed, log, _ = await project_stage_validator("stage2", candidate, sha)
    assert passed is False
    assert "dirty" in log.lower()
