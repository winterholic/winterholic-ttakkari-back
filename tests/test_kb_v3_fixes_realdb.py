"""KB v3 독립 검증 결함 교정(실 PostgreSQL): 접근 행 없는 문서 격리, L1 대소문자, 검색 필터·표시, 현재성.

conftest 가 띄우는 프로세스 전용 임시 클러스터에서 돈다. 공유 TTAKKARI_TEST_DB 는 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from ttakkari.collector.control_models import KbDocumentAccess, KbObservation
from ttakkari.common.lexical import terms
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_access import (
    create_owner_context,
    create_personal_agent_context,
    create_work_agent_context,
)
from ttakkari.kb_library import register_text
import ttakkari.kb_library as lib
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.kb_ontology.canonicals import (
    synthesize_canonical_for_entity,
    update_superseded_assertions,
)
from ttakkari.kb_ontology.conflicts import detect_conflicts_for_entity
from ttakkari.kb_ontology.retrieval import find_matched_entities
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbEntity,
    KbEntityAlias,
    KbEntityCanonical,
)
from ttakkari.kb_pipeline import (
    KbPipelineOrchestrator,
    KbPipelineWorker,
    get_default_worker,
)
from ttakkari.kb_retrieval import search
from ttakkari.kb_store import ingest_source, originals_root, segment_revision


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    root = originals_root()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    s = get_settings()
    monkeypatch.setattr(s, "kb_semantic_enabled", False)
    monkeypatch.setattr(s, "kb_l2_adapter", "disabled")
    worker = get_default_worker()
    was_paused = worker.claiming_paused
    worker.pause_claiming()
    yield
    if not was_paused:
        worker.resume_claiming()
    if root.exists():
        shutil.rmtree(root)


def _worker() -> KbPipelineWorker:
    sf = sessionmaker()
    return KbPipelineWorker(
        session_factory=sf,
        orchestrator=KbPipelineOrchestrator(session_factory=sf, base_backoff_seconds=0.0),
    )


async def _drain(worker: KbPipelineWorker, rounds: int = 8) -> int:
    pumped = 0
    for _ in range(rounds):
        n = await worker.pump()
        if worker._tasks:
            await asyncio.gather(*list(worker._tasks))
        pumped += n
        if n == 0:
            break
    return pumped


async def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """CLI 는 자체 이벤트 루프·엔진을 쓰므로 자식 프로세스로 돌린다(블로킹 호출은 실행기 스레드에서)."""
    def run():
        return subprocess.run(
            [sys.executable, "-m", "ttakkari.cli", *args], env=os.environ.copy(),
            capture_output=True, text=True, timeout=120, check=False,
        )

    return await asyncio.get_running_loop().run_in_executor(None, run)


async def _register(text: str, *, scope: str = "personal", key: str | None = None, locator: str | None = None):
    async with sessionmaker()() as db:
        res = await register_text(
            db, title="문서", text=text, source_key=key, lang=None, locator=locator, scope=scope
        )
        await db.commit()
        return res


# ---------------------------------------------------------------------------
# 1. 접근 행이 없는 문서는 work_only 로 취급한다
# ---------------------------------------------------------------------------
UNCLASSIFIED_TEXT = "# Zebrafoo\n\nQuokkaword unclassified body about internal Zebrafoo roadmap.\n"


async def _ingest_without_access(key: str = "cli:unclassified") -> uuid.UUID:
    async with sessionmaker()() as db:
        res = await ingest_source(db, source_key=key, data=UNCLASSIFIED_TEXT.encode(), title="미분류", lang="en")
        await segment_revision(db, res.revision_id)
        await db.commit()
        assert await db.get(KbDocumentAccess, res.document_id) is None
        return res.document_id


async def test_document_without_access_row_is_invisible_to_personal_retrieval():
    doc_id = await _ingest_without_access()
    async with sessionmaker()() as db:
        personal = await search(db, query="Quokkaword", access_context=create_personal_agent_context(), mode="lexical")
        assert personal["items"] == []
        work = await search(db, query="Quokkaword", access_context=create_work_agent_context(), mode="lexical")
        assert {i["document"]["id"] for i in work["items"]} == {str(doc_id)}
        owner = await search(db, query="Quokkaword", access_context=create_owner_context(), mode="lexical")
        assert {i["document"]["id"] for i in owner["items"]} == {str(doc_id)}


async def test_document_without_access_row_is_invisible_to_personal_library_listing_and_search():
    from ttakkari import kb_library as lib

    doc_id = await _ingest_without_access()
    async with sessionmaker()() as db:
        listed = await lib.list_documents(db, q=None, limit=20, offset=0, scope="personal")
        assert str(doc_id) not in {d["id"] for d in listed["items"]}
        found = await lib.search_passages(
            db, query="Quokkaword", limit=10, offset=0, document_id=None, current_only=True, scope="personal"
        )
        assert found["items"] == []
        # 알 수 없는 것은 work_only 다. work 컨텍스트에서는 보인다.
        listed_work = await lib.list_documents(db, q=None, limit=20, offset=0, scope="work")
        assert str(doc_id) in {d["id"] for d in listed_work["items"]}


async def test_cli_kb_ingest_scope_flag_personal_is_global(tmp_path: Path):
    f = tmp_path / "note2.md"
    f.write_text("# CLI note\n\nCliquokkapersonal body text.\n", encoding="utf-8")
    proc = await _run_cli("kb-ingest", str(f), "--source-key", "file:cli-note-personal", "--scope", "personal")
    assert proc.returncode == 0, proc.stderr[-800:]
    doc_id = uuid.UUID(__import__("json").loads(proc.stdout.strip().splitlines()[-1])["document_id"])
    async with sessionmaker()() as db:
        row = await db.get(KbDocumentAccess, doc_id)
        assert row is not None and (row.scope, row.origin_scope, row.visibility) == ("personal", "personal", "global")


async def test_cli_kb_ingest_default_access_row_is_work_only(tmp_path: Path):
    f = tmp_path / "note.md"
    f.write_text("# CLI note\n\nCliquokka body text for ingestion.\n", encoding="utf-8")
    proc = await _run_cli("kb-ingest", str(f), "--source-key", "file:cli-note-default")
    assert proc.returncode == 0, proc.stderr[-800:]
    doc_id = uuid.UUID(__import__("json").loads(proc.stdout.strip().splitlines()[-1])["document_id"])
    async with sessionmaker()() as db:
        row = await db.get(KbDocumentAccess, doc_id)
        assert row is not None
        assert (row.scope, row.origin_scope, row.visibility) == ("work", "work", "work_only")
        personal = await search(db, query="Cliquokka", access_context=create_personal_agent_context(), mode="lexical")
        assert personal["items"] == []


async def test_research_work_scope_source_import_has_work_only_access_row(client, auth):
    r = await client.post(
        "/api/research/studies", headers=auth,
        json={"title": "work study", "goal": "g", "environment": {}, "criteria": ["c"], "scope": "work"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    src = await client.post(
        f"/api/research/studies/{sid}/sources", headers=auth,
        json={"source_key": "wk1", "title": "비공개 자료", "locator": "https://intra.example.com/doc",
              "body": "Researchquokka confidential finding body."},
    )
    assert src.status_code == 201, src.text
    doc_id = uuid.UUID(src.json()["revision"]["document_id"])
    async with sessionmaker()() as db:
        row = await db.get(KbDocumentAccess, doc_id)
        assert row is not None and (row.scope, row.origin_scope, row.visibility) == ("work", "work", "work_only")
        personal = await search(db, query="Researchquokka", access_context=create_personal_agent_context(), mode="lexical")
        assert personal["items"] == []


async def test_research_personal_scope_source_import_has_global_access_row(client, auth):
    r = await client.post(
        "/api/research/studies", headers=auth,
        json={"title": "personal study", "goal": "g", "environment": {}, "criteria": ["c"], "scope": "personal"},
    )
    sid = r.json()["id"]
    src = await client.post(
        f"/api/research/studies/{sid}/sources", headers=auth,
        json={"source_key": "pe1", "title": "공개 자료", "locator": "https://example.org/x",
              "body": "Researchquokkapublic finding body."},
    )
    assert src.status_code == 201, src.text
    doc_id = uuid.UUID(src.json()["revision"]["document_id"])
    async with sessionmaker()() as db:
        row = await db.get(KbDocumentAccess, doc_id)
        assert row is not None and (row.scope, row.origin_scope, row.visibility) == ("personal", "personal", "global")


# ---------------------------------------------------------------------------
# 2. L1 시드 매칭: 대문자 영어 이름
# ---------------------------------------------------------------------------
K8S_BODY = (
    "# Platform Notes\n\n"
    "We deploy every workload on Kubernetes clusters managed by the platform team and review upgrades monthly.\n"
)


async def test_l1_uppercase_english_name_becomes_verified_mention_and_korean_alias_matches():
    await _register(K8S_BODY)
    assert await _drain(_worker()) == 1
    async with sessionmaker()() as db:
        ent = await db.scalar(select(KbEntity).where(KbEntity.canonical_name == "Kubernetes"))
        assert ent is not None, "L1 이 대문자 Kubernetes 를 근거 없는 언급으로 버렸다"
        aliases = {a.alias: a.source for a in await db.scalars(select(KbEntityAlias).where(KbEntityAlias.entity_id == ent.id))}
        assert "쿠버네티스" in aliases and "k8s" in aliases, aliases
        for ctx in (create_owner_context(), create_personal_agent_context()):
            for q in ("쿠버네티스", "k8s"):
                matched = await find_matched_entities(db, q, terms(q), ctx)
                assert [(m["canonical_name"], m["matched_by"]) for m in matched] == [("Kubernetes", "alias")], q
                out = await search(db, query=q, access_context=ctx, mode="graph", limit=5)
                assert out["items"] and "Kubernetes" in out["items"][0]["snippet"], q
                # 한국어 본문이 없으므로 어휘 경로로는 못 찾는다: 위 결과는 별칭 경로가 만든 것이다
                lexical = await search(db, query=q, access_context=ctx, mode="lexical", limit=5)
                assert lexical["items"] == [], q


async def test_l1_extracted_aliases_without_source_occurrence_stay_forbidden():
    """불변식: 문서에서 배운(extracted) 별칭은 인가된 패시지 본문에 나타나야 한다. 시드 별칭은 면제."""
    await _register(K8S_BODY)
    assert await _drain(_worker()) == 1
    async with sessionmaker()() as db:
        bodies = " ".join(await db.scalars(select(KbPassage.body))).casefold()
        rows = (await db.execute(select(KbEntityAlias.alias, KbEntityAlias.source))).all()
        assert rows
        for alias, source in rows:
            if source == "extracted":
                assert alias.casefold() in bodies, alias


def test_l1_match_known_entities_is_case_insensitive_with_exact_original_quote():
    from ttakkari.kb_pipeline.alias_dictionary import load_seed_alias_dictionary
    from ttakkari.kb_pipeline.extract_l1 import match_known_entities
    from ttakkari.kb_pipeline.quote_verifier import verify_extraction_candidates

    body = "Teams run KUBERNETES in production, and Kubernetes upgrades are monthly. Docker images, ok."
    p = KbPassage(id=uuid.uuid4(), revision_id=uuid.uuid4(), ord=0, body=body, char_start=0, char_end=len(body), sha256="x")
    mentions = match_known_entities([p], load_seed_alias_dictionary())
    by_entity = {m.entity_name: m for m in mentions}
    assert "Kubernetes" in by_entity
    m = by_entity["Kubernetes"]
    assert m.evidence_quote == body[m.span_start:m.span_end] and m.evidence_quote.casefold() == "kubernetes"
    report = verify_extraction_candidates([p], [], [], mentions)
    assert "Kubernetes" in {x.entity_name for x in report.verified_mentions}
    # 단어 중간 부분 문자열은 언급이 아니다
    p2 = KbPassage(id=uuid.uuid4(), revision_id=uuid.uuid4(), ord=0, body="Googlekubernetesx is not a word here.",
                   char_start=0, char_end=37, sha256="y")
    assert not [m for m in match_known_entities([p2], load_seed_alias_dictionary()) if m.entity_name == "Kubernetes"]


# ---------------------------------------------------------------------------
# 3. 검색 필터와 표시
# ---------------------------------------------------------------------------
async def test_domain_filter_and_display_work_for_url_sources():
    res = await _register(
        "# Domain doc\n\nDomainquokka body text of the documentation page.\n",
        key="url:https://Docs.Example.org/guide/a", locator="https://Docs.Example.org/guide/a?x=1",
    )
    other = await _register("# Other\n\nDomainquokka another page elsewhere.\n", key="url:https://other.test/p", locator="https://other.test/p")
    async with sessionmaker()() as db:
        out = await search(db, query="Domainquokka", access_context=create_owner_context(), mode="lexical",
                           filters={"domain": "docs.example.org"})
        assert {i["document"]["id"] for i in out["items"]} == {str(res.document_id)}
        assert out["items"][0]["document"]["domain"] == "docs.example.org"
        both = await search(db, query="Domainquokka", access_context=create_owner_context(), mode="lexical")
        assert {i["document"]["id"] for i in both["items"]} == {str(res.document_id), str(other.document_id)}
        assert {i["document"]["domain"] for i in both["items"]} == {"docs.example.org", "other.test"}


async def test_domain_filter_derives_domain_for_existing_rows_without_source_meta():
    """수집 경로가 domain 을 쓰기 전의 기존 행(source_meta 에 domain 없음)도 locator 로 도출해 필터에 걸린다."""
    from sqlalchemy import text

    res = await _register(
        "# Legacy\n\nLegacyquokka page body.\n", key="url:https://legacy.example.net/p", locator="https://legacy.example.net/p"
    )
    async with sessionmaker()() as db:
        # 원장 불변 트리거는 UPDATE 를 거부하므로, 구 수집 경로가 남긴 행을 같은 DB 에서 트리거를 잠시 꺼 재현한다.
        await db.execute(text("ALTER TABLE kb_sources DISABLE TRIGGER USER"))
        await db.execute(text("UPDATE kb_sources SET source_meta = '{}'::jsonb"))
        await db.execute(text("ALTER TABLE kb_sources ENABLE TRIGGER USER"))
        await db.commit()
    async with sessionmaker()() as db:
        meta = await db.scalar(select(KbSource.source_meta).where(KbSource.locator == "https://legacy.example.net/p"))
        assert "domain" not in (meta or {})
        out = await search(db, query="Legacyquokka", access_context=create_owner_context(), mode="lexical",
                           filters={"domain": "legacy.example.net"})
        assert {i["document"]["id"] for i in out["items"]} == {str(res.document_id)}
        assert out["items"][0]["document"]["domain"] == "legacy.example.net"


async def test_status_historical_returns_only_non_current_revisions():
    key = "doc:history"
    r1 = await _register("# H\n\nHistoryquokka alpha first version text.\n", key=key)
    r2 = await _register("# H\n\nHistoryquokka beta second version text.\n", key=key)
    assert (r1.rev_no, r2.rev_no) == (1, 2) and r1.document_id == r2.document_id
    async with sessionmaker()() as db:
        ctx = create_owner_context()
        hist = await search(db, query="Historyquokka", access_context=ctx, mode="lexical", filters={"status": "historical"})
        assert [(i["revision"]["rev_no"], i["revision"]["current"]) for i in hist["items"]] == [(1, False)]
        cur = await search(db, query="Historyquokka", access_context=ctx, mode="lexical", filters={"status": "current"})
        assert [(i["revision"]["rev_no"], i["revision"]["current"]) for i in cur["items"]] == [(2, True)]
        hybrid = await search(db, query="Historyquokka", access_context=ctx, mode="hybrid", filters={"status": "historical"})
        assert [i["revision"]["rev_no"] for i in hybrid["items"]] == [1]
        # 기본(current_only) 은 현재만
        default = await search(db, query="Historyquokka", access_context=ctx, mode="lexical")
        assert [i["revision"]["rev_no"] for i in default["items"]] == [2]


CONFLICT_A = (
    "# Kubernetes Glossary\n\n"
    "Kubernetes refers to an open-source container orchestration system for automating deployment and scaling.\n\n"
    "## Operations\n\n"
    "Operators upgrade Kubernetes clusters monthly and review capacity every quarter.\n"
)
CONFLICT_B = "# Kubernetes Glossary (vendor sheet)\n\nKubernetes refers to a proprietary virtualization appliance sold as a single hardware bundle.\n"


async def test_has_conflict_marks_only_conflict_party_evidence_passages():
    a = await _register(CONFLICT_A)
    b = await _register(CONFLICT_B)
    assert await _drain(_worker()) == 2
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbConflict).where(KbConflict.status == "open")) == 1
        party = set()
        conflict = await db.scalar(select(KbConflict))
        for aid in (conflict.assertion_a_id, conflict.assertion_b_id):
            party |= set(await db.scalars(
                select(KbEvidence.passage_id).join(KbAssertionEvidence, KbAssertionEvidence.evidence_id == KbEvidence.id)
                .where(KbAssertionEvidence.assertion_id == aid)
            ))
        ctx = create_owner_context()
        out = await search(db, query="Kubernetes", access_context=ctx, mode="graph", limit=20)
        flagged = {i["passage_id"] for i in out["items"] if i["has_conflict"]}
        assert flagged and flagged == {str(p) for p in party} & {i["passage_id"] for i in out["items"]}
        unflagged = [i for i in out["items"] if not i["has_conflict"]]
        assert any(i["document"]["id"] == str(a.document_id) for i in unflagged), "충돌 당사자가 아닌 패시지에 has_conflict 가 붙었다"
        assert {str(a.document_id), str(b.document_id)} >= {i["document"]["id"] for i in out["items"]}


# ---------------------------------------------------------------------------
# 4. 현재성: rev_no 가 문서 안의 현재 포인터
# ---------------------------------------------------------------------------
async def _make_doc_with_claims(
    db, *, key: str, entity: KbEntity, revisions: list[tuple[int, str, datetime | None]], scope="personal",
    predicate="port", value_by_rev: dict[int, str] | None = None,
):
    """revisions: (rev_no, 본문, observation event_time|None). 각 리비전에 주장 1개와 근거를 직접 만든다."""
    vis = "global" if scope == "personal" else "work_only"
    sha = hashlib.sha256(key.encode()).hexdigest()
    src = KbSource(source_key=key, kind="test", locator=key, sha256=sha, byte_size=1, store_path=f"t/{sha}")
    doc = KbDocument(doc_key=key, title=key, lang="en")
    db.add_all([src, doc])
    await db.flush()
    db.add(KbDocumentAccess(document_id=doc.id, tenant_id="owner", scope=scope, origin_scope=scope, visibility=vis))
    out = {}
    prev_id = None
    for rev_no, body, event_time in revisions:
        bsha = hashlib.sha256(body.encode()).hexdigest()
        rev = KbRevision(document_id=doc.id, source_id=src.id, rev_no=rev_no, parser_version="t", body=body,
                         text_sha256=bsha, char_count=len(body), supersedes_id=prev_id)
        db.add(rev)
        await db.flush()
        prev_id = rev.id
        p = KbPassage(revision_id=rev.id, ord=0, body=body, char_start=0, char_end=len(body), sha256=bsha, token_estimate=3)
        db.add(p)
        await db.flush()
        ev = KbEvidence(passage_id=p.id, span_start=0, span_end=len(body), sha256=bsha)
        db.add(ev)
        await db.flush()
        value = (value_by_rev or {}).get(rev_no, body)
        a = KbAssertion(document_id=doc.id, revision_id=rev.id, origin_scope=scope, visibility=vis,
                        subject_entity_id=entity.id, predicate=predicate, object_value=value,
                        norm_object_value=value.casefold(), modality="fact", polarity="positive",
                        status="active", trust_score=0.9)
        db.add(a)
        await db.flush()
        db.add(KbAssertionEvidence(assertion_id=a.id, evidence_id=ev.id, relation="supports"))
        if event_time is not None:
            db.add(KbObservation(document_id=doc.id, revision_id=rev.id, envelope_id=uuid.uuid4(), event_time=event_time))
        await db.flush()
        out[rev_no] = (rev, a, ev)
    return doc, out


async def _entity(db, name="Valkey") -> KbEntity:
    e = KbEntity(tenant_id="personal", origin_scope="personal", visibility="global", canonical_name=name,
                 norm_name=name.casefold(), entity_kind="technology", domain="infra", status="active")
    db.add(e)
    await db.flush()
    return e


async def test_higher_rev_no_with_older_event_time_keeps_current_claims_and_canonical():
    """rev_no 가 크더라도 event_time 이 더 과거(오래된 스냅샷)이면 최신 관측 리비전이 현재다."""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    async with sessionmaker()() as db:
        ent = await _entity(db)
        doc, revs = await _make_doc_with_claims(
            db, key="doc:inversion", entity=ent,
            revisions=[(1, "port is 6379", now), (2, "port is 6380", now - timedelta(days=30))],
        )
        rev2, a2, _ev2 = revs[2]
        rev1, a1, _ev1 = revs[1]
        await update_superseded_assertions(db, rev2.id)
        await detect_conflicts_for_entity(db, ent.id)
        card = await synthesize_canonical_for_entity(db, ent.id)
        await db.flush()
        await db.refresh(a1)
        await db.refresh(a2)
        # 최신 관측인 rev1 의 주장이 active 로 유지되고, 과거 스냅샷인 rev2 의 주장이 superseded 된다.
        assert (a1.status, a2.status) == ("active", "superseded"), (a1.status, a2.status)
        assert card is not None and [f["assertion_id"] for f in card.key_facts] == [str(a1.id)]
        assert card.canonical_definition == "" and card.key_facts[0]["value"] == "port is 6379"
        # 검색 현재 패시지도 rev1 이다.
        hits = await lib.search_passages(db, query="port", limit=10, offset=0, document_id=None, current_only=True)
        assert len(hits["items"]) == 1
        assert hits["items"][0]["revision"]["id"] == str(rev1.id)
        await db.rollback()


async def test_counterexample_a_late_old_snapshot():
    """반례 (a): 늦게 도착한 오래된 스냅샷. 검색 현재 패시지와 정본 근거 리비전이 일치하고 정본이 비지 않는다."""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    async with sessionmaker()() as db:
        ent = await _entity(db, "PostgreSQL")
        doc, revs = await _make_doc_with_claims(
            db, key="doc:counter_a", entity=ent,
            revisions=[
                (1, "PostgreSQL port is 5432", now),
                (2, "PostgreSQL port is 5431", now - timedelta(days=60)),  # 늦게 수집된 과거 스냅샷
            ],
            predicate="port",
        )
        rev2, a2, _ev2 = revs[2]
        rev1, a1, _ev1 = revs[1]
        await update_superseded_assertions(db, rev2.id)
        await detect_conflicts_for_entity(db, ent.id)
        card = await synthesize_canonical_for_entity(db, ent.id)
        await db.flush()

        # 1. 정본이 비지 않고 최신 관측 rev1 을 가리킨다.
        assert card is not None
        assert len(card.key_facts) == 1
        assert card.key_facts[0]["assertion_id"] == str(a1.id)
        assert card.key_facts[0]["value"] == "PostgreSQL port is 5432"

        # 2. 검색 현재 패시지가 동일한 rev1 을 가리킨다.
        hits = await lib.search_passages(db, query="PostgreSQL", limit=10, offset=0, document_id=None, current_only=True)
        assert len(hits["items"]) == 1
        assert hits["items"][0]["revision"]["id"] == str(rev1.id)
        assert hits["items"][0]["revision"]["current"] is True

        # 3. 비현재(과거) 스냅샷은 current_only=False 일 때만 나온다.
        all_hits = await lib.search_passages(db, query="PostgreSQL", limit=10, offset=0, document_id=None, current_only=False)
        assert len(all_hits["items"]) == 2
        await db.rollback()


async def test_counterexample_b_aba_return():
    """반례 (b): A->B->A 복귀. 검색 현재 패시지와 정본 근거 리비전이 동일한 rev1 을 가리키고 정본이 비지 않는다."""
    start = datetime(2026, 10, 1, tzinfo=UTC)
    async with sessionmaker()() as db:
        ent = await _entity(db, "Elasticsearch")
        # 1. A 수집 (day 1)
        doc, revs = await _make_doc_with_claims(
            db, key="doc:counter_b", entity=ent,
            revisions=[(1, "Elasticsearch cluster port 9200", start + timedelta(days=1))],
            predicate="port",
        )
        rev1, a1, _ev1 = revs[1]
        await update_superseded_assertions(db, rev1.id)
        card1 = await synthesize_canonical_for_entity(db, ent.id)
        assert card1 is not None and card1.key_facts[0]["assertion_id"] == str(a1.id)

        # 2. B 수집 (day 2, 새 리비전 rev2: 9201)
        body2 = "Elasticsearch cluster port 9201"
        sha2 = hashlib.sha256(body2.encode()).hexdigest()
        src = await db.get(KbSource, rev1.source_id)
        rev2 = KbRevision(document_id=doc.id, source_id=src.id, rev_no=2, parser_version="t", body=body2,
                          text_sha256=sha2, char_count=len(body2), supersedes_id=rev1.id)
        db.add(rev2)
        await db.flush()
        p2 = KbPassage(revision_id=rev2.id, ord=0, body=body2, char_start=0, char_end=len(body2), sha256=sha2, token_estimate=3)
        db.add(p2)
        await db.flush()
        ev2 = KbEvidence(passage_id=p2.id, span_start=0, span_end=len(body2), sha256=sha2)
        db.add(ev2)
        await db.flush()
        a2 = KbAssertion(document_id=doc.id, revision_id=rev2.id, origin_scope="personal", visibility="global",
                         subject_entity_id=ent.id, predicate="port", object_value=body2,
                         norm_object_value=body2.casefold(), modality="fact", polarity="positive",
                         status="active", trust_score=0.9)
        db.add(a2)
        await db.flush()
        db.add(KbAssertionEvidence(assertion_id=a2.id, evidence_id=ev2.id, relation="supports"))
        db.add(KbObservation(document_id=doc.id, revision_id=rev2.id, envelope_id=uuid.uuid4(), event_time=start + timedelta(days=2)))
        await db.flush()
        await update_superseded_assertions(db, rev2.id)
        await db.refresh(a1)
        assert a1.status == "superseded" and a2.status == "active"
        card2 = await synthesize_canonical_for_entity(db, ent.id)
        assert card2 is not None and card2.key_facts[0]["assertion_id"] == str(a2.id)

        # 3. A 재관측 (day 3, 다시 rev1 재사용하여 관측 원장에 추가)
        db.add(KbObservation(document_id=doc.id, revision_id=rev1.id, envelope_id=uuid.uuid4(), event_time=start + timedelta(days=3)))
        await db.flush()
        await update_superseded_assertions(db, rev1.id)
        await db.refresh(a1)
        await db.refresh(a2)
        # a1 은 active 로 복원되고 a2 는 superseded 로 전환된다!
        assert (a1.status, a2.status) == ("active", "superseded")
        card3 = await synthesize_canonical_for_entity(db, ent.id)

        # 4. 검증: 정본 근거 리비전과 검색 현재 패시지가 모두 rev1 을 가리킨다.
        assert card3 is not None
        assert card3.key_facts[0]["assertion_id"] == str(a1.id)
        assert card3.key_facts[0]["value"] == "Elasticsearch cluster port 9200"

        hits = await lib.search_passages(db, query="Elasticsearch", limit=10, offset=0, document_id=None, current_only=True)
        assert len(hits["items"]) == 1
        assert hits["items"][0]["revision"]["id"] == str(rev1.id)
        assert hits["items"][0]["revision"]["current"] is True
        await db.rollback()


async def test_counterexample_c_higher_rev_no_older_event_time_inversion():
    """반례 (c): rev_no↑·event_time↓ 역전. 검색 현재 패시지와 정본 근거 리비전이 같고 정본이 비지 않는다."""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    async with sessionmaker()() as db:
        ent = await _entity(db, "RabbitMQ")
        doc, revs = await _make_doc_with_claims(
            db, key="doc:counter_c", entity=ent,
            revisions=[
                (1, "RabbitMQ port is 5672", now),
                (2, "RabbitMQ port is 5673", now - timedelta(days=10)),  # rev_no=2 가 더 크지만 event_time 은 과거
            ],
            predicate="port",
        )
        rev2, a2, _ev2 = revs[2]
        rev1, a1, _ev1 = revs[1]
        await update_superseded_assertions(db, rev2.id)
        await detect_conflicts_for_entity(db, ent.id)
        card = await synthesize_canonical_for_entity(db, ent.id)
        await db.flush()

        assert card is not None
        assert card.key_facts[0]["assertion_id"] == str(a1.id)
        assert card.key_facts[0]["value"] == "RabbitMQ port is 5672"

        hits = await lib.search_passages(db, query="RabbitMQ", limit=10, offset=0, document_id=None, current_only=True)
        assert len(hits["items"]) == 1
        assert hits["items"][0]["revision"]["id"] == str(rev1.id)
        await db.rollback()


async def test_conflict_closed_when_both_parties_superseded_is_not_resolved_both_valid():
    """충돌 당사자 둘 다 대체된 경우: resolved_both_valid 가 아닌 kept_separate 로 닫히고 사유가 남는다."""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    async with sessionmaker()() as db:
        ent = await _entity(db, "Cassandra")
        _da, ra = await _make_doc_with_claims(db, key="doc:c_both_a", entity=ent, revisions=[(1, "Cassandra port 9042", now)])
        _db, rb = await _make_doc_with_claims(db, key="doc:c_both_b", entity=ent, revisions=[(1, "Cassandra port 9043", now)])
        found = await detect_conflicts_for_entity(db, ent.id)
        await db.flush()
        assert len(found) == 1 and found[0].status == "open"
        _rev_b1, b1, _ = rb[1]
        _rev_a1, a1, _ = ra[1]

        # 두 당사자 모두 대체된다
        a1.status = "superseded"
        b1.status = "superseded"
        await db.flush()

        from ttakkari.kb_ontology.canonicals import _close_conflicts_of_superseded
        closed = await _close_conflicts_of_superseded(db, {a1.id, b1.id})
        await db.flush()

        assert len(closed) == 1
        conflict = await db.get(KbConflict, found[0].id)
        # 핵심 검증: resolved_both_valid 가 아니어야 한다!
        assert conflict.status != "resolved_both_valid", conflict.status
        assert conflict.status == "kept_separate"
        assert conflict.resolution_note == "both_parties_superseded_by_newer_revision"
        assert conflict.resolved_by == "system:revision_supersession"
        assert conflict.resolved_at is not None
        await db.rollback()


async def test_cross_document_comparison_still_uses_event_time_for_freshness_ordering():
    """문서 간 비교는 event_time 을 유지한다. 서로 다른 문서의 같은 술어 주장은 서로를 대체하지 않는다."""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    async with sessionmaker()() as db:
        ent = await _entity(db, "Redis")
        await _make_doc_with_claims(db, key="doc:x1", entity=ent, revisions=[(1, "port 6379", now)])
        await _make_doc_with_claims(db, key="doc:x2", entity=ent, revisions=[(1, "port 6379", now - timedelta(days=5))])
        await detect_conflicts_for_entity(db, ent.id)
        card = await synthesize_canonical_for_entity(db, ent.id)
        assert card is not None and len(card.source_document_ids) == 2
        await db.rollback()


async def test_conflict_is_not_left_open_after_a_correcting_revision_supersedes_a_party():
    now = datetime(2026, 10, 20, tzinfo=UTC)
    async with sessionmaker()() as db:
        ent = await _entity(db, "Memcached")
        _da, ra = await _make_doc_with_claims(db, key="doc:ca", entity=ent, revisions=[(1, "port 11211", now)])
        _db, rb = await _make_doc_with_claims(db, key="doc:cb", entity=ent, revisions=[(1, "port 11212", now)])
        found = await detect_conflicts_for_entity(db, ent.id)
        await db.flush()
        assert len(found) == 1 and found[0].status == "open"
        _rev_b1, b1, _ = rb[1]
        _rev_a1, a1, _ = ra[1]
        assert (a1.status, b1.status) == ("disputed", "disputed")
        # 문서 B 가 정정 개정(rev 2: 11211)으로 대체된다.
        doc_b = await db.get(KbDocument, _rev_b1.document_id)
        body2 = "port 11211"
        sha2 = hashlib.sha256(body2.encode()).hexdigest()
        rev2 = KbRevision(document_id=doc_b.id, source_id=_rev_b1.source_id, rev_no=2, parser_version="t", body=body2,
                          text_sha256=sha2, char_count=len(body2), supersedes_id=_rev_b1.id)
        db.add(rev2)
        await db.flush()
        p2 = KbPassage(revision_id=rev2.id, ord=0, body=body2, char_start=0, char_end=len(body2), sha256=sha2, token_estimate=2)
        db.add(p2)
        await db.flush()
        ev2 = KbEvidence(passage_id=p2.id, span_start=0, span_end=len(body2), sha256=sha2)
        db.add(ev2)
        await db.flush()
        b2 = KbAssertion(document_id=doc_b.id, revision_id=rev2.id, origin_scope="personal", visibility="global",
                         subject_entity_id=ent.id, predicate="port", object_value="port 11211", norm_object_value="port 11211",
                         modality="fact", polarity="positive", status="active", trust_score=0.9)
        db.add(b2)
        await db.flush()
        db.add(KbAssertionEvidence(assertion_id=b2.id, evidence_id=ev2.id, relation="supports"))
        db.add(KbObservation(document_id=doc_b.id, revision_id=rev2.id, envelope_id=uuid.uuid4(), event_time=now + timedelta(seconds=1)))
        await db.flush()

        await update_superseded_assertions(db, rev2.id)
        await detect_conflicts_for_entity(db, ent.id)
        card = await synthesize_canonical_for_entity(db, ent.id)
        await db.flush()
        conflict = await db.get(KbConflict, found[0].id)
        assert conflict.status != "open", conflict.status
        assert conflict.resolved_at is not None and conflict.resolved_by == "system:revision_supersession"
        open_count = await db.scalar(select(func.count()).select_from(KbConflict).where(KbConflict.status == "open"))
        assert open_count == 0
        await db.refresh(a1)
        await db.refresh(b1)
        assert (b1.status, a1.status) == ("superseded", "active")  # 살아남은 당사자는 disputed 에서 풀린다
        assert card is not None and card.has_open_conflict is False
        assert isinstance(card, KbEntityCanonical)
        await db.rollback()
