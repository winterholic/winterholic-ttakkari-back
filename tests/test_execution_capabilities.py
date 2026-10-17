"""ADR 0037: 엔진별 실행 환경 능력(프로브 실측 저장·노출·프롬프트 주입)."""
from __future__ import annotations

import copy
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from helpers import run_to_end
from test_orchestrator import _orch, _session

from ttakkari.models import SessionKind
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import capabilities, env_probe


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(capabilities, "store_path", lambda: tmp_path / "caps" / "execution-capabilities.json")


def _report(**overrides) -> dict:
    items = {name: {"status": "available", "detail": "ok"} for name in env_probe.ITEMS}
    items.update(overrides)
    return {"probe_version": env_probe.PROBE_VERSION, "measured_epoch": time.time(), "platform": "darwin",
            "items": items}


# ---- 프로브

def test_probe_reports_every_item_with_a_known_status():
    out = env_probe.run_probe()
    assert out["probe_version"] == env_probe.PROBE_VERSION and out["measured_epoch"] <= time.time() + 1
    assert set(out["items"]) == set(env_probe.ITEMS)
    assert all(v["status"] in capabilities.STATUSES for v in out["items"].values())
    assert out["items"]["spawn_process"]["status"] == "available"
    assert out["items"]["network_loopback"]["status"] == "available"


def test_probe_metal_nil_is_blocked_not_unavailable_host(monkeypatch):
    class Lib:
        def __getattr__(self, name):
            fn = lambda: None
            return fn
    monkeypatch.setattr(env_probe.sys, "platform", "darwin")
    monkeypatch.setattr(env_probe.ctypes, "CDLL", lambda *_: Lib())
    result = env_probe.probe_gpu_metal()
    assert result["status"] == "blocked" and "nil" in result["detail"]


def test_probe_leaves_untried_items_unknown(tmp_path):
    result = env_probe.probe_llama_list_devices(str(tmp_path / "missing-llama-server"))
    assert result["status"] == "unknown"


def test_probe_llama_devices_parse_metal_and_missing_metal(tmp_path):
    def fake(body: str) -> str:
        path = tmp_path / f"llama-{abs(hash(body))}"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return str(path)
    ok = env_probe.probe_llama_list_devices(fake("echo 'MTL0: Apple M5 Max (53084 MiB, 53083 MiB free)'"))
    assert ok["status"] == "available" and "Apple M5 Max" in ok["detail"]
    none = env_probe.probe_llama_list_devices(fake("echo 'Available devices:'; echo 'CPU: x'"))
    assert none["status"] == "blocked"


# ---- 저장

def test_unmeasured_engines_are_unknown_with_no_measurement_time():
    snap = capabilities.snapshot()
    assert set(snap) == set(capabilities.ENGINES)
    codex = snap["codex"]
    assert codex["measured_at"] is None and codex["stale"] is True and codex["source"] is None
    assert all(v == {"status": "unknown", "detail": "not measured"} for v in codex["items"].values())
    assert codex["known_limitations"] == []


def test_record_stores_measurement_time_source_and_blocked_limits():
    blocked = {"gpu_metal": {"status": "blocked", "detail": "MTLCreateSystemDefaultDevice returned nil"}}
    view = capabilities.record("codex", _report(**blocked), source="session_probe", session_id="s1", run_id="r1")
    assert view["source"] == "session_probe" and view["measured_at"].endswith("+09:00")
    assert view["items"]["gpu_metal"]["status"] == "blocked"
    assert view["known_limitations"] == ["gpu_metal: MTLCreateSystemDefaultDevice returned nil"]
    assert capabilities.snapshot()["codex"]["known_limitations"] == view["known_limitations"]
    assert capabilities.snapshot()["claude"]["measured_at"] is None  # 다른 엔진은 건드리지 않는다


def test_items_missing_from_a_report_stay_unknown():
    report = _report()
    del report["items"]["network_outbound"]
    view = capabilities.record("muse", report, source="session_probe")
    assert view["items"]["network_outbound"]["status"] == "unknown"


@pytest.mark.parametrize("mutate,message", [
    (lambda r: r.update(probe_version=99), "probe_version"),
    (lambda r: r["items"].update(rm_rf={"status": "available"}), "unknown probe item"),
    (lambda r: r["items"]["gpu_metal"].update(status="maybe"), "status"),
    (lambda r: r.update(measured_epoch=time.time() - 3 * 86400), "24 hours"),
    (lambda r: r.update(measured_epoch=time.time() + 3600), "24 hours"),
    (lambda r: r.pop("measured_epoch"), "measured_epoch"),
    (lambda r: r.update(items={}), "items"),
])
def test_record_rejects_hand_written_or_malformed_reports(mutate, message):
    report = copy.deepcopy(_report())
    mutate(report)
    with pytest.raises(capabilities.ReportError, match=message):
        capabilities.record("codex", report, source="session_probe")
    assert capabilities.snapshot()["codex"]["measured_at"] is None


def test_unknown_engine_is_rejected():
    with pytest.raises(capabilities.ReportError, match="unknown engine"):
        capabilities.record("gemini-web", _report(), source="session_probe")


def test_older_report_does_not_overwrite_a_newer_measurement():
    new = _report(gpu_metal={"status": "available", "detail": "new"})
    old = _report(gpu_metal={"status": "blocked", "detail": "old"})
    old["measured_epoch"] -= 600
    capabilities.record("claude", new, source="session_probe")
    capabilities.record("claude", old, source="session_probe")
    assert capabilities.snapshot()["claude"]["items"]["gpu_metal"]["detail"] == "new"


def test_detail_is_truncated_and_control_characters_removed():
    view = capabilities.record("claude", _report(gpu_metal={"status": "blocked", "detail": "x\x00\x1b" + "y" * 500}),
                               source="session_probe")
    detail = view["items"]["gpu_metal"]["detail"]
    assert len(detail) <= capabilities.MAX_DETAIL and "\x00" not in detail and "\x1b" not in detail


def test_stale_flag_follows_measurement_age():
    capabilities.record("claude", _report(), source="session_probe")
    fresh = capabilities.view_engine("claude")
    old = capabilities.view_engine("claude", now=time.time() + capabilities.STALE_SECONDS + 60)
    assert fresh["stale"] is False and old["stale"] is True


# ---- 프롬프트 주입

def test_prompt_note_carries_the_scope_principle_and_per_engine_limits():
    capabilities.record("codex", _report(
        gpu_metal={"status": "blocked", "detail": "nil"}, process_list={"status": "blocked", "detail": "EPERM"}),
        source="session_probe")
    capabilities.record("claude", _report(), source="session_probe")
    note = capabilities.prompt_note()
    assert "holds for that environment only" in note and "Never generalize it to \"the host cannot\"" in note
    assert "- codex (measured " in note and "blocked gpu_metal, process_list" in note
    assert "- claude (measured " in note and "nothing blocked" in note
    assert "- muse: not measured (all unknown)" in note
    assert "host_gpu_start" in note and "report_execution_environment" in note
    assert "measured on the host GPU path" in note
    assert len(note) < 2500  # 모든 세션에 들어가므로 짧게 유지한다


async def test_system_note_includes_capabilities_for_every_session_kind(monkeypatch):
    from importlib import import_module
    m = import_module("ttakkari.runs.manager")
    monkeypatch.setattr(m, "pinned_brief", AsyncMock(return_value=""))
    monkeypatch.setattr(m.instructions_mod, "read", lambda role: "")
    capabilities.record("codex", _report(gpu_metal={"status": "blocked", "detail": "nil"}), source="session_probe")
    for kind, parent in ((SessionKind.orchestrator, "desk"), (SessionKind.orchestrator, None), (SessionKind.worker, None)):
        note = await m._system_note(None, Path("/tmp/outbox"), SimpleNamespace(kind=kind, parent_id=parent))
        assert "[Execution environment capabilities" in note, kind
        assert "- codex (measured " in note and "blocked gpu_metal" in note, kind


# ---- API·노출

async def test_report_is_filed_under_the_engine_of_the_sessions_run(client, auth, make_dir):
    session = await _session(client, auth, make_dir)
    h = _orch(issue_token(session["id"]))
    r = await client.post("/api/agent/execution-capabilities/report", json={"report": _report()}, headers=h)
    assert r.status_code == 409  # Run 이 없으면 엔진을 알 수 없다
    await run_to_end(client, auth, session["id"], "hello")
    r = await client.post("/api/agent/execution-capabilities/report", json={"report": _report(
        gpu_metal={"status": "blocked", "detail": "nil"})}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "claude" and r.json()["source"] == "session_probe"
    snap = (await client.get("/api/agent/execution-capabilities", headers=h)).json()
    assert snap["engines"]["claude"]["items"]["gpu_metal"]["status"] == "blocked"
    assert snap["engines"]["codex"]["measured_at"] is None
    assert snap["probe_script"].endswith("env_probe.py")
    # 호스트 기준선은 백엔드가 직접 잰다.
    assert snap["engines"]["host"]["source"] == "backend_probe" and snap["engines"]["host"]["measured_at"]


async def test_report_rejects_forged_engine_and_bad_values(client, auth, make_dir):
    session = await _session(client, auth, make_dir)
    await run_to_end(client, auth, session["id"], "hello")
    h = _orch(issue_token(session["id"]))
    bad = _report()
    bad["items"]["gpu_metal"]["status"] = "yes"
    assert (await client.post("/api/agent/execution-capabilities/report", json={"report": bad}, headers=h)).status_code == 422
    # 보고 본문으로 엔진을 지정할 수 없다.
    forged = {"report": _report(), "engine": "host"}
    assert (await client.post("/api/agent/execution-capabilities/report", json=forged, headers=h)).status_code == 422
    assert (await client.post("/api/agent/execution-capabilities/report", json={"report": _report()})).status_code == 401


async def test_account_usage_adds_execution_capabilities_without_changing_existing_fields(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    capabilities.record("codex", _report(gpu_metal={"status": "blocked", "detail": "nil"}), source="session_probe")
    body = (await client.get("/api/orchestrator/accounts/usage", headers=h)).json()
    for key in ("measured_at", "priority_override_until", "worker_routing", "muse_auto_routing", "note", "accounts"):
        assert key in body, key
    assert isinstance(body["worker_routing"], dict) and "general" in body["worker_routing"]
    caps = body["execution_capabilities"]
    assert set(caps) == set(capabilities.ENGINES)
    assert caps["codex"]["known_limitations"] == ["gpu_metal: nil"] and caps["codex"]["measured_at"]
    assert caps["muse"]["measured_at"] is None


def test_worker_routing_receipt_carries_the_selected_engines_limits():
    from ttakkari.runs import profiles, worker_routing
    capabilities.record("claude", _report(gpu_metal={"status": "blocked", "detail": "nil"}), source="session_probe")
    import uuid
    p = profiles.Profile(uuid.uuid4(), "claude", "claude", model="sonnet")
    out = worker_routing.receipt("general", p, [p], [], {}, trigger="test")
    env = out["execution_environment"]
    assert env["engine"] == "claude" and env["known_limitations"] == ["gpu_metal: nil"]
    assert out["policy"] == worker_routing.POLICY_VERSION and "selected" in out  # 기존 필드 유지
