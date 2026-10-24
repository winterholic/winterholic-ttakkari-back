"""Regressions from the independent A/B/C reviews; no database access."""

import importlib.machinery
import json
import sys
import types
from pathlib import Path

from scripts import platform_v2_inventory as inventory
from scripts import platform_v2_test_audit as frontend


def test_loader_paths_do_not_prove_executed_source(monkeypatch):
    inventory.run_audit()
    path = inventory.AuditContext().src_dir / "ttakkari/config.py"
    fake = types.ModuleType("ttakkari.config")
    fake.__file__ = str(path)
    fake.__loader__ = types.SimpleNamespace(path=str(path))
    fake.__spec__ = importlib.machinery.ModuleSpec(
        "ttakkari.config", fake.__loader__, origin=str(path)
    )
    fake.Settings = types.SimpleNamespace(model_fields={"alpha": object()})
    monkeypatch.setitem(sys.modules, "ttakkari.config", fake)
    binding = inventory.evaluate_import_binding(inventory.AuditContext())
    assert binding["categories"]["configs"]["state"] in {"unknown", "unbound"}


def test_partial_registry_cannot_bind_api(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([{"id": "core.auth_session"}]))
    result = frontend.audit_frontend_api(
        client_source_text='export const me = () => fetch("/api/auth/me");',
        backend_inventory_path=registry,
    )
    assert result["registry_status"] == "incomplete"
    assert result["coverage_status"] == "incomplete"
    assert result["endpoints"], result
    assert all(e["backend_module_id"] is None for e in result["endpoints"])


def test_missing_candidate_registry_never_uses_shared_checkout(tmp_path):
    expected = tmp_path / "docs/design/platform-v2/backend-module-inventory.json"
    assert frontend.backend_registry_path(tmp_path) == expected
    assert not expected.exists()


def test_truncated_full_rows_are_incomplete(tmp_path):
    original = (
        Path(frontend.__file__).parents[1]
        / "docs/design/platform-v2/backend-module-inventory.json"
    )
    rows = json.loads(original.read_text())
    registry = tmp_path / "truncated.json"
    registry.write_text(json.dumps(rows[:1]))
    result = frontend.audit_frontend_api(
        client_source_text='export const me = () => fetch("/api/auth/me");',
        backend_inventory_path=registry,
    )
    assert result["coverage_status"] == "incomplete"
    assert result["endpoints"][0]["backend_module_id"] is None


def test_invalid_typescript_is_an_unresolved_input():
    registry = (
        Path(frontend.__file__).parents[1]
        / "docs/design/platform-v2/backend-module-inventory.json"
    )
    result = frontend.audit_frontend_api(
        client_source_text="export const broken = ( => {",
        backend_inventory_path=registry,
    )
    assert result["coverage_status"] == "incomplete"
    assert result["unresolved_ledger"]


def test_invalid_route_source_reports_errors_without_aborting():
    result = frontend.audit_frontend_routes(source_text="export const broken = ( => {")
    assert result["metrics"]["coverage"] == "incomplete"
    assert result["unresolved_ledger"]


def test_complete_registry_and_supported_call_can_be_complete():
    registry = (
        Path(frontend.__file__).parents[1]
        / "docs/design/platform-v2/backend-module-inventory.json"
    )
    result = frontend.audit_frontend_api(
        client_source_text='export const me = () => fetch("/api/auth/me");',
        backend_inventory_path=registry,
    )
    assert result["registry_status"] == "valid"
    assert result["coverage_status"] == "complete"
    assert result["endpoints"][0]["backend_module_id"] == "core.auth_session"


def test_audit_input_manifest_tracks_frontend_bytes(tmp_path):
    front = tmp_path / "front"
    (front / "src").mkdir(parents=True)
    source = front / "src/App.tsx"
    source.write_text("source A")
    before = frontend.capture_audit_inputs(tmp_path / "back", front)
    source.write_text("source B")
    assert before != frontend.capture_audit_inputs(tmp_path / "back", front)
