import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ttakkari.api import research
from ttakkari.db import get_db
from ttakkari.models import utcnow
from ttakkari.research import ownership, resources
from ttakkari.research.models import Run
from ttakkari.security import auth


def run_with_owner(owner):
    return SimpleNamespace(
        id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        experiment_id=uuid.uuid4(),
        adapter_id="llm_inference",
        environment_snapshot=({ownership.OWNER_KEY: owner} if owner is not None else {}),
    )


def test_execution_owner_receipt_reports_held_and_released_lock(monkeypatch):
    owner = {"contract": 1, "token": str(uuid.uuid4()), "generation_id": str(uuid.uuid4()),
             "accepted_at": "2026-10-30T00:00:00+00:00"}
    row = run_with_owner(owner)
    probes = []

    class Handle:
        def close(self):
            probes.append("closed")

    monkeypatch.setattr(ownership, "probe", lambda token: probes.append(token) or None)
    held = ownership.execution_owner_receipt(row)
    assert held["status"] == "owned"
    assert held["observed_at"]
    assert held["scope"] == {
        "study_id": str(row.study_id), "experiment_id": str(row.experiment_id),
        "run_id": str(row.id), "adapter_id": "llm_inference",
    }
    assert held["owner"] == owner
    assert held["evidence"] == "os_lock_observed"

    monkeypatch.setattr(ownership, "probe", lambda token: Handle())
    released = ownership.execution_owner_receipt(row)
    assert released["status"] == "released"
    assert released["evidence"] == "os_lock_available"
    assert probes[-1] == "closed"

    class BadHandle:
        def close(self):
            raise OSError("close failed")

    monkeypatch.setattr(ownership, "probe", lambda token: BadHandle())
    unverified = ownership.execution_owner_receipt(row)
    assert unverified["status"] == "unverified"
    assert unverified["evidence"] == "lock_probe_close_failed"


def test_execution_owner_receipt_keeps_legacy_and_invalid_owner_unverified(monkeypatch):
    monkeypatch.setattr(ownership, "probe", lambda token: None)
    legacy = ownership.execution_owner_receipt(run_with_owner(None))
    assert legacy["status"] == "unrecorded"
    assert legacy["evidence"] == "owner_checkpoint_missing"

    invalid = ownership.execution_owner_receipt(run_with_owner({"contract": 1, "token": "bad"}))
    assert invalid["status"] == "unverified"
    assert invalid["evidence"] == "owner_checkpoint_invalid"


def test_execution_owner_receipt_keeps_lock_probe_errors_unverified(monkeypatch):
    row = run_with_owner({"contract": 1, "token": str(uuid.uuid4())})

    def fail(token):
        raise OSError("lock probe denied")

    monkeypatch.setattr(ownership, "probe", fail)
    receipt = ownership.execution_owner_receipt(row)
    assert receipt["status"] == "unverified"
    assert receipt["evidence"] == "lock_probe_failed"


@pytest.mark.parametrize(
    "snapshot",
    [[], ["not", "an", "object"], "", "corrupt", 0, 7, 1.5, True, False],
)
def test_execution_owner_receipt_marks_non_object_snapshots_unverified(snapshot, monkeypatch):
    monkeypatch.setattr(
        ownership, "probe", lambda token: pytest.fail("invalid snapshot must not probe a lock")
    )
    row = run_with_owner(None)
    row.environment_snapshot = snapshot

    receipt = ownership.execution_owner_receipt(row)

    assert receipt["status"] == "unverified"
    assert receipt["evidence"] == "environment_snapshot_invalid"
    assert receipt["owner"] is None


def test_execution_owner_receipt_keeps_null_and_missing_snapshots_unrecorded(monkeypatch):
    monkeypatch.setattr(ownership, "probe", lambda token: None)
    for snapshot in (None, {}):
        row = run_with_owner(None)
        row.environment_snapshot = snapshot
        receipt = ownership.execution_owner_receipt(row)
        assert receipt["status"] == "unrecorded"
        assert receipt["evidence"] == "owner_checkpoint_missing"


@pytest.mark.parametrize(
    "snapshot",
    [[], ["not", "an", "object"], "", "corrupt", 0, 7, 1.5, True, False],
)
def test_list_owner_observer_keeps_malformed_active_candidates_unverified(snapshot, monkeypatch):
    row = run_with_owner(None)
    row.status = "queued"
    row.environment_snapshot = snapshot
    monkeypatch.setattr(
        resources, "owner_token", lambda value: pytest.fail("malformed snapshot must not be parsed")
    )

    locks, observations = resources.observe_execution_owners([row], truncated=False)

    assert locks == []
    assert observations["unverified_run_ids"] == [str(row.id)]


@pytest.mark.parametrize("owner", [[], "bad", "", 0, 9, 1.5, True, False])
def test_list_owner_observer_keeps_malformed_owner_checkpoints_unverified(owner, monkeypatch):
    row = run_with_owner(owner)
    row.status = "running"
    monkeypatch.setattr(
        resources, "probe", lambda token: pytest.fail("malformed checkpoint must not probe a lock")
    )

    locks, observations = resources.observe_execution_owners([row], truncated=False)

    assert locks == []
    assert observations["unverified_run_ids"] == [str(row.id)]


@pytest.mark.parametrize("owner", [[], ["bad"], "bad", "", 0, 9, 1.5, True, False])
def test_execution_owner_receipt_never_promotes_non_object_owner_checkpoints(owner, monkeypatch):
    monkeypatch.setattr(
        ownership, "probe", lambda token: pytest.fail("invalid checkpoint must not probe a lock")
    )
    row = run_with_owner(owner)

    receipt = ownership.execution_owner_receipt(row)

    assert receipt["owner"] == owner
    assert receipt["status"] == "unverified"
    assert receipt["evidence"] == "owner_checkpoint_invalid"


def test_authenticated_run_detail_preserves_malformed_snapshot_and_receipt(monkeypatch):
    secret = "isolated-test-secret-key-32-bytes-long"
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(jwt_secret=secret))

    row = Run(
        id=uuid.uuid4(), study_id=uuid.uuid4(), experiment_id=uuid.uuid4(),
        configuration_id=uuid.uuid4(), adapter_id="llm_inference", status="succeeded",
        environment_snapshot=["not", "an", "object"], raw_output={}, created_at=utcnow(),
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class FakeDb:
        async def get(self, model, ident):
            return row if ident == row.id else None

        async def execute(self, statement):
            return Result()

    async def get_fake_db():
        yield FakeDb()

    app = FastAPI()
    app.include_router(research.router)
    app.dependency_overrides[get_db] = get_fake_db
    payload = {
        "iss": "ttakkari", "aud": "api", "sub": "owner",
        "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=2),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/api/research/runs/{row.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["environment_snapshot"] == ["not", "an", "object"]
    assert body["execution_owner_receipt"]["status"] == "unverified"
    assert body["execution_owner_receipt"]["evidence"] == "environment_snapshot_invalid"
    print(
        "HTTP detail malformed snapshot:", response.status_code,
        body["execution_owner_receipt"]["status"],
        body["execution_owner_receipt"]["evidence"],
        "snapshot_preserved=", body["environment_snapshot"],
    )


def test_authenticated_run_list_keeps_global_malformed_candidate_unverified(monkeypatch):
    secret = "isolated-test-secret-key-32-bytes-long"
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(jwt_secret=secret))

    visible = Run(
        id=uuid.uuid4(), study_id=uuid.uuid4(), experiment_id=uuid.uuid4(),
        configuration_id=uuid.uuid4(), adapter_id="llm_inference", status="succeeded",
        environment_snapshot=None, raw_output={}, created_at=utcnow(),
    )
    malformed_candidate = Run(
        id=uuid.uuid4(), study_id=uuid.uuid4(), experiment_id=uuid.uuid4(),
        configuration_id=uuid.uuid4(), adapter_id="llm_inference", status="queued",
        environment_snapshot=["malformed", "global", "candidate"], raw_output={}, created_at=utcnow(),
    )

    class Result:
        def all(self):
            return [(visible, "Experiment", "Configuration", "Visible study")]

    class FakeDb:
        async def execute(self, statement):
            return Result()

        async def scalars(self, statement):
            return [malformed_candidate]

    async def get_fake_db():
        yield FakeDb()

    async def readiness(*args, **kwargs):
        return {}, None

    monkeypatch.setattr(research.svc, "adapter_readiness", readiness)
    monkeypatch.setattr(
        resources, "probe", lambda token: pytest.fail("malformed global candidate must not probe")
    )

    app = FastAPI()
    app.include_router(research.router)
    app.dependency_overrides[get_db] = get_fake_db
    payload = {
        "iss": "ttakkari", "aud": "api", "sub": "owner",
        "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=2),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")

    with TestClient(app, raise_server_exceptions=False) as client:
        unauthenticated = client.get("/api/research/runs")
        response = client.get(
            f"/api/research/runs?study_id={visible.study_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert [run["id"] for run in body["runs"]] == [str(visible.id)]
    assert body["runs"][0]["execution_owner_receipt"]["status"] == "unrecorded"
    assert body["lock_observations"]["unverified_run_ids"] == [str(malformed_candidate.id)]
    print(
        "HTTP filtered list with global malformed candidate:", response.status_code,
        "runs=", [run["id"] for run in body["runs"]],
        "unverified_run_ids=", body["lock_observations"]["unverified_run_ids"],
        "snapshot_preserved=", malformed_candidate.environment_snapshot,
    )
