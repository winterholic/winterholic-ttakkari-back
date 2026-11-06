"""Independent black-box probes for Reverse v2's evidence promotion contract.

This lives outside implementation tests. It imports only the repository's existing
isolated test harness (tests/conftest.py) for API/DB integration probes.
"""
pytest_plugins = ("conftest",)

import base64
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from ttakkari.reverse_compare import run_comparison
from ttakkari.reverse_tasks import validate_report, verify_comparisons, _store_latest


def _report(root: Path, *, checks=None, comparisons=None, run_id="audit-run", verdict=None):
    (root / "src").mkdir(exist_ok=True)
    (root / ".reverse").mkdir(exist_ok=True)
    (root / "src/main.py").write_text("print('ok')\n")
    (root / "README.md").write_text("환경 Python 3.13; 의존성 없음; 실행 python src/main.py; 테스트 pytest -q")
    (root / "check.log").write_text("audit output\n")
    return {"summary": "audit", "source_files": ["src/main.py"],
            "observations": [{"finding": "sample", "evidence": "fixture"}],
            "checks": checks if checks is not None else [
                {"command": "python src/main.py", "exit_code": 0, "expected_exit_code": 0,
                 "required": True, "log": "check.log"}],
            "comparisons": comparisons if comparisons is not None else [], "verdict": verdict}


def _write_capture(root: Path, record: dict, *, raw_override=None):
    raw_path = root / record["evidence"][0]["path"]
    raw = json.loads(raw_path.read_text()) if raw_override is None else raw_override
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    raw_path.write_bytes(encoded)
    record["evidence"][0]["sha256"] = hashlib.sha256(encoded).hexdigest()
    return raw


def test_cli_invalid_utf8_is_compared_as_bytes(tmp_path):
    py = sys.executable
    rec = run_comparison(tmp_path, comparison_id="invalid-utf8", behavior="invalid UTF8",
        target_type="cli", input_data={}, original_command=[py, "-c", "import sys;sys.stdout.buffer.write(b'\\xff')"],
        replica_command=[py, "-c", "import sys;sys.stdout.buffer.write(b'\\xfe')"], run_id="audit-utf8")
    assert rec["original"]["stdout"] == rec["replica"]["stdout"] == "�"
    assert rec["result"] == "mismatch"
    assert rec["difference"]["field"] == "stdout_bytes_b64"
    assert verify_comparisons(tmp_path, [rec], expected_run_id="audit-utf8")[0]["result"] == "mismatch"


@pytest.mark.parametrize("kind,cmd", [
    ("timeout", [sys.executable, "-c", "import time;time.sleep(0.2)"]),
    ("unavailable", ["/audit/no-such-executable"]),
    ("truncated", [sys.executable, "-c", "import sys;sys.stdout.write('x'*70000)"]),
])
def test_incomplete_cli_capture_never_becomes_direct(tmp_path, kind, cmd):
    rec = run_comparison(tmp_path, comparison_id=f"case-{kind}", behavior=kind, target_type="cli",
        input_data={}, original_command=cmd, replica_command=[sys.executable, "-c", "print('ok')"],
        run_id=f"audit-{kind}", timeout_seconds=0.03 if kind == "timeout" else 1)
    assert rec["result"] == "unverified"
    assert rec["original"] is None or rec["evidence_level"] == "observed"
    assert verify_comparisons(tmp_path, [rec], expected_run_id=f"audit-{kind}")[0]["result"] == "unverified"


def test_report_checker_recomputes_forgery_and_run_linkage(tmp_path):
    good = run_comparison(tmp_path, comparison_id="same", behavior="same", target_type="cli",
        input_data={"argv": ["x"]}, original_command=[sys.executable, "-c", "print('x')"],
        replica_command=[sys.executable, "-c", "print('x')"], run_id="audit-valid")
    assert good["result"] == "match"
    forged = {**good, "result": "mismatch", "difference": {"forged": True}, "evidence_level": "inferred"}
    assert verify_comparisons(tmp_path, [forged], expected_run_id="audit-valid")[0]["result"] == "match"
    stale = verify_comparisons(tmp_path, [good], expected_run_id="another-run")[0]
    assert stale["result"] == "unverified" and "run_id" in stale["difference"]["reason"]
    missing = {**good, "evidence": [{"kind": "execution", "path": ".reverse/comparisons/deleted.json", "sha256": "0" * 64}]}
    assert verify_comparisons(tmp_path, [missing], expected_run_id="audit-valid")[0]["result"] == "unverified"
    path = tmp_path / good["evidence"][0]["path"]
    path.write_text("{corrupt json")
    assert verify_comparisons(tmp_path, [good], expected_run_id="audit-valid")[0]["result"] == "unverified"


@pytest.mark.parametrize("field,value", [
    ("input", {"argv": ["changed"]}),
    ("environment", {"os": "different"}),
])
def test_input_and_environment_must_match_hashed_capture(tmp_path, field, value):
    rec = run_comparison(tmp_path, comparison_id=f"link-{field}", behavior="link", target_type="cli",
        input_data={"argv": ["original"]}, original_command=[sys.executable, "-c", "print('x')"],
        replica_command=[sys.executable, "-c", "print('x')"], run_id="audit-link")
    rec[field] = value
    checked = verify_comparisons(tmp_path, [rec], expected_run_id="audit-link")[0]
    assert checked["result"] == "unverified"
    assert "일치하지 않습니다" in checked["difference"]["reason"]


@pytest.mark.parametrize("field,value", [
    ("status", 503), ("body", {"body": "other", "body_bytes_b64": base64.b64encode(b"other").decode()}),
    ("headers", {"content-type": "text/plain"}),
])
def test_api_response_status_body_and_headers_are_recomputed(tmp_path, field, value):
    root = tmp_path
    (root / ".reverse/comparisons").mkdir(parents=True)
    original = {"status": 200, "body": "ok", "body_bytes_b64": base64.b64encode(b"ok").decode(),
                "headers": {"content-type": "application/json"}}
    replica = dict(original)
    if field == "body":
        replica.update(value)
    else:
        replica[field] = value
    raw = {"schema_version": 2, "target_type": "api", "input": {}, "pre_state": {},
        "environment": {"os": "test"}, "original": original, "replica": replica,
        "execution_log": [{"side": side, "method": "GET", "url": "http://127.0.0.1/test",
            "request_headers": {"Accept": "*/*"}, "output": output}
            for side, output in (("original", original), ("replica", replica))],
        "verified_at": "2026-11-07T00:00:00Z", "run_id": "audit-api", "complete": True}
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path = root / ".reverse/comparisons/api-case.json"; path.write_bytes(encoded)
    rec = {"id": "api-case", "behavior": "response contract", "target_type": "api", "input": {},
        "pre_state": {}, "original": original, "replica": replica, "environment": {"os": "test"},
        "evidence_level": "direct", "evidence": [{"kind": "execution", "path": ".reverse/comparisons/api-case.json",
            "sha256": hashlib.sha256(encoded).hexdigest()}], "verified_at": raw["verified_at"],
        "run_id": "audit-api", "result": "match", "difference": None}
    checked = verify_comparisons(root, [rec], expected_run_id="audit-api")[0]
    expected_field = "body_bytes_b64" if field == "body" else field
    assert checked["result"] == "mismatch" and checked["difference"]["field"] == expected_field


def test_api_request_error_remains_unverified(tmp_path):
    root = tmp_path; (root / ".reverse/comparisons").mkdir(parents=True)
    replica = {"status": 200, "body": "ok", "body_bytes_b64": base64.b64encode(b"ok").decode(), "headers": {}}
    raw = {"schema_version": 2, "target_type": "api", "input": {}, "pre_state": {}, "environment": {},
        "original": None, "replica": replica,
        "execution_log": [{"side": "original", "method": "GET", "url": "http://127.0.0.1/a",
            "request_headers": {}, "output": None, "error": "TimeoutError: timed out"},
            {"side": "replica", "method": "GET", "url": "http://127.0.0.1/b",
             "request_headers": {}, "output": replica, "error": None}],
        "verified_at": "2026-11-07T00:00:00Z", "run_id": "audit-api-error", "complete": False}
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    path = root / ".reverse/comparisons/api-error.json"; path.write_bytes(encoded)
    rec = {"id": "api-error", "behavior": "request error", "target_type": "api", "input": {}, "pre_state": {},
        "original": None, "replica": replica, "environment": {}, "evidence_level": "observed", "evidence": [
            {"kind": "execution", "path": ".reverse/comparisons/api-error.json", "sha256": hashlib.sha256(encoded).hexdigest()}],
        "verified_at": raw["verified_at"], "run_id": raw["run_id"], "result": "match", "difference": None}
    checked = verify_comparisons(root, [rec], expected_run_id=raw["run_id"])[0]
    assert checked["result"] == "unverified"


def test_web_functional_visual_evidence_stays_unverified_for_completion(tmp_path):
    from ttakkari.reverse_compare import run_web_comparison
    web = run_web_comparison(tmp_path, comparison_id="web-case", behavior="button state",
        input_data={"event": "click"}, original_observation={"events": ["click"], "dom": "ok"},
        replica_observation={"events": ["click"], "dom": "ok"}, run_id="audit-web",
        screenshots={"original": "original.png", "replica": "replica.png"},
        visual_assessment={"result": "match", "reviewer": "independent"})
    assert web["result"] == "match"
    raw = json.loads((tmp_path / web["evidence"][0]["path"]).read_text())
    assert raw["functional_result"] == "match" and raw["visual_result"] == "match"
    checked = verify_comparisons(tmp_path, [web], expected_run_id="audit-web")[0]
    assert checked["result"] == "unverified"


def test_required_failure_and_zero_comparisons_remain_unverified(tmp_path):
    failed = _report(tmp_path, comparisons=[], checks=[
        {"command": "pass", "exit_code": 0, "required": True, "log": "check.log"},
        {"command": "required failure", "exit_code": 1, "required": True, "log": "check.log"}], run_id="audit-required")
    checked = validate_report(tmp_path, failed, expected_run_id="audit-required")
    assert checked["ready"] is False and checked["execution_ready"] is False
    assert checked["direct_comparison"] == {"status": "unverified", "count": 0, "matched": 0, "mismatched": 0, "unverified": 0}
    assert checked["verdict"] is None


def test_symlink_and_path_escape_cannot_supply_source_or_capture(tmp_path):
    from ttakkari.reverse_tasks import _file
    root = tmp_path / "workspace"; root.mkdir()
    outside = tmp_path / "outside.py"; outside.write_text("print('outside')\n")
    (root / "escape.py").symlink_to(outside)
    assert _file(root, "escape.py") is None
    assert _file(root, "../outside.py") is None
    assert _file(root, "/etc/passwd") is None


def test_history_append_preserves_prior_latest():
    class Job:
        result = {"schema_version": 2, "latest": {"run_id": "old", "comparisons": [{"id": "one"}]}, "history": []}
    _store_latest(Job, {"run_id": "new"})
    assert Job.result["history"] == [{"run_id": "old", "comparisons": [{"id": "one"}]}]
    assert Job.result["latest"] == {"run_id": "new"}


@pytest.mark.asyncio
async def test_actual_detail_payload_is_flattened_and_result_is_null(client, auth, monkeypatch):
    from ttakkari.runs.manager import manager
    async def dispatch(_):
        return None
    monkeypatch.setattr(manager, "dispatch", dispatch)
    started = await client.post("/api/reverse", headers=auth,
        json={"description": "UI fixture API 출력 shape 확인을 위한 CLI 동작 설명"})
    assert started.status_code == 202, started.text
    detail = await client.get("/api/reverse/" + started.json()["id"], headers=auth)
    assert detail.status_code == 200, detail.text
    data = detail.json()
    assert data["result"] is None
    assert "latest" in data and "history" in data and "result_state" in data
    assert all(key in data["result_state"] for key in ("source", "execution", "direct_comparison", "independent_verification"))
    assert all(key in stage for stage in data["stages"] for key in ("key", "status", "evidence", "notes"))
    fixture = Path(__file__).parents[1] / "fixtures" / "detail-response-shape.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(json.dumps({k: v for k, v in data.items() if k in {
        "kind", "status", "stage", "result", "latest", "history", "result_state", "stages", "reconstruction"
    }}, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_reverse_start_requires_authentication(client):
    response = await client.post("/api/reverse", json={"description": "unauthenticated audit input"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_agent_route_rejects_work_pool_session():
    from fastapi import HTTPException
    from ttakkari.api.reverse import agent_access
    from ttakkari.models import ChatSession
    import uuid
    class FakeDb:
        async def get(self, _model, _sid):
            return ChatSession(id=uuid.uuid4(), pool="work")
    with pytest.raises(HTTPException) as exc:
        await agent_access(uuid.uuid4(), FakeDb())
    assert exc.value.status_code == 403


async def _start(client, auth, monkeypatch, *, mode="reconstruct", repairs=0):
    from ttakkari.runs.manager import manager
    async def dispatch(_):
        return None
    monkeypatch.setattr(manager, "dispatch", dispatch)
    response = await client.post("/api/reverse", headers=auth, json={
        "description": "독립 감사용 동작 입력과 기대 출력을 확인한다.", "mode": mode,
        "target_type": "cli", "max_repair_attempts": repairs})
    assert response.status_code == 202, response.text
    from ttakkari.db import sessionmaker
    from ttakkari.models import ReverseJob, Run
    import uuid
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(response.json()["id"]))
        impl = await db.scalar(__import__("sqlalchemy").select(Run).where(Run.session_id == job.session_id))
        root = Path(job.output_path)
        root.mkdir(parents=True, exist_ok=True)
        (root / "src").mkdir(exist_ok=True)
        (root / "src/main.py").write_text("print('same')\n")
        (root / "README.md").write_text("환경 Python 3.13; 의존성 없음; 실행 python src/main.py; 테스트 pytest -q")
        (root / "check.log").write_text("check passed\n")
        return str(job.id), impl.id, root


def _comparison(root, run_id, suffix, result):
    original = [sys.executable, "-c", "print('same')"]
    replica = [sys.executable, "-c", f"print({result!r})"]
    return run_comparison(root, comparison_id=f"{suffix}-{str(run_id)[:8]}", behavior="same output",
        target_type="cli", input_data={}, original_command=original, replica_command=replica,
        run_id=str(run_id))


def _full_report(comp, *, verdict=None):
    return {"summary": "audit report", "mode": "reconstruct", "source_files": ["src/main.py"],
        "observations": [{"finding": "same input gives same output", "evidence": "local fixture"}],
        "checks": [{"command": "python src/main.py", "exit_code": 0, "expected_exit_code": 0,
            "required": True, "log": "check.log"}], "comparisons": [comp] if comp is not None else [], "verdict": verdict}


def _save_detail_fixture(data):
    fixture = Path(__file__).parents[1] / "fixtures" / "completed-detail-response-shape.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(json.dumps({k: v for k, v in data.items() if k in {
        "kind", "status", "stage", "result", "latest", "history", "result_state", "stages", "reconstruction"
    }}, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_passed_verifier_cannot_promote_zero_direct_comparisons(client, auth, monkeypatch):
    from ttakkari import reverse_tasks
    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession, ReverseJob, Run, RunStatus
    from sqlalchemy import select
    import uuid
    jid, impl_id, root = await _start(client, auth, monkeypatch)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(jid)); impl = await db.get(Run, impl_id)
        impl.status = RunStatus.succeeded
        (root / ".reverse").mkdir(exist_ok=True)
        (root / ".reverse" / f"{impl.id}.json").write_text(json.dumps(_full_report(None)))
        await reverse_tasks.on_finished(db, impl)
        verifier = await db.get(ChatSession, job.verifier_session_id)
        review = await db.scalar(select(Run).where(Run.session_id == verifier.id))
        review.status = RunStatus.succeeded
        (root / ".reverse" / f"{review.id}.json").write_text(json.dumps(_full_report(None, verdict="passed")))
        await reverse_tasks.on_finished(db, review)
        assert job.status == "partial"
        assert job.result["latest"]["direct_comparison"]["count"] == 0
        assert job.result["latest"]["independent_verification"]["status"] == "failed"
        await db.commit()


@pytest.mark.asyncio
async def test_verifier_mismatch_does_not_enter_bounded_repair_cycle(client, auth, monkeypatch):
    from ttakkari import reverse_tasks
    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession, ReverseJob, Run, RunStatus
    from sqlalchemy import select
    import uuid
    jid, impl_id, root = await _start(client, auth, monkeypatch, repairs=2)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(jid)); impl = await db.get(Run, impl_id)
        impl.status = RunStatus.succeeded
        (root / ".reverse").mkdir(exist_ok=True)
        impl_report = _full_report(_comparison(root, impl.id, "impl", "same"))
        (root / ".reverse" / f"{impl.id}.json").write_text(json.dumps(impl_report))
        await reverse_tasks.on_finished(db, impl)
        verifier = await db.get(ChatSession, job.verifier_session_id)
        review = await db.scalar(select(Run).where(Run.session_id == verifier.id))
        review.status = RunStatus.succeeded
        verify_report = _full_report(_comparison(root, review.id, "verify", "different"), verdict="failed")
        (root / ".reverse" / f"{review.id}.json").write_text(json.dumps(verify_report))
        await reverse_tasks.on_finished(db, review)
        runs = list(await db.scalars(select(Run).where(Run.usage["reverse"]["job_id"].astext == jid)))
        assert job.status == "partial"
        assert job.result["latest"]["direct_comparison"]["status"] == "match"
        assert job.result["latest"]["verification_report"]["direct_comparison"]["status"] == "mismatch"
        assert job.result["job"]["repair_attempts"] == 0
        assert len(runs) == 2  # verifier mismatch yields no bounded repair or fresh re-verification
        await db.commit()


@pytest.mark.asyncio
async def test_direct_mismatch_repair_is_bounded_and_verifier_is_linked_to_latest_round(client, auth, monkeypatch):
    from ttakkari import reverse_tasks
    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession, ReverseJob, Run, RunStatus
    from sqlalchemy import select
    import uuid
    jid, impl_id, root = await _start(client, auth, monkeypatch, repairs=1)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(jid)); impl = await db.get(Run, impl_id)
        (root / ".reverse").mkdir(exist_ok=True)
        impl.status = RunStatus.succeeded
        (root / ".reverse" / f"{impl.id}.json").write_text(json.dumps(
            _full_report(_comparison(root, impl.id, "impl-mismatch", "wrong"))))
        await reverse_tasks.on_finished(db, impl)
        repair = await db.scalar(select(Run).where(Run.session_id == job.session_id).order_by(Run.created_at.desc()))
        assert repair.id != impl.id and repair.usage["reverse"]["role"] == "repair"
        assert job.result["job"]["repair_attempts"] == 1
        repair.status = RunStatus.succeeded
        (root / ".reverse" / f"{repair.id}.json").write_text(json.dumps(
            _full_report(_comparison(root, repair.id, "repair-mismatch", "still-wrong"))))
        await reverse_tasks.on_finished(db, repair)
        verifier = await db.get(ChatSession, job.verifier_session_id)
        review = await db.scalar(select(Run).where(Run.session_id == verifier.id))
        assert review.usage["reverse"]["role"] == "verify"
        assert job.result["latest"]["run_id"] == str(repair.id)
        assert job.result["latest"]["comparisons"][0]["run_id"] == str(repair.id)
        assert job.result["job"]["repair_attempts"] == job.result["job"]["max_repair_attempts"] == 1
        assert job.result["history"][0]["run_id"] == str(impl.id)
        await db.commit()


@pytest.mark.asyncio
async def test_duplicate_verifier_callback_appends_duplicate_history(client, auth, monkeypatch):
    from ttakkari import reverse_tasks
    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession, ReverseJob, Run, RunStatus
    from sqlalchemy import select
    import uuid
    jid, impl_id, root = await _start(client, auth, monkeypatch)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(jid)); impl = await db.get(Run, impl_id)
        impl.status = RunStatus.succeeded
        (root / ".reverse").mkdir(exist_ok=True)
        (root / ".reverse" / f"{impl.id}.json").write_text(json.dumps(_full_report(_comparison(root, impl.id, "impl", "same"))))
        await reverse_tasks.on_finished(db, impl)
        verifier = await db.get(ChatSession, job.verifier_session_id)
        review = await db.scalar(select(Run).where(Run.session_id == verifier.id))
        review.status = RunStatus.succeeded
        (root / ".reverse" / f"{review.id}.json").write_text(json.dumps(_full_report(_comparison(root, review.id, "verify", "same"), verdict="passed")))
        await reverse_tasks.on_finished(db, review)
        first_history_len = len(job.result.get("history", []))
        await reverse_tasks.on_finished(db, review)
        assert job.status == "done"
        assert len(job.result["history"]) == first_history_len + 1
        assert job.result["history"][-1]["run_id"] == str(impl.id)
        assert job.result["history"][-2]["run_id"] == str(impl.id)
        await db.commit()


@pytest.mark.asyncio
async def test_analyze_only_then_mode_switch_reuses_observations_without_source_requirement(client, auth, monkeypatch):
    from ttakkari import reverse_tasks
    from ttakkari.db import sessionmaker
    from ttakkari.models import ReverseJob, Run, RunStatus
    from sqlalchemy import select
    import uuid
    jid, impl_id, root = await _start(client, auth, monkeypatch, mode="analyze")
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(jid)); impl = await db.get(Run, impl_id)
        impl.status = RunStatus.succeeded
        (root / ".reverse").mkdir(exist_ok=True)
        analysis = {"mode": "analyze", "summary": "state finding", "observations": [
            {"finding": "state persists after restart", "evidence": "evidence/state.txt"}],
            "hypotheses": ["local state"], "unknowns": ["cross-device behavior"]}
        (root / ".reverse" / f"{impl.id}.json").write_text(json.dumps(analysis))
        await reverse_tasks.on_finished(db, impl)
        assert job.status == "done"
        assert job.result["latest"]["source"]["status"] == "unverified"
        assert job.result["latest"]["execution"]["status"] == "unverified"
        assert job.result["latest"]["report"]["observations"][0]["finding"] == "state persists after restart"
        await db.commit()
        detail = (await client.get(f"/api/reverse/{jid}", headers=auth)).json()
        assert detail["result"] is None and detail["latest"]["report"]["observations"]
        assert detail["result_state"]["independent_verification"]["status"] == "not_required"
        _save_detail_fixture(detail)
        switched = await reverse_tasks.update_configuration(db, job, mode="reconstruct")
        next_run = await db.scalar(select(Run).where(Run.session_id == job.session_id).order_by(Run.created_at.desc()))
        assert switched["mode"] == "reconstruct"
        assert job.result["history"][0]["report"]["summary"] == "state finding"
        assert "state persists after restart" in next_run.prompt and "state finding" in job.result["history"][0]["report"]["summary"]
        await db.commit()


@pytest.mark.asyncio
async def test_cancel_then_retry_ignores_late_old_callback(client, auth, monkeypatch):
    from ttakkari import reverse_tasks
    from ttakkari.db import sessionmaker
    from ttakkari.models import ReverseJob, Run
    from sqlalchemy import select
    import uuid
    jid, impl_id, _root = await _start(client, auth, monkeypatch)
    cancel = await client.post(f"/api/runs/{impl_id}/cancel", headers=auth)
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"
    retried = await client.post(f"/api/reverse/{jid}/retry", headers=auth)
    assert retried.status_code == 202, retried.text
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(jid))
        old = await db.get(Run, impl_id)
        before = list(await db.scalars(select(Run).where(Run.session_id == job.session_id).order_by(Run.created_at)))
        assert len(before) == 2 and before[-1].id != old.id
        assert job.status == "queued"
        await reverse_tasks.on_finished(db, old)  # late cancelled callback from old run
        assert job.status == "queued"
        assert job.result["latest"] is None
        await db.commit()
