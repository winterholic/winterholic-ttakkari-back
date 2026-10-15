"""An independent 0025 schema contains source-only data and a user-edited meeting."""

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path

import asyncpg
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def migration_database():
    name = "ttk_retire_" + uuid.uuid4().hex
    admin = await asyncpg.connect(database="postgres", host="localhost")
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def migrate(revision, database):
    env = {
        **os.environ,
        "TTAKKARI_NO_ENV_FILE": "1",
        "TTAKKARI_DATABASE_URL": f"postgresql+asyncpg://localhost/{database}",
    }
    subprocess.run(
        [str(ROOT / ".venv/bin/alembic"), "upgrade", revision]
        if revision != "-1"
        else [str(ROOT / ".venv/bin/alembic"), "downgrade", "0025"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


async def test_retirement_preserves_every_source_row_and_meetings_and_downgrades(
    migration_database,
):
    database = migration_database
    # The database is dedicated to this migration test, never the app/test fixture DB.
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, "0025", database)
    conn = await asyncpg.connect(database=database, host="localhost")
    mids = [uuid.uuid4(), uuid.uuid4()]
    noteids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    jobid = uuid.uuid4()
    try:
        for i, mid in enumerate(mids):
            await conn.execute(
                """INSERT INTO meetings(id,title,scope,status,participants,source,transcript,transcript_source,analysis_context,created_at,updated_at)
                VALUES($1,$2,'personal','transcribed','[]','text',$3,'provider:clovanote','{}',now(),now())""",
                mid,
                f"보존 {i}",
                "사용자 교정 원문" if i == 0 else None,
            )
        for i, nid in enumerate(noteids):
            await conn.execute(
                """INSERT INTO clovanote_notes(id,note_id,title,state,speakers,segments,transcript,keywords,meta,meeting_id,synced_at,created_at)
                VALUES($1,$2,'원래 제목','ready','["A"]','[{"text":"원시 구간"}]',$3,'[]','{}',$4,now(),now())""",
                nid,
                f"note-{i}",
                f"외부 원전사 {i}",
                mids[i] if i < 2 else None,
            )
        await conn.execute(
            """INSERT INTO clovanote_jobs(id,kind,status,stage,meeting_id,params,result,steps,created_at,updated_at)
            VALUES($1,'upload','failed','recognize',$2,'{}','{}','[]',now(),now())""",
            jobid,
            mids[0],
        )
        before_notes = await conn.fetch(
            "SELECT id,to_jsonb(n) AS data FROM clovanote_notes n ORDER BY id"
        )
        before_job = await conn.fetchval(
            "SELECT to_jsonb(j) FROM clovanote_jobs j WHERE id=$1", jobid
        )
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, "head", database)
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        assert await conn.fetchval("SELECT to_regclass('clovanote_notes')") is None
        assert await conn.fetchval("SELECT to_regclass('clovanote_jobs')") is None
        assert await conn.fetchval("SELECT count(*) FROM meetings") == 2
        assert (
            await conn.fetchval("SELECT transcript FROM meetings WHERE id=$1", mids[0])
            == "사용자 교정 원문"
        )
        assert (
            await conn.fetchval("SELECT transcript FROM meetings WHERE id=$1", mids[1])
            == "외부 원전사 1"
        )
        for row in before_notes:
            archived = await conn.fetchval(
                "SELECT payload FROM retired_integration_records WHERE source='clovanote_notes' AND id=$1",
                row["id"],
            )
            assert json.loads(archived) == json.loads(row["data"])
        assert json.loads(
            await conn.fetchval(
                "SELECT payload FROM retired_integration_records WHERE source='clovanote_jobs' AND id=$1",
                jobid,
            )
        ) == json.loads(before_job)
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, "-1", database)
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        for row in before_notes:
            assert json.loads(
                await conn.fetchval(
                    "SELECT to_jsonb(n) FROM clovanote_notes n WHERE id=$1", row["id"]
                )
            ) == json.loads(row["data"])
        assert json.loads(
            await conn.fetchval(
                "SELECT to_jsonb(j) FROM clovanote_jobs j WHERE id=$1", jobid
            )
        ) == json.loads(before_job)
        assert (
            await conn.fetchval("SELECT transcript FROM meetings WHERE id=$1", mids[0])
            == "사용자 교정 원문"
        )
    finally:
        await conn.close()

    await asyncio.to_thread(migrate, "head", database)
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        await conn.execute("DELETE FROM meetings WHERE id=$1", mids[0])
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, "-1", database)
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        assert await conn.fetchval("SELECT count(*) FROM clovanote_notes") == 3
        assert (
            await conn.fetchval(
                "SELECT meeting_id FROM clovanote_notes WHERE id=$1", noteids[0]
            )
            is None
        )
        assert (
            await conn.fetchval(
                "SELECT meeting_id FROM clovanote_jobs WHERE id=$1", jobid
            )
            is None
        )
    finally:
        await conn.close()
