"""Independent, revision-pinned probes for the reverse core correction."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

pytest_plugins = ["conftest"]

# Import the test harness before application modules so it selects only its
# per-process temporary PostgreSQL and test-only settings.
import conftest as _test_harness  # noqa: F401

from ttakkari import reverse_tasks
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, ReverseJob, Run, RunStatus
from ttakkari.runs.manager import manager


async def _start(client, auth, monkeypatch, *, repair_limit=0):
    async def no_dispatch(_run_id):
        return None

    monkeypatch.setattr(manager, "dispatch", no_dispatch)
    response = await client.post(
        "/api/reverse",
        headers=auth,
        json={"description": "CLI 계산 결과 재현", "target_type": "cli",
              "mode": "reconstruct", "max_repair_attempts": repair_limit},
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def _write_capture(root: Path, run: Run, *, replica="4\n") -> dict:
    raw = {
        "schema_version": 2, "target_type": "cli", "run_id": str(run.id), "complete": True,
        "input": {"argv": ["calc", "2"]}, "pre_state": {}, "environment": {"os": "probe"},
        "original": {"stdout": "4\n", "stderr": "", "exit_code": 0},
        "replica": {"stdout": replica, "stderr": "", "exit_code": 0},
    }
    raw["execution_log"] = [
        {"side": side, **raw[key]} for side, key in (("original", "original"), ("replica", "replica"))
    ]
    evidence_path = root / ".reverse" / "comparisons" / f"{run.id}.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(raw).encode()
    evidence_path.write_bytes(encoded)
    return {
        "id": "calc-two", "behavior": "2 입력의 결과", "target_type": "cli",
        "input": raw["input"], "pre_state": {}, "original": raw["original"],
        "replica": raw["replica"], "environment": raw["environment"],
        "evidence_level": "direct",
        "evidence": [{"kind": "execution", "path": evidence_path.relative_to(root).as_posix(),
                      "sha256": hashlib.sha256(encoded).hexdigest()}],
        "verified_at": "2026-11-07T00:00:00Z", "run_id": str(run.id),
        "result": "match", "difference": None,
    }


def _write_report(root: Path, run: Run, *, replica="4\n", comparisons=True,
                  failed_required=False) -> None:
    valid_comparison = _write_capture(root, run, replica=replica)
    checks = [{"command": "python -m pytest", "exit_code": 0, "log": "probe.log"}]
    if failed_required:
        checks.append({"command": "python -m pytest regression", "exit_code": 1, "log": "probe.log"})
    (root / "src").mkdir(exist_ok=True)
    (root / "src/main.py").write_text("print(4)\n")
    (root / "README.md").write_text("환경: Python. 의존성 없음. 실행: python src/main.py. 테스트: pytest -q\n")
    (root / "probe.log").write_text("probe output\n")
    report = {
        "summary": "독립 core 감사 fixture", "source_files": ["src/main.py"],
        "observations": [{"finding": "2 입력은 4 출력", "evidence": "probe fixture", "level": "direct"}],
        "checks": checks, "limitations": [], "verdict": "passed",
        "comparisons": [valid_comparison] if comparisons else [],
    }
    report_dir = root / ".reverse"
    report_dir.mkdir(exist_ok=True)
    (report_dir / f"{run.id}.json").write_text(json.dumps(report))


async def _advance_implementation(db, job: ReverseJob, *, replica="4\n") -> tuple[Run, Run]:
    implementation = await db.scalar(select(Run).where(Run.session_id == job.session_id)
                                     .order_by(Run.created_at.asc()))
    implementation.status = RunStatus.succeeded
    _write_report(Path(job.output_path), implementation, replica=replica)
    await reverse_tasks.on_finished(db, implementation)
    verifier = await db.get(ChatSession, job.verifier_session_id)
    verification_run = await db.scalar(select(Run).where(Run.session_id == verifier.id))
    return implementation, verification_run


async def _finish_verifier(db, job: ReverseJob, run: Run, *, replica="4\n", comparisons=True,
                          failed_required=False) -> None:
    run.status = RunStatus.succeeded
    _write_report(Path(job.output_path), run, replica=replica, comparisons=comparisons,
                  failed_required=failed_required)
    await reverse_tasks.on_finished(db, run)


@pytest.mark.parametrize(
    ("replica", "comparisons", "failed_required", "expected_comparison"),
    [("4\n", False, False, "unverified"), ("5\n", True, False, "mismatch"),
     ("4\n", True, True, "match")],
)
async def test_verifier_evidence_failures_never_finish_job(
    client, auth, monkeypatch, replica, comparisons, failed_required, expected_comparison
):
    job_id = await _start(client, auth, monkeypatch)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(job_id))
        _, verifier_run = await _advance_implementation(db, job)
        await _finish_verifier(db, job, verifier_run, replica=replica, comparisons=comparisons,
                               failed_required=failed_required)
        assert job.status == "partial"
        latest = job.result["latest"]
        assert latest["independent_verification"]["status"] == "failed"
        assert latest["verification_direct_comparison"]["status"] == expected_comparison
        assert latest["verification_run_id"] == str(verifier_run.id)
        assert job.result["job"]["repair_attempts"] == 0


async def test_verifier_mismatch_repairs_same_session_then_reverifies_and_keeps_failure_at_limit(
    client, auth, monkeypatch
):
    job_id = await _start(client, auth, monkeypatch, repair_limit=1)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(job_id))
        original_session_id = job.session_id
        implementation, verifier1 = await _advance_implementation(db, job)

        await _finish_verifier(db, job, verifier1, replica="5\n")
        assert job.status == "running"
        assert job.session_id == original_session_id
        assert job.result["job"]["repair_attempts"] == 1
        assert job.result["latest"]["verification_direct_comparison"]["status"] == "mismatch"
        assert job.result["latest"]["independent_verification"]["status"] == "failed"
        repair = await db.scalar(select(Run).where(Run.session_id == original_session_id)
                                 .order_by(Run.created_at.desc()))
        assert repair.id != implementation.id and repair.usage["reverse"]["role"] == "repair"
        assert "독립 검증 결함 중심 수정 요청" in repair.prompt
        assert job.verifier_session_id is None

        assert await reverse_tasks.on_finished(db, verifier1) is None
        assert job.result["latest"]["verification_run_id"] == str(verifier1.id)

        repair.status = RunStatus.succeeded
        _write_report(Path(job.output_path), repair)
        await reverse_tasks.on_finished(db, repair)
        verifier2_id = job.verifier_session_id
        assert verifier2_id and verifier2_id != verifier1.session_id
        assert job.result["job"]["repair_attempts"] == job.result["job"]["max_repair_attempts"] == 1
        assert job.result["history"] and job.result["history"][0]["run_id"] == str(implementation.id)
        verifier2 = await db.get(ChatSession, verifier2_id)
        review2 = await db.scalar(select(Run).where(Run.session_id == verifier2.id))

        await _finish_verifier(db, job, review2, replica="5\n")
        assert job.status == "partial"
        final = job.result["latest"]
        assert final["independent_verification"]["status"] == "failed"
        assert final["verification_direct_comparison"]["status"] == "mismatch"
        assert final["verification_comparisons"][0]["result"] == "mismatch"
        assert final["verification_run_id"] == str(review2.id)
        assert job.result["job"]["repair_attempts"] == 1
        assert not await db.scalar(select(Run.id).where(
            Run.session_id == original_session_id,
            Run.usage["reverse"]["role"].astext == "repair",
            Run.id != repair.id,
        ))


async def test_success_duplicate_late_cancel_retry_and_public_detail_are_stable(
    client, auth, monkeypatch
):
    job_id = await _start(client, auth, monkeypatch)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(job_id))
        implementation, verifier_run = await _advance_implementation(db, job)
        await _finish_verifier(db, job, verifier_run)
        before = json.loads(json.dumps(job.result))
        run_count = len(list(await db.scalars(select(Run).where(
            Run.usage["reverse"]["job_id"].astext == job_id))))

        await reverse_tasks.on_finished(db, verifier_run)
        assert job.result["history"] == before["history"]
        assert job.result["latest"]["run_id"] == before["latest"]["run_id"]
        assert len(list(await db.scalars(select(Run).where(
            Run.usage["reverse"]["job_id"].astext == job_id)))) == run_count
        await db.commit()

    success_detail_response = await client.get(f"/api/reverse/{job_id}", headers=auth)
    assert success_detail_response.status_code == 200
    success_detail = success_detail_response.json()
    assert success_detail["result"]["latest"] == success_detail["latest"]
    assert success_detail["result"]["history"] == success_detail["history"]
    assert success_detail["result_state"]["source"]["files"] == ["src/main.py"]
    assert success_detail["result_state"]["direct_comparison"]["count"] == 1
    assert success_detail["result_state"]["independent_verification"]["status"] == "passed"

    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(job_id))
        verifier_run.status = RunStatus.cancelled
        await reverse_tasks.on_finished(db, verifier_run)
        assert job.status == "partial"
        assert job.result["latest"]["independent_verification"]["status"] == "cancelled"
        await db.commit()

    retry = await client.post(f"/api/reverse/{job_id}/retry", headers=auth)
    assert retry.status_code == 202, retry.text
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(job_id))
        assert job.result["latest"] is None
        assert job.result["history"][-1]["run_id"] == str(implementation.id)
        implementation_session = await db.get(ChatSession, job.session_id)
        resumed = await db.scalar(select(Run).where(Run.session_id == implementation_session.id)
                                  .order_by(Run.created_at.desc()))
        assert resumed.id != implementation.id and resumed.status == RunStatus.queued
        assert job.verifier_session_id is None
        after_retry = json.loads(json.dumps(job.result))

        assert await reverse_tasks.on_finished(db, verifier_run) is None
        assert job.result == after_retry
        await db.commit()

    detail_response = await client.get(f"/api/reverse/{job_id}", headers=auth)
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["result"]["latest"] == detail["latest"] is None
    assert detail["result"]["history"] == detail["history"]
    assert detail["result_state"]["source"]["files"] == []
    assert detail["result_state"]["direct_comparison"]["count"] == 0
    assert detail["result_state"]["independent_verification"]["status"] == "pending"
