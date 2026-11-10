"""오염 점검: 찾고, 기록하며 가리고, 되돌린다. 지우지 않는다."""
import shutil
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from ttakkari import kb_repair as repair
from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument
from ttakkari.kb_ontology_models import KbEntity
from ttakkari.kb_store import ingest_source, originals_root

GOAL = "로보틱스 종합 기술 백과사전 구축"
LONG = "Robot arm kinematics and control. " * 40


@pytest.fixture(autouse=True)
def _clean_originals():
    yield
    root = originals_root()
    if root.exists():
        shutil.rmtree(root)


async def _doc(db, key, url, body, title="Doc", age=0):
    res = await ingest_source(db, source_key=key, data=body.encode(), kind="web", locator=url, media_type="text/html",
                              title=title, lang="en", normalized_body=body, origin_meta={"via": "collector", "tenant_id": "owner", "pool": "personal"})
    await db.execute(text("INSERT INTO kb_document_access (document_id, tenant_id, scope, hidden, pool, origin_scope, visibility) "
                          "VALUES (:d, 'owner', 'personal', false, 'personal', 'personal', 'global') ON CONFLICT DO NOTHING"), {"d": res.document_id})
    await db.execute(text("UPDATE kb_documents SET created_at = :t WHERE id = :d"),
                     {"t": datetime.now(UTC) - timedelta(minutes=age), "d": res.document_id})
    return res.document_id


async def _hidden(db, doc_id):
    return (await db.get(KbDocumentAccess, doc_id)).hidden


async def test_scan_hides_thin_listing_and_duplicates_then_revert_restores_everything():
    async with sessionmaker()() as db:
        good = await _doc(db, "c:good", "https://a.org/arm", LONG + "unique one", "Arm", age=10)
        dup = await _doc(db, "c:dup", "https://b.org/arm-copy", LONG + "unique one", "Arm copy", age=1)
        thin = await _doc(db, "c:thin", "https://a.org/tiny", "short", "Tiny")
        listing = await _doc(db, "c:list", "https://arxiv.org/list/cs.SE/recent?skip=50", LONG + "listing page", "Software Engineering")
        await db.commit()

        dry = await repair.scan(db, apply=False)
        assert dry["applied"] is False and dry["hide"]["total"] == 3
        assert not any([await _hidden(db, d) for d in (good, dup, thin, listing)])

        done = await repair.scan(db, apply=True)
        await db.commit()
        assert {await _hidden(db, d) for d in (thin, listing, dup)} == {True}
        assert await _hidden(db, good) is False
        assert (await db.get(KbDocument, dup)) is not None  # 지우지 않는다

        again = await repair.scan(db, apply=True)
        assert again["hide"]["total"] == 0  # 이미 가린 것은 다시 세지 않는다

        result = await repair.revert(db, done["run_id"])
        await db.commit()
        assert result["restored"]["repair_hide"] == 3
        assert not any([await _hidden(db, d) for d in (good, dup, thin, listing)])

        after_revert = await repair.scan(db, apply=True)
        assert after_revert["hide"]["total"] == 0  # 주인이 되돌린 것은 다시 가리지 않는다


async def test_retitle_and_template_entities_are_recorded_and_revertible():
    async with sessionmaker()() as db:
        doc = await _doc(db, "c:t", "https://raw.githubusercontent.com/o/r/sha/docs/badge.md", "# Badge API\n" + LONG, "Software Engineering")
        for name in ("On this page", "Gestalt Principles"):
            db.add(KbEntity(canonical_name=name, norm_name=name.lower(), entity_kind="concept"))
        await db.commit()
        report = await repair.scan(db, apply=True)
        await db.commit()
        assert report["retitle"]["total"] == 1 and report["archive_entities"]["total"] == 1
        assert (await db.get(KbDocument, doc)).title == "Badge API"
        statuses = dict((await db.execute(text("SELECT canonical_name, status FROM kb_entities"))).all())
        assert statuses == {"On this page": "archived", "Gestalt Principles": "active"}

        await repair.revert(db, report["run_id"])
        await db.commit()
        db.expire_all()
        assert (await db.get(KbDocument, doc)).title == "Software Engineering"
        assert dict((await db.execute(text("SELECT canonical_name, status FROM kb_entities"))).all())["On this page"] == "active"


def test_spam_host_is_found_from_data_not_from_a_list():
    def fact(i, host, rel):
        return repair.Facts(doc_id=f"d{host}{i}", title="t", created_at=datetime.now(UTC), url=f"https://{host}/p{i}", host=host,
                            chars=2000, text_hash=f"{host}{i}", cta=0.0, relevance=rel, own_relevance=rel, mission_goal_title=None, new_title=None)
    docs = [fact(i, "seo.example", 0.02) for i in range(24)] + [fact(i, "seo.example", None) for i in range(24, 40)]
    docs += [fact(i, "good.example", 0.6) for i in range(40)]
    repair.judge(docs)
    flagged = {d.host for d in docs if d.reasons}
    assert flagged == {"seo.example"}
    assert all(d.reasons for d in docs if d.host == "seo.example")


def test_one_low_scoring_document_is_never_hidden_alone():
    def fact(i, rel):
        return repair.Facts(doc_id=f"x{i}", title="t", created_at=datetime.now(UTC), url=f"https://docs.example/p{i}", host="docs.example",
                            chars=2000, text_hash=f"x{i}", cta=0.0, relevance=rel, own_relevance=rel, mission_goal_title=None, new_title=None)
    docs = [fact(i, 0.0) for i in range(5)] + [fact(i, 0.7) for i in range(5, 60)]
    repair.judge(docs)
    assert not any(d.reasons for d in docs)


async def test_api_dry_run_is_default(client, auth):
    r = await client.post("/api/assistant/collect/repair", headers=auth)
    assert r.status_code == 200 and r.json()["applied"] is False
    assert (await client.get("/api/assistant/collect/repair/runs", headers=auth)).status_code == 200
    assert (await client.post("/api/assistant/collect/repair/kbrepair:nope/revert", headers=auth)).status_code == 404
    assert (await client.post("/api/assistant/collect/repair/other/revert", headers=auth)).status_code == 422


def test_title_equal_to_the_mission_goal_is_replaced():
    row = {"doc_id": "x", "title": GOAL, "created_at": datetime.now(UTC), "char_count": 2000,
           "body": "# Badge API\n" + LONG, "url": "https://raw.githubusercontent.com/o/r/sha/a.md", "goal": GOAL}
    assert repair.facts_for(row).new_title == "Badge API"


def test_blocked_host_is_hidden_and_suspects_are_only_reported():
    def fact(i, host, own):
        return repair.Facts(doc_id=f"{host}{i}", title="t", created_at=datetime.now(UTC), url=f"https://{host}/p{i}", host=host,
                            chars=2000, text_hash=f"{host}{i}", cta=0.0, relevance=0.5, own_relevance=own, mission_goal_title=None, new_title=None)
    docs = [fact(i, "agency.example", 0.01) for i in range(25)] + [fact(i, "ok.example", 0.7) for i in range(25)]
    repair.judge(docs)
    assert not any(d.reasons for d in docs)  # 의심만으로는 가리지 않는다
    assert [h["host"] for h in repair.suspect_hosts(docs, frozenset())] == ["agency.example"]
    repair.judge(docs, frozenset({"agency.example"}))
    assert {d.host for d in docs if d.reasons} == {"agency.example"}


async def test_blocking_a_host_through_the_api_persists_and_hides_on_next_scan(client, auth):
    async with sessionmaker()() as db:
        doc = await _doc(db, "c:b", "https://agency.example/x", LONG + "agency page", "Agency")
        await db.commit()
    r = await client.put("/api/assistant/collect/repair/blocked-hosts", json={"host": "www.Agency.example", "blocked": True}, headers=auth)
    assert r.status_code == 200 and r.json()["hosts"] == ["agency.example"]
    async with sessionmaker()() as db:
        await repair.scan(db, apply=True)
        await db.commit()
        assert await _hidden(db, doc) is True
    r = await client.put("/api/assistant/collect/repair/blocked-hosts", json={"host": "agency.example", "blocked": False}, headers=auth)
    assert r.json()["hosts"] == []
    async with sessionmaker()() as db:
        assert await _hidden(db, doc) is False
    bad = await client.put("/api/assistant/collect/repair/blocked-hosts", json={"host": "not a host", "blocked": True}, headers=auth)
    assert bad.status_code == 422
