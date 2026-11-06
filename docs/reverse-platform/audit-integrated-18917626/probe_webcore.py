"""Independent web evidence and callback probes pinned to 9fb233bd.

This is an audit harness, not an implementation test suite. It calls the public
capture writer, the server comparison consumer, validate_report, and the real
on_finished callback. Only database query/session boundaries are faked.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image

from ttakkari import reverse_tasks as rt
from ttakkari.models import ChatSession, ReverseJob, Run, RunStatus
from ttakkari.reverse_web_evidence import create_web_capture

VIEWPORT = {"width": 2, "height": 2, "device_scale_factor": 1}
INPUT = {"action": "submit", "value": "probe"}
PRE_STATE = {"status": "idle"}
ENVIRONMENT = {"browser": "synthetic", "device": "audit"}
OBSERVED_AT = datetime.now(UTC).isoformat()
BASELINE_SHA = "18917626934ab38302bddfee1c7f4b9ebe6dde73"
AUDIT_PREFIX = "docs/reverse-platform/audit-integrated-18917626/"


def _assert_baseline() -> str:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if subprocess.run(["git", "merge-base", "--is-ancestor", BASELINE_SHA, head], check=False).returncode != 0:
        raise AssertionError(f"fixed product baseline {BASELINE_SHA} is not an ancestor of HEAD {head}")
    changed = subprocess.check_output(["git", "diff", "--name-only", BASELINE_SHA, head], text=True).splitlines()
    outside = [path for path in changed if not path.startswith(AUDIT_PREFIX)]
    if outside:
        raise AssertionError(f"HEAD differs from fixed product baseline outside audit path: {outside}")
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], text=True)
    outside_dirty = [line for line in dirty.splitlines() if AUDIT_PREFIX not in line]
    if outside_dirty:
        raise AssertionError(f"worktree has changes outside audit path: {outside_dirty}")
    return head


def _image(root: Path, name: str, color: tuple[int, int, int]) -> dict[str, Any]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), color).save(path, format="PNG")
    blob = path.read_bytes()
    return {"path": name, "sha256": hashlib.sha256(blob).hexdigest(),
            "size_bytes": len(blob), "viewport": VIEWPORT, "format": "png"}


def _observation(run_id: str, image_ref: dict[str, Any], *, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "observed_at": OBSERVED_AT,
        "url": "https://example.invalid/probe",
        "input": INPUT,
        "viewport": VIEWPORT,
        "pre_state": PRE_STATE,
        "environment": ENVIRONMENT,
        "events": events if events is not None else [{"type": "submit", "value": "probe"}],
        "dom": [{"role": "button", "name": "submit"}],
        "state": {"status": "accepted"},
        "network": [],
        "screenshot": image_ref,
        "complete": True,
        "capture_state": "complete",
    }


def _capture(root: Path, case: str, run_id: str, *, same_pixels: bool,
             same_function: bool, missing_replica: bool = False) -> dict[str, Any]:
    original_ref = _image(root, f".reverse/png/{case}-original.png", (220, 10, 10))
    replica_ref = original_ref if same_pixels else _image(root, f".reverse/png/{case}-replica.png", (10, 10, 220))
    original = _observation(run_id, original_ref)
    replica = None if missing_replica else _observation(
        run_id, replica_ref,
        events=original["events"] if same_function else [{"type": "submit", "value": "different"}],
    )
    return create_web_capture(
        root,
        comparison_id=case,
        behavior=f"independent synthetic case {case}",
        input_data=INPUT,
        original=original,
        replica=replica,
        run_id=run_id,
        expected_viewport=VIEWPORT,
        environment=ENVIRONMENT,
        pre_state=PRE_STATE,
    )


def _verify(root: Path, record: dict[str, Any], expected_run_id: str) -> dict[str, Any]:
    return rt.verify_comparisons(root, [record], expected_run_id=expected_run_id)[0]


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    evidence = record.get("evidence") or [{}]
    item = evidence[0]
    return {
        "functional_result": record.get("functional_result", record.get("result")),
        "visual_result": record.get("visual_result", item.get("visual_result")),
        "integrity": (item.get("integrity") or {}).get("status"),
        "provenance": (item.get("provenance") or {}).get("status"),
        "result": record.get("result"),
        "reason": record.get("difference"),
    }


class _Query:
    def __init__(self, target: Any):
        self.target = target

    def where(self, *_: Any) -> _Query:
        return self

    def order_by(self, *_: Any) -> _Query:
        return self

    def limit(self, *_: Any) -> _Query:
        return self


class _FakeDB:
    """No SQL engine: return explicit fixture rows at SQLAlchemy query boundaries."""
    def __init__(self, job: Any, session: Any, implementation_run: Any):
        self.job = job
        self.session = session
        self.implementation_run = implementation_run
        self.latest_run_id: uuid.UUID | None = None
        self.added: list[Any] = []

    async def get(self, model: Any, _identity: Any, **_: Any) -> Any:
        if model is ReverseJob:
            return self.job
        if model is ChatSession:
            return self.session
        return None

    async def scalar(self, query: _Query) -> Any:
        if query.target is Run.id:
            return self.latest_run_id
        if query.target is Run:
            return self.implementation_run
        raise AssertionError(f"unexpected fake SQL selection: {query.target!r}")

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for item in self.added:
            if isinstance(item, (ChatSession, Run)) and getattr(item, "id", None) is None:
                item.id = uuid.uuid4()
            if isinstance(item, Run) and getattr(item, "last_seq", None) is None:
                item.last_seq = 0
            if isinstance(item, Run) and getattr(item, "status", None) is None:
                item.status = RunStatus.queued


def _write_report(root: Path, run_id: str, comparison: dict[str, Any] | None, *, verdict: str = "passed",
                  mode: str = "analyze_reconstruct", include_source: bool = True,
                  include_check: bool = True) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        "Python 환경. 의존성: Pillow. 실행: python src/demo.py. 테스트: pytest -q.\n",
        encoding="utf-8",
    )
    source = root / "src/demo.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def demo(): return 'ok'\n", encoding="utf-8")
    log = root / "evidence/check.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("probe check passed\n", encoding="utf-8")
    report = {
        "mode": mode,
        "summary": "synthetic callback report",
        "source_files": ["src/demo.py"] if include_source else [],
        "observations": [{"finding": "observed synthetic behavior", "evidence": "synthetic input", "level": "direct"}],
        "hypotheses": [],
        "unknowns": [],
        "comparisons": [comparison] if comparison is not None else [],
        "checks": ([{"command": "python -m pytest -q", "exit_code": 0,
                     "expected_exit_code": 0, "required": True, "log": "evidence/check.log"}]
                   if include_check else []),
        "limitations": ["synthetic; no browser or DB"],
        "verdict": verdict,
    }
    destination = root / f".reverse/{run_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


def _job(root: Path, *, max_repair: int, repair_attempts: int = 0,
         latest: dict[str, Any] | None = None, verifier_session_id: uuid.UUID | None = None) -> tuple[Any, Any, Any, _FakeDB]:
    job_id, session_id, workspace_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = SimpleNamespace(id=session_id, workspace_id=workspace_id, pool="work")
    job = SimpleNamespace(
        id=job_id, kind=rt.KIND, target="web completion probe", workspace_id=workspace_id,
        session_id=session_id, verifier_session_id=verifier_session_id,
        output_path=str(root), status="running", error=None, updated_at=datetime.now(UTC),
        result={"schema_version": 2,
                "job": {"target_type": "web", "requested_target_type": "web", "mode": "analyze_reconstruct",
                        "max_repair_attempts": max_repair, "repair_attempts": repair_attempts},
                "recommendation": {"target_type": "web"}, "plan": [], "latest": latest, "history": []},
    )
    impl = SimpleNamespace(id=uuid.uuid4(), session_id=session_id, usage={}, status=RunStatus.succeeded)
    return job, session, impl, _FakeDB(job, session, impl)


def _run(run_id: uuid.UUID, session_id: uuid.UUID, job_id: uuid.UUID, role: str) -> Any:
    return SimpleNamespace(id=run_id, session_id=session_id, status=RunStatus.succeeded, error=None,
                           usage={"reverse": {"job_id": str(job_id), "role": role}})


async def _callback(db: _FakeDB, run: Any) -> uuid.UUID | None:
    original_select = rt.select
    rt.select = _Query
    try:
        return await rt.on_finished(db, run)
    finally:
        rt.select = original_select


async def _callback_probes(base: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for label, impl_visual, repair_limit in (("implementation_visual_mismatch", "mismatch", 0),
                                             ("implementation_visual_unverified", "unverified", 0),
                                             ("implementation_visual_mismatch_after_repair", "mismatch", 1)):
        root = base / label
        job, session, _impl, db = _job(root, max_repair=repair_limit)
        impl_run_id = uuid.uuid4()
        bad_impl = _capture(root, f"impl-{label}", str(impl_run_id), same_pixels=False,
                            same_function=True, missing_replica=impl_visual == "unverified")
        _write_report(root, str(impl_run_id), bad_impl)
        db.latest_run_id = impl_run_id
        implementation_callback_result = await _callback(db, _run(impl_run_id, session.id, job.id, "implement"))
        repair_behavior: dict[str, Any] = {"limit": repair_limit,
                                           "attempts_after_initial_callback": rt._metadata(job.result)["job"]["repair_attempts"],
                                           "initial_status": job.status,
                                           "repair_queued": any(isinstance(x, Run) and x.usage["reverse"]["role"] == "repair"
                                                                for x in db.added)}
        if repair_limit:
            repair_run = next(x for x in db.added if isinstance(x, Run) and x.usage["reverse"]["role"] == "repair")
            repair_run.status = RunStatus.succeeded
            still_bad = _capture(root, f"repair-still-bad-{label}", str(repair_run.id),
                                 same_pixels=False, same_function=True)
            _write_report(root, str(repair_run.id), still_bad)
            db.latest_run_id = repair_run.id
            await _callback(db, repair_run)
            repair_behavior["attempts_after_repair_callback"] = rt._metadata(job.result)["job"]["repair_attempts"]
            repair_behavior["verifier_started_after_repair"] = job.verifier_session_id is not None
            repair_behavior["history_kept_initial_run"] = any(
                item.get("run_id") == str(impl_run_id) for item in rt._metadata(job.result).get("history", []))
        verifier_session_id = job.verifier_session_id
        assert verifier_session_id is not None

        verify_run_id = uuid.uuid4()
        good_verify = _capture(root, f"verify-{label}", str(verify_run_id), same_pixels=True, same_function=True)
        _write_report(root, str(verify_run_id), good_verify)
        verify_run = _run(verify_run_id, verifier_session_id, job.id, "verify")
        db.latest_run_id = verify_run_id
        await _callback(db, verify_run)
        latest = rt._metadata(job.result)["latest"]
        actual = {
            "job_status": job.status,
            "implementation_direct": latest.get("direct_comparison"),
            "implementation_visual": latest.get("visual_comparison"),
            "verification_direct": latest.get("verification_direct_comparison"),
            "verification_visual": latest.get("verification_visual_comparison"),
            "independent_verification": latest.get("independent_verification"),
        }
        expected_status = "partial"  # Intention: retain the implementation's visual failure.
        results[label] = {"expected_status": expected_status, "actual": actual,
                          "finding": "FAIL" if job.status != expected_status else "PASS",
                          "repair_behavior": repair_behavior,
                          "implementation_callback_result": str(implementation_callback_result)}
        before = copy.deepcopy(job.result)
        status_before_duplicate = job.status
        added_before = len(db.added)
        await _callback(db, verify_run)
        results[label]["duplicate_callback"] = {
            "result_unchanged": job.result == before,
            "status_before": status_before_duplicate,
            "status_after": job.status,
            "status_unchanged": job.status == status_before_duplicate,
            "no_rows_added": len(db.added) == added_before,
        }
        state_before_late = copy.deepcopy(job.result)
        status_before_late = job.status
        db.latest_run_id = uuid.uuid4()
        await _callback(db, verify_run)
        results[label]["late_callback"] = {
            "result_unchanged": job.result == state_before_late,
            "status_before": status_before_late,
            "status_after": job.status,
            "status_unchanged": job.status == status_before_late,
        }

    # A failing verifier with quota remaining requests repair, clears the old
    # verifier session, and a later repair callback creates a fresh verifier.
    root = base / "verifier_failure_repair"
    job, session, _impl, db = _job(root, max_repair=1)
    job.result["latest"] = {
        "run_id": "implementation-seed", "role": "implement",
        "source": {"status": "verified", "files": ["src/demo.py"]},
        "report": {"observations": [{"finding": "seed", "evidence": "seed"}]},
        "direct_comparison": {"status": "match", "count": 1, "matched": 1, "mismatched": 0, "unverified": 0},
        "visual_comparison": {"status": "match", "count": 1, "matched": 1, "mismatched": 0, "unverified": 0},
        "independent_verification": {"status": "pending", "run_id": None, "verdict": None},
    }
    old_verifier_id = uuid.uuid4()
    job.verifier_session_id = old_verifier_id
    fail_run_id = uuid.uuid4()
    fail_record = _capture(root, "verify-fail", str(fail_run_id), same_pixels=True, same_function=False)
    _write_report(root, str(fail_run_id), fail_record, verdict="failed")
    failed_run = _run(fail_run_id, old_verifier_id, job.id, "verify")
    db.latest_run_id = fail_run_id
    await _callback(db, failed_run)
    first_repair = next(x for x in db.added if isinstance(x, Run) and x.usage["reverse"]["role"] == "repair")
    first_failure_state = copy.deepcopy(job.result)
    rows_before_duplicate = len(db.added)
    await _callback(db, failed_run)
    duplicate_failure_unchanged = job.result == first_failure_state and len(db.added) == rows_before_duplicate

    repair_id = first_repair.id
    first_repair.status = RunStatus.succeeded
    _write_report(root, str(repair_id), _capture(root, "repair-good", str(repair_id), same_pixels=True, same_function=True))
    db.latest_run_id = repair_id
    new_verifier_session = await _callback(db, _run(repair_id, session.id, job.id, "repair"))
    new_verifier_runs = [x for x in db.added if isinstance(x, Run) and x.usage["reverse"]["role"] == "verify"]
    results["verifier_failure_repair"] = {
        "initial_verifier_status": first_failure_state["latest"]["independent_verification"]["status"],
        "repair_attempts": rt._metadata(job.result)["job"]["repair_attempts"],
        "old_verifier_cleared": first_failure_state["latest"]["verification_run_id"] == str(fail_run_id)
                                 and job.verifier_session_id != old_verifier_id,
        "duplicate_failure_noop": duplicate_failure_unchanged,
        "repair_run_created": first_repair is not None,
        "fresh_verifier_session_created": new_verifier_session is not None and new_verifier_session != old_verifier_id,
        "fresh_verifier_run_created": any(r.session_id == new_verifier_session for r in new_verifier_runs),
        "dispatch": "not executed; fake DB boundary and generation.current unentered",
    }
    # The positive control calls both real callback paths with fully matching
    # implementation and verifier evidence; the verifier's match alone is not
    # enough for the failure cases above.
    root = base / "both_roles_match"
    job, session, _impl, db = _job(root, max_repair=0)
    impl_run_id = uuid.uuid4()
    _write_report(root, str(impl_run_id), _capture(root, "impl-good", str(impl_run_id),
                  same_pixels=True, same_function=True))
    db.latest_run_id = impl_run_id
    await _callback(db, _run(impl_run_id, session.id, job.id, "implement"))
    verifier_session_id = job.verifier_session_id
    assert verifier_session_id is not None
    verify_run_id = uuid.uuid4()
    verifier_raw = _write_report(root, str(verify_run_id), _capture(root, "verify-good", str(verify_run_id),
                               same_pixels=True, same_function=True))
    db.latest_run_id = verify_run_id
    await _callback(db, _run(verify_run_id, verifier_session_id, job.id, "verify"))
    final = rt._metadata(job.result)["latest"]
    results["both_roles_match"] = {
        "expected_status": "done", "actual_status": job.status,
        "implementation_run_id": final["run_id"], "verifier_run_id": final["verification_run_id"],
        "implementation_direct": final.get("direct_comparison"),
        "implementation_visual": final.get("visual_comparison"),
        "verification_direct": final.get("verification_direct_comparison"),
        "verification_visual": final.get("verification_visual_comparison"),
        "independent_verification": final.get("independent_verification"),
        "raw_reports_separate": final.get("report") is not final.get("verification_report")
            and final.get("report", {}).get("comparisons") != final.get("verification_report", {}).get("comparisons"),
        "verifier_raw_report_ready": verifier_raw.get("ready"),
    }
    # Seed implementation results with non-web failures so the real verifier
    # callback must retain the CLI/API/no-comparison/source/check gates too.
    for label, target_type, direct, source_status, execution_status in (
        ("cli_implementation_mismatch", "cli", {"status": "mismatch", "count": 1,
         "matched": 0, "mismatched": 1, "unverified": 0}, "verified", "verified"),
        ("api_implementation_mismatch", "api", {"status": "mismatch", "count": 1,
         "matched": 0, "mismatched": 1, "unverified": 0}, "verified", "verified"),
        ("implementation_comparison_zero", "cli", {"status": "unverified", "count": 0,
         "matched": 0, "mismatched": 0, "unverified": 0}, "verified", "verified"),
        ("implementation_source_missing", "cli", {"status": "match", "count": 1,
         "matched": 1, "mismatched": 0, "unverified": 0}, "unverified", "verified"),
        ("implementation_execution_missing", "cli", {"status": "match", "count": 1,
         "matched": 1, "mismatched": 0, "unverified": 0}, "verified", "unverified"),
    ):
        case_root = base / label
        case_job, case_session, _case_impl, case_db = _job(case_root, max_repair=0)
        case_job.result["job"]["target_type"] = target_type
        case_job.verifier_session_id = uuid.uuid4()
        seed_run_id, verify_id = uuid.uuid4(), uuid.uuid4()
        case_job.result["latest"] = {
            "run_id": str(seed_run_id), "role": "implement",
            "source": {"status": source_status, "files": ["src/demo.py"] if source_status == "verified" else []},
            "execution": {"status": execution_status, "checks": []},
            "direct_comparison": direct,
            "visual_comparison": {"status": "not_required", "count": 0, "matched": 0,
                                  "mismatched": 0, "unverified": 0},
            "report": {"ready": True, "observations": [{"finding": "seed", "evidence": "seed"}],
                       "comparisons": [{"target_type": target_type, "result": "mismatch"}]
                         if direct["count"] else []},
            "comparisons": [{"target_type": target_type, "result": "mismatch"}] if direct["count"] else [],
            "independent_verification": {"status": "pending", "run_id": None, "verdict": None},
        }
        verifier_raw = _capture(case_root, f"{label}-verify", str(verify_id), same_pixels=True, same_function=True)
        _write_report(case_root, str(verify_id), verifier_raw)
        case_db.latest_run_id = verify_id
        await _callback(case_db, _run(verify_id, case_job.verifier_session_id, case_job.id, "verify"))
        case_latest = rt._metadata(case_job.result)["latest"]
        results[label] = {"target_type": target_type, "expected_status": "partial",
            "actual_status": case_job.status, "implementation_direct": case_latest.get("direct_comparison"),
            "implementation_source": case_latest.get("source"), "implementation_execution": case_latest.get("execution"),
            "verification_direct": case_latest.get("verification_direct_comparison"),
            "independent_verification": case_latest.get("independent_verification"),
            "raw_reports_separate": case_latest.get("report") is not case_latest.get("verification_report")}

    # Analyze mode intentionally completes without an independent verifier.
    root = base / "analyze_mode"
    job, session, _impl, db = _job(root, max_repair=0)
    job.result["job"]["mode"] = "analyze"
    run_id = uuid.uuid4()
    _write_report(root, str(run_id), None, mode="analyze", include_source=False, include_check=False)
    db.latest_run_id = run_id
    await _callback(db, _run(run_id, session.id, job.id, "implement"))
    results["analyze_mode"] = {"expected_status": "done", "actual_status": job.status,
        "independent_verification": rt._metadata(job.result)["latest"].get("independent_verification"),
        "history": rt._metadata(job.result).get("history", [])}
    return results


def main() -> None:
    head = _assert_baseline()
    with tempfile.TemporaryDirectory(prefix="reverse-webcore-audit-") as tmp:
        root = Path(tmp)
        captures: dict[str, Any] = {}

        # Functional match / visual mismatch. Tampered declarations must not win.
        feature_match = _capture(root, "feature-match-visual-mismatch", "run-main", same_pixels=False, same_function=True)
        feature_match["result"] = "match"
        feature_match["difference"] = None
        feature_match["evidence"][0].update(functional_result="mismatch", visual_result="match",
                                             integrity={"status": "verified"}, provenance={"status": "verified"})
        captures["feature_match_visual_mismatch"] = _summary(_verify(root, feature_match, "run-main"))

        # Functional mismatch / visual match, same hashed PNG bytes on both sides.
        feature_mismatch = _capture(root, "feature-mismatch-visual-match", "run-main", same_pixels=True, same_function=False)
        captures["feature_mismatch_visual_match"] = _summary(_verify(root, feature_mismatch, "run-main"))

        missing = _capture(root, "missing-replica", "run-main", same_pixels=True,
                           same_function=True, missing_replica=True)
        captures["missing_observation"] = _summary(_verify(root, missing, "run-main"))

        wrong_run = _capture(root, "wrong-run", "run-written", same_pixels=True, same_function=True)
        captures["different_run"] = _summary(_verify(root, wrong_run, "run-expected"))

        tampered = _capture(root, "tampered-png", "run-main", same_pixels=True, same_function=True)
        raw_ref = tampered["evidence"][0]["capture"]
        raw_capture = json.loads((root / raw_ref["path"]).read_text(encoding="utf-8"))
        png_path = root / raw_capture["original"]["screenshot"]["path"]
        png_path.write_bytes(png_path.read_bytes() + b"tamper")
        captures["tampered_png"] = _summary(_verify(root, tampered, "run-main"))

        # Reopen a second intact comparison to show verified bytes do not authenticate origin.
        intact = _capture(root, "integrity-provenance", "run-main", same_pixels=True, same_function=True)
        captures["integrity_provenance_split"] = _summary(_verify(root, intact, "run-main"))

        # Actual report verifier: target configuration controls whether missing web visuals are required;
        # a stored web comparison remains separately summarized even if the job type was later changed.
        auto_web = _capture(root, "auto-web-recommendation", "run-main", same_pixels=False, same_function=True)
        web_summary = rt.validate_report(root, {"comparisons": [auto_web]}, mode="analyze_reconstruct",
                                         expected_run_id="run-main", target_type="web")
        changed_to_cli_summary = rt.validate_report(root, {"comparisons": [auto_web]}, mode="analyze_reconstruct",
                                                   expected_run_id="run-main", target_type="cli")
        no_web_compare_web = rt.validate_report(root, {"comparisons": []}, mode="analyze_reconstruct",
                                               expected_run_id="run-main", target_type="web")
        no_web_compare_cli = rt.validate_report(root, {"comparisons": []}, mode="analyze_reconstruct",
                                               expected_run_id="run-main", target_type="cli")
        captures["target_type_policy"] = {
            "auto_web_recommendation_source": "start() regex chooses web and stores inferred target_type in job config (static code check)",
            "web_config_with_actual_web_record": {"direct": web_summary["direct_comparison"], "visual": web_summary["visual_comparison"]},
            "changed_cli_with_actual_web_record": {"direct": changed_to_cli_summary["direct_comparison"], "visual": changed_to_cli_summary["visual_comparison"]},
            "web_config_missing_web_record": no_web_compare_web["visual_comparison"],
            "cli_config_missing_web_record": no_web_compare_cli["visual_comparison"],
        }
        captures["callbacks"] = asyncio.run(_callback_probes(root / "callbacks"))

        output = {"baseline_revision": BASELINE_SHA, "audit_head": head,
                          "probe_kind": "independent synthetic files + real callbacks with fake DB boundary",
                          "captures": captures,
                          "database_api_dispatch": "unverified: fake DB, no API/DB or generation dispatch",
                          "browser_provenance": "unverified: writer-authored synthetic observations"}
        destination = Path(__file__).with_name("probe-webcore-output.json")
        destination.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"baseline_revision": BASELINE_SHA, "audit_head": head,
                          "result_file": str(destination),
                          "callbacks": {name: {"job_status": value["actual"]["job_status"],
                              "independent_verification": value["actual"]["independent_verification"],
                              "duplicate_callback": value["duplicate_callback"]}
                              for name, value in captures["callbacks"].items() if "actual" in value},
                          "callback_failures": [name for name, value in captures["callbacks"].items()
                                                if "finding" in value and value["finding"] == "FAIL"]},
                         ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
