import asyncio
import datetime
import uuid
from types import SimpleNamespace

from ttakkari.api import collection_missions
from ttakkari.api.collection_missions import (
    _classified_error,
    _diagnostic_view,
    _policy_diagnostic,
    _safe_url,
    _source_diagnostics,
)
from ttakkari.collector.control_models import CollectorSource
from ttakkari.collector.library import LIBRARY_SCOPE
from ttakkari.collector.policy import SourcePolicySpec
from ttakkari.collector_models import CollectorHostState, CollectorSourcePolicy


class _Result:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _ReadOnlyDb:
    def __init__(self, sources, policies, host_states):
        self.sources = sources
        self.policies = policies
        self.host_states = host_states

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is CollectorSource:
            return self.sources
        if entity is CollectorHostState:
            return self.host_states
        raise AssertionError(f"unexpected read: {entity}")

    async def execute(self, statement):
        params = statement.compile().params
        source_key = next(value for key, value in params.items() if "source_key" in key)
        return _Result(self.policies.get(source_key))


def test_diagnostic_classification_keeps_exception_contract_without_raw_message():
    assert _classified_error({
        "code": "attempts_exhausted", "type": "RuntimeError", "cause": "invalid_envelope",
        "error_type": "CollectorContractError",
        "error_code": "invalid_envelope", "retryable": False, "attempts": 3,
        "message": "password=should-not-leak",
    }) == {
        "code": "attempts_exhausted", "type": "RuntimeError", "cause": "invalid_envelope",
        "error_type": "CollectorContractError",
        "error_code": "invalid_envelope", "retryable": False, "attempts": 3,
    }


def test_diagnostic_classification_drops_unrecognized_free_text_fields():
    raw = "synthetic private exception detail"
    for key in ("code", "type", "cause", "cause_code", "error_type", "error_code"):
        assert key not in _classified_error({key: raw})
    assert _classified_error({"cause": {"message": raw}, "message": raw}) == {}


def test_diagnostic_classification_keeps_stable_collector_codes():
    for code in ("unbound_mission_preparation", "fetch_deferred"):
        assert _classified_error({"code": code}) == {"code": code}


def test_policy_diagnostic_is_bounded_to_rights_and_strips_query_credentials():
    assert _policy_diagnostic({
        "allowed_hosts": ["docs.test"], "allowed_path_prefixes": ["/guide"],
        "excluded_path_prefixes": ["/private"],
        "permissions": {"store": "allowed", "knowledge": "allowed", "train": "denied", "export": "denied"},
        "license_id": "CC-BY-4.0", "policy_url": "https://docs.test/license?token=secret",
        "robots_required": True, "basis": "Internal reviewer notes", "unexpected_secret": "no",
    }) == {
        "allowed_hosts": ["docs.test"], "allowed_path_prefixes": ["/guide"],
        "excluded_path_prefixes": ["/private"],
        "permissions": {"store": "allowed", "knowledge": "allowed", "train": "denied", "export": "denied"},
        "license_id": "CC-BY-4.0", "policy_url": "https://docs.test/license", "robots_required": True,
    }
    assert _safe_url("https://user:password@docs.test/guide?token=secret#private") == "https://docs.test/guide"


def test_source_diagnostic_uses_owner_active_policy_and_reports_robots_cache_without_body():
    source = CollectorSource(
        tenant_id="owner", scope=LIBRARY_SCOPE, source_key="approved-docs", title="Docs",
        request={"seeds": ["https://docs.test/"]}, enabled=True,
    )
    spec = SourcePolicySpec.from_dict({
        "allowed_hosts": ["docs.test"], "schemes": ["https"], "ports": [443],
        "permissions": {"store": "allowed", "knowledge": "allowed", "train": "denied", "export": "denied"},
        "license_id": "CC-BY-4.0", "policy_url": "https://docs.test/license",
        "basis": "Terms section 4", "checked_at": "2026-10-29T00:00:00Z",
        "allowed_path_prefixes": ["/guide"], "excluded_path_prefixes": ["/guide/private"],
    })
    policy_row = CollectorSourcePolicy(
        tenant_id="owner", scope=LIBRARY_SCOPE, source_key=source.source_key,
        version=1, spec=spec.to_dict(), spec_hash=spec.spec_hash(),
    )
    now = datetime.datetime.now(datetime.UTC)
    host_state = CollectorHostState(
        origin="https://docs.test", robots_status="ok", robots_body="private disallow rules",
        robots_fetched_at=now - datetime.timedelta(hours=1), robots_expires_at=now - datetime.timedelta(minutes=1),
    )
    mission = SimpleNamespace(
        id=uuid.uuid4(), tenant_id="owner",
        plan={"sources": [
            {"url": "https://docs.test/guide/start?token=secret"},
            {"url": "https://docs.test/guide/private?token=secret"},
        ]},
    )
    db = _ReadOnlyDb([source], {source.source_key: policy_row}, [host_state])

    result = asyncio.run(_source_diagnostics(db, mission))

    assert result[0]["url"] == "https://docs.test/guide/start"
    assert result[0]["policy"]["matched"] is True
    assert result[0]["policy"]["source_key_hash"]
    assert result[0]["policy"]["allowed_path_prefixes"] == ["/guide"]
    assert result[0]["robots"]["status"] == "ok" and result[0]["robots"]["fresh"] is False
    assert result[1]["policy"]["matched"] is False
    assert result[1]["policy"]["candidates"][0]["reason"] == "path_excluded_by_policy"
    assert result[1]["policy"]["candidates"][0]["excluded_path_prefixes"] == ["/guide/private"]
    assert "secret" not in str(result) and "private disallow rules" not in str(result)


def test_mission_diagnostic_reports_current_run_active_jobs_and_safe_final_classification(monkeypatch):
    async def fake_view(_db, _mission):
        return {
            "id": "mission", "title": "Design collection", "status": "failed", "run_seq": 4,
            "counts": {"acquired": 0, "imported": 0}, "research": {},
            "knowledge_processing": {"storage": "pending"},
            "jobs": [
                {"job_id": "failed-job", "run_seq": 4, "stage": "mission_prepare", "status": "failed",
                 "error": {"code": "attempts_exhausted", "cause": "invalid_envelope",
                           "error_type": "CollectorContractError", "error_code": "invalid_envelope",
                           "retryable": False, "message": "secret=do-not-return"}},
                {"job_id": "queued-job", "run_seq": 4, "stage": "website", "status": "queued", "error": None},
                {"job_id": "old-run", "run_seq": 3, "stage": "website", "status": "running", "error": None},
            ],
        }

    class _NoPolicyDb:
        async def scalars(self, statement):
            assert statement.column_descriptions[0]["entity"] is CollectorSource
            return []

    mission = SimpleNamespace(
        id="mission", tenant_id="owner", status="failed", plan={"sources": [], "research": {}}, gaps=[],
    )
    monkeypatch.setattr(collection_missions.missions, "view", fake_view)

    result = asyncio.run(_diagnostic_view(_NoPolicyDb(), mission))

    assert result["reservations"]["active_count"] == 1
    assert result["reservations"]["jobs"] == [
        {"job_id": "failed-job", "stage": "mission_prepare", "status": "failed", "active": False},
        {"job_id": "queued-job", "stage": "website", "status": "queued", "active": True},
    ]
    assert result["jobs"][0]["error"]["error_type"] == "CollectorContractError"
    assert result["jobs"][0]["error"]["cause"] == "invalid_envelope"
    assert "do-not-return" not in str(result)
