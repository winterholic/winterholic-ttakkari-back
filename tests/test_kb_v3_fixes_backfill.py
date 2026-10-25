"""접근 행이 없는 기존 문서의 백필(kb_access_backfill): dry-run 은 쓰지 않고, 미상은 work_only, 멱등, DB 이름 확인."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.db import sessionmaker
from ttakkari.kb_access import create_personal_agent_context, create_work_agent_context
from ttakkari.kb_access_backfill import check_target, classify, run_backfill
from ttakkari.kb_retrieval import search
from ttakkari.kb_store import ingest_source, originals_root, segment_revision


@pytest.fixture(autouse=True)
def _originals():
    root = originals_root()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    yield
    if root.exists():
        shutil.rmtree(root)


async def _rowless(key: str, text: str, origin_meta: dict | None = None):
    async with sessionmaker()() as db:
        res = await ingest_source(db, source_key=key, data=text.encode(), title=key, lang="en", origin_meta=origin_meta)
        await segment_revision(db, res.revision_id)
        await db.commit()
        return res.document_id


def test_classify_rules_and_fail_closed_default():
    assert classify("research:work:x", None) == ("work", "research_doc_key")
    assert classify("research:personal:x", None) == ("personal", "research_doc_key")
    assert classify("c1", {"via": "collector", "scope": "personal"}) == ("personal", "collector_origin_meta")
    assert classify("c2", {"via": "collector", "scope": "bogus"}) == ("work", "unknown_fail_closed")
    assert classify("file:/notes/a.md", {}, ["file:/notes/"]) == ("personal", "operator_personal_prefix")
    assert classify("file:/other/a.md", {}, ["file:/notes/"]) == ("work", "unknown_fail_closed")
    assert classify("", {}, [""]) == ("work", "unknown_fail_closed")  # 빈 접두사로 전부 personal 이 되지 않는다


def test_apply_requires_confirming_the_database_name():
    url = "postgresql+asyncpg://u:p@127.0.0.1:5432/ttakkari_prod_like"
    assert check_target(url, apply=False, confirm_database=None) == "ttakkari_prod_like"
    with pytest.raises(PermissionError):
        check_target(url, apply=True, confirm_database=None)
    with pytest.raises(PermissionError):
        check_target(url, apply=True, confirm_database="other")
    assert check_target(url, apply=True, confirm_database="ttakkari_prod_like") == "ttakkari_prod_like"


async def test_dry_run_writes_nothing_and_apply_fills_rows_idempotently_without_widening_exposure():
    unknown = await _rowless("file:/misc/cli.md", "Backfillquokka unknown origin document body.")
    research_work = await _rowless("research:work:wk", "Backfillquokka research work body.", {"research_scope": "work"})
    research_personal = await _rowless("research:personal:pe", "Backfillquokka research personal body.")
    prefixed = await _rowless("file:/notes/mine.md", "Backfillquokka operator picked personal body.")
    async with sessionmaker()() as db:
        rows_before = await db.scalar(select(func.count()).select_from(KbDocumentAccess))
        personal = await search(db, query="Backfillquokka", access_context=create_personal_agent_context(), mode="lexical")
        assert personal["items"] == []  # 접근 행이 없으면 백필 전에도 개인에는 안 보인다

        dry = await run_backfill(db, apply=False, personal_prefixes=["file:/notes/"])
        assert dry.as_dict()["mode"] == "dry_run" and dry.total == 4 and dry.inserted == 0
        assert dry.by_scope == {"work": 2, "personal": 2}
        assert dry.by_rule == {"unknown_fail_closed": 1, "research_doc_key": 2, "operator_personal_prefix": 1}
        assert await db.scalar(select(func.count()).select_from(KbDocumentAccess)) == rows_before

        applied = await run_backfill(db, apply=True, personal_prefixes=["file:/notes/"])
        assert applied.inserted == 4
        again = await run_backfill(db, apply=True, personal_prefixes=["file:/notes/"])
        assert again.total == 0 and again.inserted == 0  # 멱등

    async with sessionmaker()() as db:
        rows = {r.document_id: (r.scope, r.origin_scope, r.visibility, r.tenant_id, r.hidden)
                for r in await db.scalars(select(KbDocumentAccess))}
        assert rows[unknown] == ("work", "work", "work_only", "owner", False)
        assert rows[research_work] == ("work", "work", "work_only", "owner", False)
        assert rows[research_personal] == ("personal", "personal", "global", "owner", False)
        assert rows[prefixed] == ("personal", "personal", "global", "owner", False)
        personal = await search(db, query="Backfillquokka", access_context=create_personal_agent_context(), mode="lexical")
        assert {i["document"]["id"] for i in personal["items"]} == {str(research_personal), str(prefixed)}
        work = await search(db, query="Backfillquokka", access_context=create_work_agent_context(), mode="lexical")
        assert {i["document"]["id"] for i in work["items"]} == {str(unknown), str(research_work), str(research_personal), str(prefixed)}


def test_cli_dry_run_reports_and_apply_without_confirmation_is_refused():
    def cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ttakkari.cli", "kb-backfill-access", *args],
            env=os.environ.copy(), capture_output=True, text=True, timeout=120, check=False,
        )

    dry = cli()
    assert dry.returncode == 0, dry.stderr[-600:]
    assert json.loads(dry.stdout)["mode"] == "dry_run"
    refused = cli("--apply")
    assert refused.returncode == 2 and "--confirm-database" in refused.stderr
