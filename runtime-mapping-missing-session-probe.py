import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ttakkari.operations.inline import _lease_process_mappings


def uid():
    return str(uuid.uuid4())


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    ids = {name: uid() for name in (
        "active_session", "archived_session", "absent_session",
        "active_lease_run", "archived_lease_run", "absent_session_lease_run", "missing_lease_run",
        "active_group_run", "archived_group_run", "absent_session_group_run", "missing_group_run",
    )}
    async with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE runtime_leases (run_id UUID, session_id UUID, generation_id UUID, token UUID, account_id UUID, created_at TEXT)",
            "CREATE TABLE runtime_process_groups (id UUID, run_id UUID, generation_id UUID, attempt INTEGER, lease_token UUID, created_at TEXT, pid INTEGER, pgid INTEGER, process_started_at TEXT)",
            "CREATE TABLE runs (id UUID, session_id UUID, status TEXT)",
            "CREATE TABLE sessions (id UUID, archived_at TEXT)",
            "CREATE TABLE engine_accounts (id UUID, name TEXT, config_dir TEXT)",
        ):
            await conn.execute(text(ddl))
        await conn.execute(text("INSERT INTO sessions VALUES (:id, NULL)"), {"id": ids["active_session"]})
        await conn.execute(
            text("INSERT INTO sessions VALUES (:id, '2026-11-01T00:00:00Z')"),
            {"id": ids["archived_session"]},
        )
        for key, status, session_key in (
            ("active_lease_run", "running", "active_session"),
            ("archived_lease_run", "succeeded", "archived_session"),
            ("absent_session_lease_run", "failed", "absent_session"),
            ("active_group_run", "running", "active_session"),
            ("archived_group_run", "succeeded", "archived_session"),
            ("absent_session_group_run", "failed", "absent_session"),
        ):
            await conn.execute(
                text("INSERT INTO runs VALUES (:id, :session, :status)"),
                {"id": ids[key], "session": ids[session_key], "status": status},
            )
        lease_cases = (
            ("active_lease_run", "active_session"),
            ("archived_lease_run", "archived_session"),
            ("absent_session_lease_run", "absent_session"),
            ("missing_lease_run", "absent_session"),
        )
        for index, (run_key, session_key) in enumerate(lease_cases):
            await conn.execute(
                text("INSERT INTO runtime_leases VALUES (:run, :session, :gen, :token, NULL, :created)"),
                {"run": ids[run_key], "session": ids[session_key], "gen": uid(), "token": uid(),
                 "created": f"2026-11-01-{index:02}"},
            )
        group_cases = (
            ("active_group_run", "active_session"),
            ("archived_group_run", "archived_session"),
            ("absent_session_group_run", "absent_session"),
            ("missing_group_run", "absent_session"),
        )
        for index, (run_key, _session_key) in enumerate(group_cases):
            await conn.execute(
                text("INSERT INTO runtime_process_groups VALUES (:id, :run, :gen, 1, :token, :created, NULL, NULL, NULL)"),
                {"id": uid(), "run": ids[run_key], "gen": uid(), "token": uid(),
                 "created": f"2026-11-02-{index:02}"},
            )

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        rows, truncated = await _lease_process_mappings(session)
        assert not truncated
        for kind in ("lease_only", "orphan_process_group"):
            selected = {row["run_id"]: row for row in rows if row["entry_kind"] == kind}
            for suffix, expected in (("active", False), ("archived", True), ("absent_session", None)):
                row = selected[ids[f"{suffix}_{'lease_run' if kind == 'lease_only' else 'group_run'}"]]
                assert row["session_archived"] is expected, (kind, suffix, row["session_archived"])
            missing = selected[ids[f"missing_{'lease_run' if kind == 'lease_only' else 'group_run'}"]]
            assert missing["run_status"] == "missing"
            assert missing["session_archived"] is None, (kind, missing["session_archived"])
    print("lease SQL: active=false, archived=true, missing session=unknown; missing Run/session=unknown: PASS")
    print("orphan-group SQL: active=false, archived=true, missing session=unknown; missing Run/session=unknown: PASS")
    await engine.dispose()


asyncio.run(main())
