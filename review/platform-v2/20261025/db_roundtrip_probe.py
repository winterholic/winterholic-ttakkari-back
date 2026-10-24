"""Exercise the existing Alembic graph in one owned, injected test-cluster DB.

Never starts initdb, reads credentials from files, or connects to the app database.
Only synthetic rows; the managed URL is passed to children and never logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
COMMON = "0067_collector_outbox_retry"
LIBRARY = "0068_library_intent"
HEAD = "0071_library_platform_v2_merge"


async def main() -> None:
    url = make_url(os.environ["TTAKKARI_TEST_DB"])
    assert url.host in {"localhost", "127.0.0.1", "::1"} and "test" in url.database and url.database != "ttakkari"
    name = "ttk_v2_roundtrip_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False))
    target = url.set(database=name)
    revision = (await asyncio.to_thread(subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True)).strip()
    result = {"source_revision": revision,
              "environment": "root Codex command sandbox; explicitly injected isolated Run test cluster",
              "database": name, "initdb": "not invoked", "synthetic_data_only": True,
              "steps": [], "status": "running", "cleanup": False}
    created = False
    conn = None

    async def migrate(direction: str, revision: str) -> None:
        started = time.monotonic()
        command = [str(ROOT / ".venv/bin/alembic"), direction, revision]
        done = await asyncio.to_thread(subprocess.run, command, cwd=ROOT,
                                       env={**os.environ, "TTAKKARI_NO_ENV_FILE": "1",
                                            "TTAKKARI_DATABASE_URL": target.render_as_string(hide_password=False)},
                                       capture_output=True, text=True, timeout=90)
        stderr = done.stderr
        if url.password:
            stderr = stderr.replace(url.password, "<redacted>")
        step = {"command": [".venv/bin/alembic", direction, revision], "exit": done.returncode,
                "seconds": round(time.monotonic() - started, 3), "stderr_tail": stderr.splitlines()[-6:]}
        result["steps"].append(step)
        assert done.returncode == 0, step

    async def versions() -> list[asyncpg.Record]:
        return list(await conn.fetch("SELECT version_num FROM alembic_version ORDER BY version_num"))

    async def state(label: str) -> dict:
        rows = await conn.fetch("SELECT table_name,column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position")
        columns = [list(r) for r in rows]
        return {"label": label, "versions": [r[0] for r in await versions()],
                "columns": columns, "column_sha256": hashlib.sha256(json.dumps(columns).encode()).hexdigest()}

    async def artifact_hash(fields: str) -> str:
        values = [dict(r) for r in await conn.fetch(f"SELECT {fields} FROM artifacts ORDER BY filename")]
        return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()

    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        await migrate("upgrade", COMMON)
        conn = await asyncpg.connect(target.set(drivername="postgresql").render_as_string(hide_password=False))
        common_before = await state("common_before")
        await migrate("upgrade", LIBRARY)
        for filename, source, origin, reason in (("upload.txt", "upload", "user", "direct_upload"),
                                                 ("kept.md", "agent_output", "agent", "chat_request")):
            await conn.execute("INSERT INTO artifacts(id,filename,mime_type,kind,category,source,origin,area,keep_reason,keep_evidence,original_path,rel_path,storage_mode,stored_path,size_bytes,sha256,preview_status,export_policy,created_at) VALUES($1,$2,'text/plain','code','document',$3,$4,'files',$5,'{\"instruction\":\"synthetic test only\"}','/tmp/synthetic','synthetic','copy','/tmp/synthetic',1,$6,'not_required','allow',now())",
                               uuid.uuid4(), filename, source, origin, reason, "a" * 64)
        library_seed = await state("library_seed")
        assert library_seed["versions"] == [LIBRARY]
        keep_fields = "filename,source,origin,area,keep_reason,keep_evidence,category,sha256"
        keep_hash = await artifact_hash(keep_fields)
        stable_fields = "id,filename,source,area,sha256,size_bytes,created_at"
        stable_hash = await artifact_hash(stable_fields)
        await migrate("upgrade", "head")
        head_first = await state("head_first")
        assert head_first["versions"] == [HEAD]
        assert keep_hash == await artifact_hash(keep_fields), "Library intent replayed during V2 upgrade"
        await migrate("downgrade", LIBRARY)
        one_parent = await state("downgrade_one_parent")
        assert "0070_background_consumer_queue" in one_parent["versions"], "Record actual surviving V2 branch"
        assert any(row[:2] == ["game_builds", "queue_state"] for row in one_parent["columns"])
        await migrate("downgrade", COMMON)
        common_after = await state("common_after")
        assert common_after["versions"] == [COMMON]
        assert common_before["columns"] == common_after["columns"], "Schema did not round trip to common ancestor"
        assert stable_hash == await artifact_hash(stable_fields), "Existing payload changed"
        await migrate("upgrade", "head")
        head_again = await state("head_again")
        assert head_again["versions"] == [HEAD] and head_again["columns"] == head_first["columns"]
        result.update(status="passed", snapshots=[common_before, library_seed, head_first, one_parent, common_after, head_again],
                      library_intent_preserved_on_main_to_v2=True, seeded_payload_preserved_on_common_roundtrip=True,
                      limitation="Downgrade to common removes library metadata by design. This is schema/payload roundtrip, not a production backup restore or lossless user-intent rollback.")
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        if conn is not None:
            await conn.close()
        if created:
            await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()
        result["cleanup"] = True
        (OUT / "db-roundtrip-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "steps": len(result["steps"]), "cleanup": result["cleanup"],
                      "one_parent_versions": result["snapshots"][3]["versions"],
                      "library_intent_preserved_on_main_to_v2": True, "common_roundtrip": "schema and seeded payload"}))


if __name__ == "__main__":
    asyncio.run(main())
