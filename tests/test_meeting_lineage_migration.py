"""Exercise lineage backfill against the previous real Alembic schema."""

import asyncio
import json
import os
import subprocess
import uuid

import asyncpg
from test_retire_clovanote_migration import ROOT, migration_database  # noqa: F401


def migrate(database, revision, direction="upgrade"):
    import pg_sandbox  # 임시 클러스터 포트를 URL 에 넣어 공유 PG 가 아닌 이 세션 클러스터로 간다

    subprocess.run(
        [str(ROOT / ".venv/bin/alembic"), direction, revision],
        cwd=ROOT,
        env={
            **os.environ,
            "TTAKKARI_NO_ENV_FILE": "1",
            "TTAKKARI_DATABASE_URL": pg_sandbox.localhost_database_url(database),
        },
        capture_output=True,
        text=True,
        check=True,
    )


async def test_lineage_migration_backfills_valid_full_jobs_and_preserves_sources(
    migration_database,  # noqa: F811
):
    database = migration_database
    await asyncio.to_thread(migrate, database, "0026")
    source, derived, missing, malformed, self_ref, candidate, normal = [
        uuid.uuid4() for _ in range(7)
    ]
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        for mid in (source, derived, missing, malformed, self_ref, candidate, normal):
            await conn.execute(
                """INSERT INTO meetings(id,title,scope,status,participants,source,transcript,
                analysis_context,created_at,updated_at)
                VALUES($1,'보존 회의','personal','transcribed','[]','text','원본 전사','{}',now(),now())""",
                mid,
            )
        root_job = uuid.uuid4()

        async def add_job(mid, origin, *, jid=None, parent=None, age=0):
            await conn.execute(
                """INSERT INTO local_stt_jobs(id,meeting_id,status,params,result_metadata,
                transcript,created_at,parent_job_id)
                VALUES($1,$2,'succeeded',$3,'{}','원본 전사',now()+$4*interval '1 second',$5)""",
                jid or uuid.uuid4(),
                mid,
                json.dumps({"source_meeting_id": origin}),
                age,
                parent,
            )

        await add_job(source, None, jid=root_job)
        await add_job(derived, str(source))
        await add_job(
            derived, str(normal), age=1
        )  # Earliest full attempt is authoritative.
        await add_job(derived, str(normal), parent=root_job, age=-1)
        await add_job(missing, str(uuid.uuid4()))
        await add_job(malformed, "not-a-uuid")
        await add_job(self_ref, str(self_ref))
        await add_job(candidate, str(source), parent=root_job)
        before = await conn.fetch(
            "SELECT id,to_jsonb(m) AS data FROM meetings m ORDER BY id"
        )
        jobs_before = await conn.fetch(
            "SELECT to_jsonb(j) AS data FROM local_stt_jobs j ORDER BY id"
        )
    finally:
        await conn.close()

    await asyncio.to_thread(migrate, database, "head")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        columns = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='meetings'"
        )
        assert "source_meeting_id" in {row["column_name"] for row in columns}
        lineage = dict(await conn.fetch("SELECT id,source_meeting_id FROM meetings"))
        assert lineage == {mid: source if mid == derived else None for mid in lineage}
        for row in before:
            current = json.loads(
                await conn.fetchval(
                    "SELECT to_jsonb(m) FROM meetings m WHERE id=$1", row["id"]
                )
            )
            current.pop("source_meeting_id")
            assert current == json.loads(row["data"])
        assert (
            await conn.fetch(
                "SELECT to_jsonb(j) AS data FROM local_stt_jobs j ORDER BY id"
            )
            == jobs_before
        )
    finally:
        await conn.close()

    await asyncio.to_thread(migrate, database, "0026", "downgrade")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        assert (
            await conn.fetch(
                "SELECT id,to_jsonb(m) AS data FROM meetings m ORDER BY id"
            )
            == before
        )
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, database, "head")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        await conn.execute("DELETE FROM meetings WHERE id=$1", source)
        assert (
            await conn.fetchval(
                "SELECT source_meeting_id FROM meetings WHERE id=$1", derived
            )
            is None
        )
        assert await conn.fetchval("SELECT count(*) FROM meetings") == 6
    finally:
        await conn.close()
