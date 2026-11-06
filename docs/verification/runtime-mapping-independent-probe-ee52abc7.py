import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from ttakkari.operations.inline import _lease_process_mappings


def uid():
    return str(uuid.uuid4())


async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE runtime_leases (run_id UUID, session_id UUID, generation_id UUID, token UUID, account_id UUID, created_at TEXT)",
            "CREATE TABLE runtime_process_groups (id UUID, run_id UUID, generation_id UUID, attempt INTEGER, lease_token UUID, created_at TEXT, pid INTEGER, pgid INTEGER, process_started_at TEXT)",
            "CREATE TABLE runs (id UUID, session_id UUID, status TEXT)",
            "CREATE TABLE sessions (id UUID, archived_at TEXT)",
            "CREATE TABLE engine_accounts (id UUID, name TEXT, config_dir TEXT)",
        ):
            await conn.execute(text(ddl))

        session_ids = {"active": uid(), "archived": uid()}
        await conn.execute(text("INSERT INTO sessions VALUES (:id, NULL)"), {"id": session_ids["active"]})
        await conn.execute(
            text("INSERT INTO sessions VALUES (:id, '2026-11-01T00:00:00Z')"),
            {"id": session_ids["archived"]},
        )
        expected = {}
        for kind in ("lease_only", "matched_group", "orphan_group"):
            for state in ("active", "archived", "missing_session", "missing_run"):
                run_id = uid()
                session_id = session_ids.get(state, uid())
                gen, token = uid(), uid()
                status = {"active": "running", "archived": "succeeded", "missing_session": "failed"}.get(state)
                if status:
                    await conn.execute(
                        text("INSERT INTO runs VALUES (:id, :session, :status)"),
                        {"id": run_id, "session": session_id, "status": status},
                    )
                if kind in ("lease_only", "matched_group"):
                    await conn.execute(
                        text("INSERT INTO runtime_leases VALUES (:run, :session, :gen, :token, NULL, '2026-11-01')"),
                        {"run": run_id, "session": session_id, "gen": gen, "token": token},
                    )
                if kind in ("matched_group", "orphan_group"):
                    await conn.execute(
                        text("INSERT INTO runtime_process_groups VALUES (:id, :run, :gen, 1, :token, '2026-11-02', NULL, NULL, NULL)"),
                        {"id": uid(), "run": run_id, "gen": gen, "token": token},
                    )
                expected[(kind, state)] = (run_id, status or "missing", {
                    "active": False, "archived": True, "missing_session": None, "missing_run": None
                }[state])

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        rows, truncated = await _lease_process_mappings(session)
        assert not truncated
        for (kind, state), (run_id, status, archived) in expected.items():
            row = next(row for row in rows if row["run_id"] == run_id)
            wanted_kind = {
                "lease_only": "lease_only",
                "matched_group": "lease_process_group",
                "orphan_group": "orphan_process_group",
            }[kind]
            assert row["entry_kind"] == wanted_kind, (kind, state, row["entry_kind"])
            assert row["run_status"] == status, (kind, state, row["run_status"])
            assert row["session_archived"] is archived, (kind, state, row["session_archived"])
    print("lease_only: active=false, archived=true, missing session=null, missing Run/session=null: PASS")
    print("matched group: active=false, archived=true, missing session=null, missing Run/session=null: PASS")
    print("orphan group: active=false, archived=true, missing session=null, missing Run/session=null: PASS")
    await engine.dispose()


asyncio.run(main())
