import pytest


@pytest.mark.parametrize("owner_dead,groups,expected", [
    (None, [], "unknown_owner"),
    (True, [], "missing_process_group_evidence"),
    (False, [("absent", True)], "owner_alive"),
    (True, [("unknown", True)], "unknown_process_group"),
    (True, [("present", True)], "process_group_live"),
    (True, [("absent", True)], "safe_to_release"),
])
def test_reconcile_disposition_fails_closed_on_missing_or_unknown_evidence(owner_dead, groups, expected):
    from ttakkari.executor_reconcile import disposition

    assert disposition(owner_dead=owner_dead, groups=groups) == expected


def test_reconcile_command_exposes_dry_run_and_apply_without_database_access():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-m", "ttakkari.cli", "executor-reconcile", "--help"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout
    assert "--apply" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("current_state", ["token_changed", "ledger_changed"])
async def test_reconcile_stale_snapshot_never_signals_process_group(monkeypatch, current_state):
    import uuid
    from types import SimpleNamespace

    from ttakkari import executor_reconcile as reconcile

    run_id, owner_id, old_token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    group = SimpleNamespace(generation_id=owner_id, lease_token=old_token, attempt=3,
                            pid=111, process_started_at=10.0, pgid=111)
    entry = {"run_id": str(run_id), "owner_id": str(owner_id), "owner_pid": 222,
             "owner_process_started_at": 20.0, "owner_dead": True,
             "_lease_token": old_token, "_group_rows": [group],
             "disposition": "process_group_live"}
    lease = SimpleNamespace(generation_id=owner_id,
        token=uuid.uuid4() if current_state == "token_changed" else old_token, pid=None)
    owner = SimpleNamespace(id=owner_id, pid=222, process_started_at=20.0)
    signal_calls = []

    class Query:
        def __init__(self, model):
            self.model = model

        def where(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def with_for_update(self):
            return self

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return None

        async def scalar(self, query):
            if query.model is reconcile.RuntimeLease:
                return lease
            if query.model is reconcile.RuntimeGeneration:
                return owner
            if query.model is reconcile.Run:
                return SimpleNamespace(status=reconcile.RunStatus.running)
            return None

        async def scalars(self, _query):
            return [] if current_state == "ledger_changed" else [group]

    monkeypatch.setattr(reconcile, "sessionmaker", lambda: (lambda: FakeSession()))
    monkeypatch.setattr(reconcile, "select", lambda model: Query(model))
    monkeypatch.setattr(reconcile, "_control", lambda _db: _return_none())
    monkeypatch.setattr(reconcile, "_process_dead", lambda *_args: True)
    states = iter([("present", True), ("absent", True)])
    monkeypatch.setattr(reconcile, "_group_state", lambda _row: next(states, ("absent", True)))

    async def stop(_group):
        signal_calls.append(_group.pgid)
        return True

    monkeypatch.setattr(reconcile, "_stop_verified_group", stop)
    assert await reconcile._apply_one(entry) is False
    assert signal_calls == []


async def _return_none():
    return None


@pytest.mark.asyncio
async def test_reconcile_dry_run_does_not_call_apply_or_change_rows(monkeypatch):
    from ttakkari import executor_reconcile as reconcile

    entry = {"run_id": "run-1", "owner_id": "owner-1", "disposition": "process_group_live",
             "groups": [], "_group_rows": [], "_lease_token": "token-1"}
    state = {"lease_deleted": False, "group_deleted": False, "signals": 0}
    monkeypatch.setattr(reconcile, "_read_orphans", lambda: _entries([entry]))

    async def forbidden_apply(_entry):
        state["signals"] += 1
        state["lease_deleted"] = True
        return True

    monkeypatch.setattr(reconcile, "_apply_one", forbidden_apply)
    result = await reconcile.reconcile_orphans(apply=False)
    assert result["mode"] == "dry-run"
    assert result["reconciled"] == 0
    assert state == {"lease_deleted": False, "group_deleted": False, "signals": 0}


async def test_bulk_reconcile_attaches_claim_fence_to_each_apply(monkeypatch):
    import uuid

    from ttakkari import executor_reconcile as reconcile

    entry = {"run_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4()),
             "disposition": "safe_to_release", "groups": [], "_group_rows": [],
             "_lease_token": uuid.uuid4()}
    fence = {"generation_id": uuid.uuid4(), "revision": "a" * 40, "epoch": 23}
    applied = []

    async def capture(entry):
        applied.append(entry)
        return False

    monkeypatch.setattr(reconcile, "_read_orphans", lambda: _entries([entry]))
    monkeypatch.setattr(reconcile, "_apply_one", capture)

    result = await reconcile.reconcile_orphans(apply=True, expected_claim_owner_fence=fence)

    assert result["reconciled"] == 0
    assert applied == [{**entry, "expected_claim_owner_fence": fence}]
    assert "expected_claim_owner_fence" not in entry


async def _run_apply_with_pg_error(monkeypatch, sqlstate, message):
    import uuid

    from sqlalchemy.exc import DBAPIError

    from ttakkari import executor_reconcile as reconcile

    entry = {"run_id": str(uuid.uuid4()), "owner_id": str(uuid.uuid4()),
             "disposition": "process_group_live",
             "groups": [], "_group_rows": [], "_lease_token": "token-1"}
    state = {"local_settings": [], "rollback": 0, "signals": 0}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            state["local_settings"].append(str(statement))

        async def rollback(self):
            state["rollback"] += 1

    class PgError(Exception):
        def __init__(self):
            super().__init__(message)
            self.sqlstate = sqlstate

    original = PgError()

    async def lock_control(_db):
        raise DBAPIError("SELECT ... FOR UPDATE", {}, original, False)

    async def forbidden_signal(_group):
        state["signals"] += 1
        return True

    monkeypatch.setattr(reconcile, "sessionmaker", lambda: (lambda: FakeSession()))
    monkeypatch.setattr(reconcile, "_read_orphans", lambda: _entries([entry]))
    monkeypatch.setattr(reconcile, "_control", lock_control)
    monkeypatch.setattr(reconcile, "_stop_verified_group", forbidden_signal)
    return reconcile, state, original


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate,message,action", [
    ("55P03", "canceling statement due to lock timeout", "retryable_lock_timeout"),
    ("57014", "canceling statement due to statement timeout", "retryable_statement_timeout"),
])
async def test_reconcile_timeout_rolls_back_and_reports_distinct_retryable(
        monkeypatch, sqlstate, message, action):
    reconcile, state, _ = await _run_apply_with_pg_error(monkeypatch, sqlstate, message)

    result = await reconcile.reconcile_orphans(apply=True)

    assert result["reconciled"] == 0
    assert result["leases"][0]["reconciled"] is False
    assert result["leases"][0]["action"] == action
    assert sqlstate in result["leases"][0]["reason"]
    assert state["rollback"] == 1
    assert state["signals"] == 0
    assert any("SET LOCAL lock_timeout" in sql for sql in state["local_settings"])
    assert any("SET LOCAL statement_timeout" in sql for sql in state["local_settings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate,message", [
    ("57014", "canceling statement due to user request"),
    ("57014", "사용자 요청에 의해 문을 취소하는 중"),
    ("40001", "could not serialize access due to concurrent update"),
    ("23505", "duplicate key value violates unique constraint"),
])
async def test_reconcile_user_cancel_and_other_errors_roll_back_and_propagate(
        monkeypatch, sqlstate, message):
    from sqlalchemy.exc import DBAPIError

    reconcile, state, original = await _run_apply_with_pg_error(monkeypatch, sqlstate, message)

    with pytest.raises(DBAPIError) as raised:
        await reconcile.reconcile_orphans(apply=True)

    assert raised.value.orig is original
    assert state["rollback"] == 1
    assert state["signals"] == 0


async def _entries(values):
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("token_matches", [True, False])
async def test_normal_release_uses_control_and_lease_fence(monkeypatch, token_matches):
    import uuid
    from types import SimpleNamespace

    from ttakkari import generation

    owner_id, token = uuid.uuid4(), uuid.uuid4()
    lease = SimpleNamespace(generation_id=owner_id, token=token if token_matches else uuid.uuid4())
    order = []

    class Query:
        def __init__(self, model):
            self.model = model
            self.locked = False

        def where(self, *_args):
            return self

        def with_for_update(self):
            self.locked = True
            return self

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def scalar(self, query):
            order.append(("lease_read", query.locked))
            return lease

        async def execute(self, _query):
            order.append(("delete_groups",))

        async def delete(self, _row):
            order.append(("delete_lease",))

        async def commit(self):
            order.append(("commit",))

    async def lock_control(_db):
        order.append(("control_lock",))

    async def wake(_db):
        order.append(("wake",))

    runtime = object.__new__(generation.GenerationRuntime)
    runtime.id = owner_id
    runtime.tokens = {}
    monkeypatch.setattr(generation, "sessionmaker", lambda: (lambda: FakeSession()))
    monkeypatch.setattr(generation, "select", lambda model: Query(model))
    monkeypatch.setattr(generation, "_control", lock_control)
    monkeypatch.setattr(generation, "wake", wake)

    if token_matches:
        await runtime.release(uuid.uuid4(), token)
        assert order == [("control_lock",), ("lease_read", True), ("delete_groups",),
                         ("delete_lease",), ("wake",), ("commit",)]
    else:
        with pytest.raises(generation.OwnershipLost):
            await runtime.release(uuid.uuid4(), token)
        assert order == [("control_lock",), ("lease_read", True)]
