"""Reproduce the generation-name contract on this Run's disposable PostgreSQL only."""
import asyncio
import os
import subprocess
import sys
import uuid

import asyncpg
from sqlalchemy.engine import make_url


async def main():
    supplied = make_url(os.environ["TTAKKARI_TEST_DB"])
    assert supplied.database == "ttakkari_test" and supplied.username == "ttk_test"
    assert supplied.host in {"127.0.0.1", "localhost"} and supplied.port
    url = supplied.set(drivername="postgresql")
    db = await asyncpg.connect(url.render_as_string(hide_password=False))
    name = "ttakkari_gen_" + uuid.uuid4().hex
    created = False
    try:
        actual = await db.fetchrow("select current_database(), current_user")
        assert tuple(actual) == (supplied.database, supplied.username)
        await db.execute(f'CREATE DATABASE "{name}"')
        created = True
        print(f"Run-owned generation-shaped database created: {name}; owner={supplied.username}", flush=True)
        child_env = {**os.environ, "TTAKKARI_TEST_DB": supplied.set(database=name).render_as_string(hide_password=False)}
        result = await asyncio.to_thread(subprocess.run, [
            sys.executable, "scripts/pytest_slots.py", "--timeout", "60", "--", sys.executable,
            "-m", "pytest", "tests/test_research_v2_combination_acceptance.py",
            "tests/test_research_source_refresh_acceptance.py", "-q", "--timeout=30", "--tb=short",
        ], env=child_env)
        return result.returncode
    finally:
        if created:
            await db.execute(f'DROP DATABASE "{name}"')
            print("Only the database created by this helper was removed.", flush=True)
        await db.close()


raise SystemExit(asyncio.run(main()))
