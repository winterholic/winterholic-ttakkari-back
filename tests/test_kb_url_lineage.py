"""The same page reached from two missions is one document lineage, not two documents."""
import shutil

import pytest

from ttakkari.db import sessionmaker
from ttakkari.kb_library import existing_doc_key_for_url
from ttakkari.kb_store import ingest_source, originals_root

URL = "https://example.org/guide/buttons"


@pytest.fixture(autouse=True)
def _clean_originals():
    yield
    root = originals_root()
    if root.exists():
        shutil.rmtree(root)


async def _web(db, key, pool="personal"):
    return await ingest_source(
        db, source_key=key, data=b"Buttons guide body text.\n", kind="web", locator=URL, media_type="text/html",
        title="Buttons", lang="en", normalized_body="Buttons guide body text.\n",
        origin_meta={"tenant_id": "owner", "pool": pool},
    )


async def test_first_document_for_a_url_is_the_lineage_other_missions_reuse():
    async with sessionmaker()() as db:
        assert await existing_doc_key_for_url(db, "owner", "personal", URL) is None
        first = await _web(db, "collector:mission-a")
        assert await existing_doc_key_for_url(db, "owner", "personal", URL) == "collector:mission-a"
        again = await _web(db, await existing_doc_key_for_url(db, "owner", "personal", URL))
        assert again.document_id == first.document_id and again.created is False


async def test_other_pool_or_tenant_never_shares_a_lineage():
    async with sessionmaker()() as db:
        await _web(db, "collector:mission-a", pool="personal")
        assert await existing_doc_key_for_url(db, "owner", "work", URL) is None
        assert await existing_doc_key_for_url(db, "someone", "personal", URL) is None
