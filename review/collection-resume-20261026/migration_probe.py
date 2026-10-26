"""Apply both published schema histories to disposable Run-owned databases."""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
HEAD = "0075_collection_research_merge"


async def main():
    base = make_url(os.environ["TTAKKARI_TEST_DB"])
    assert base.host == "127.0.0.1" and base.database == "ttakkari_test"
    admin = await asyncpg.connect(base.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False))
    records = []
    try:
        for branch in ("0073_research_reuse_source", "0074_collection_single_library"):
            name = "collection_accept_" + uuid.uuid4().hex
            await admin.execute(f'CREATE DATABASE "{name}"')
            url = base.set(database=name)
            secret_url = url.render_as_string(hide_password=False)
            try:
                env = {**os.environ, "TTAKKARI_NO_ENV_FILE": "1", "TTAKKARI_DATABASE_URL": secret_url,
                       "PYTHONPATH": str(ROOT / "src")}
                for target in (branch, "head"):
                    command = [sys.executable, "-m", "alembic", "upgrade", target]
                    result = await asyncio.to_thread(subprocess.run, command, cwd=ROOT, env=env,
                                                     capture_output=True, text=True, timeout=120)
                    output = (result.stdout + result.stderr).replace(secret_url, "<run-test-database>")
                    (OUT / f"{branch}-{target}.txt").write_text(output)
                    assert result.returncode == 0, output[-3000:]
                    db = await asyncpg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False))
                    try:
                        heads = await db.fetch("SELECT version_num FROM alembic_version")
                        versions = [r[0] for r in heads]
                        assert versions == [branch if target == branch else HEAD], versions
                        if target == "head":
                            tables = {r[0] for r in await db.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
                            assert {"collection_missions", "collection_mission_runs", "kb_document_aliases", "research_studies"} <= tables, sorted(tables)
                        records.append({"source_head": branch, "target": target, "versions": versions,
                                        "database": name, "exit": result.returncode})
                    finally:
                        await db.close()
            finally:
                await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    finally:
        await admin.close()
        (OUT / "migration-receipt.json").write_text(json.dumps(records, indent=2))
    print(json.dumps({"branches": 2, "upgrades": len(records), "head": HEAD, "isolated_cleanup": True}))


asyncio.run(main())
