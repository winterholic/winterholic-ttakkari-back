"""KB3-103 백필기: 분류, dry-run 무변경, 멱등, 재개, 중단, 오류 격리, 동시 실행 차단, 롤백. 실 PostgreSQL 에서 돈다.

0071 시점 데이터의 모양은 0071 의 쓰기 방식으로 재현한다: 수집기 문서는 본문 전체가 패시지 하나(헤딩이 평문 줄),
업로드 문서는 빈 줄 문단별 패시지(section_path 없음), 접근 행은 scope 만 넘긴 행.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from test_kb_v3_integration_flow import (  # noqa: F401 - _env 는 autouse 픽스처
    _drain,
    _env,
    _worker,
)

from ttakkari.collector.control_models import KbDocumentAccess, KbObservation
from ttakkari.db import sessionmaker
from ttakkari.kb_backfill import (
    BACKFILL_PARSER_VERSION,
    ENQUEUE_ONLY,
    RECHUNK_HTML,
    RECHUNK_SAME_BODY,
    SKIP_DONE,
    SKIP_HIDDEN,
    SKIP_UNCLASSIFIED,
    BackfillAlreadyRunning,
    rollback_backfill,
    run_backfill,
)
from ttakkari.kb_library import _latest_revision
from ttakkari.kb_models import KbDocument, KbPassage, KbRevision
from ttakkari.kb_ontology_models import KbAssertion, KbEntity
from ttakkari.kb_pipeline import enqueue_pipeline_job
from ttakkari.kb_pipeline_models import KbPipelineJob
from ttakkari.kb_store import ingest_source, segment_revision, sha256_hex
from ttakkari.models import utcnow

HTML = (
    "<html><head><title>디플로이먼트</title></head><body>"
    "<h1>디플로이먼트</h1><p>디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다. "
    "디플로이먼트는 레플리카셋을 관리한다.</p>"
    "<h2>롤링 업데이트</h2><p>롤링 업데이트는 파드를 점진적으로 교체하는 배포 전략으로 가용성을 유지한다. kubectl rollout status 로 진행을 확인한다.</p>"
    "</body></html>"
)
FLAT = "\n".join(["디플로이먼트 | 문서", "개요", "디플로이먼트(Deployment)는 선언적 업데이트를 제공하는 리소스이다."] + [f"줄 {i} 쿠버네티스 설명 문장입니다." for i in range(60)])
UPLOAD = "# 파드 정리\n\n파드(Pod)는 배포할 수 있는 가장 작은 단위이다.\n\n## 주의\n\n파드를 삭제하면 컨테이너도 종료된다."


async def _single_passage(db, revision_id, body):
    """0071 수집기 모양: 본문 전체가 패시지 하나."""
    await db.execute(insert(KbPassage).values(
        id=uuid.uuid4(), revision_id=revision_id, ord=0, heading=None, body=body, char_start=0, char_end=len(body),
        sha256=sha256_hex(body.encode()), created_at=utcnow(),
    ))


async def _legacy_doc(db, key, body, *, kind="web", media="text/html", parser="trafilatura-2.0.0+parse-1", raw=None,
                      scope="personal", observe=True, passages="single", hidden=False, access=True, day=0):
    result = await ingest_source(
        db, source_key=key, data=(raw if raw is not None else body.encode()), kind=kind, locator=f"https://k8s.example/{key}",
        media_type=media, title=key, lang="ko", parser_version=parser, normalized_body=body,
        collected_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=day),
    )
    if access:
        db.add(KbDocumentAccess(document_id=result.document_id, tenant_id="owner", scope=scope, hidden=hidden))
    if passages == "single":
        await _single_passage(db, result.revision_id, body)
    elif passages == "paragraphs":
        pos = 0
        for ordinal, para in enumerate(p for p in body.split("\n\n") if p.strip()):
            start = body.index(para, pos)
            pos = start + len(para)
            await db.execute(insert(KbPassage).values(
                id=uuid.uuid4(), revision_id=result.revision_id, ord=ordinal, heading=None, body=para, char_start=start,
                char_end=pos, sha256=sha256_hex(para.encode()), created_at=utcnow()))
    elif passages == "structured":
        await segment_revision(db, result.revision_id)
    if observe:
        db.add(KbObservation(document_id=result.document_id, revision_id=result.revision_id, envelope_id=uuid.uuid4(),
                             event_time=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=day), provenance={"seed": "0071"}))
    await db.flush()
    return result


@pytest.fixture
async def corpus():
    """0071 모양의 기존 문서 9건. 문서 id 를 이름으로 돌려준다."""
    ids: dict[str, uuid.UUID] = {}
    async with sessionmaker()() as db:
        # A: 레거시 수집기 HTML(원본 바이트가 있고 헤딩은 평문으로 잃었다) → 원본에서 헤딩 구조 복구
        ids["html"] = (await _legacy_doc(db, "collector:html", FLAT, raw=HTML.encode(), day=1)).document_id
        # B: 수집기 문서인데 원본 파일이 사라졌다(헤딩 복구 불가) → 본문 그대로 구조 청킹
        r = await _legacy_doc(db, "collector:noraw", FLAT, media="text/plain", day=2)
        ids["flat"] = r.document_id
        # C: 업로드 문단 패시지(구조 없음), 관측 없음
        ids["upload"] = (await _legacy_doc(db, "upload:para", UPLOAD, kind="upload", media="text/plain", parser="raw-v1",
                                           passages="paragraphs", observe=False, day=3)).document_id
        # D: 이미 구조 패시지 + 작업 있음 → 건드리지 않는다
        r = await _legacy_doc(db, "upload:done", UPLOAD + "\n\n끝.", kind="upload", media="text/plain", parser="raw-v1",
                              passages="structured", observe=False, day=4)
        await enqueue_pipeline_job(db, document_id=r.document_id, revision_id=r.revision_id)
        ids["done"] = r.document_id
        # E: 구조 패시지는 있는데 작업이 없다
        ids["nojob"] = (await _legacy_doc(db, "upload:nojob", UPLOAD + "\n\n잡 없음.", kind="upload", media="text/plain",
                                          parser="raw-v1", passages="structured", observe=False, day=5)).document_id
        # F: 패시지가 하나도 없다
        ids["nopassage"] = (await _legacy_doc(db, "upload:nopassage", UPLOAD + "\n\n패시지 없음.", kind="upload", media="text/plain",
                                              parser="raw-v1", passages="none", observe=False, day=6)).document_id
        # G: 수집 정책 철회로 숨김
        ids["hidden"] = (await _legacy_doc(db, "collector:hidden", FLAT, media="text/plain", hidden=True, day=7)).document_id
        # H: 접근 행 없음(레거시 미분류)
        ids["legacy"] = (await _legacy_doc(db, "upload:legacy", UPLOAD + "\n\n미분류.", kind="upload", media="text/plain", parser="raw-v1",
                                           passages="paragraphs", observe=False, access=False, day=8)).document_id
        # I: 회사 범위 문서
        ids["work"] = (await _legacy_doc(db, "work:upload", UPLOAD + "\n\n회사.", kind="upload", media="text/plain", parser="raw-v1",
                                         passages="paragraphs", scope="work", observe=False, day=9)).document_id
        await db.commit()
    # B 의 원본 파일을 지운다(원본 유실). 같은 바이트의 다른 문서가 없도록 FLAT 본문 해시 파일만 제거.
    from ttakkari.kb_store import originals_root

    async with sessionmaker()() as db:
        path = await db.scalar(text("select store_path from kb_sources where source_key = 'collector:noraw'"))
    (originals_root() / path).unlink()
    return ids


async def _snapshot():
    async with sessionmaker()() as db:
        return {
            t: await db.scalar(text(f"select count(*) from {t}"))
            for t in ("kb_documents", "kb_revisions", "kb_passages", "kb_pipeline_jobs", "kb_observations", "kb_document_access", "kb_sources")
        }


async def _run(**kw):
    return await run_backfill(sessionmaker(), **kw)


async def test_dry_run_classifies_everything_and_writes_nothing(corpus, tmp_path):
    before = await _snapshot()
    s = await _run(apply=False, batch_size=4, report_dir=tmp_path)
    assert await _snapshot() == before
    assert s.status == "completed" and s.examined == 9 and s.batches >= 3
    a = s.by_action
    assert a[RECHUNK_HTML] == 1 and a[RECHUNK_SAME_BODY] == 3  # html(복구) / flat·upload·work 는 텍스트만
    assert a[SKIP_DONE] == 1 and a[ENQUEUE_ONLY] == 2 and a[SKIP_HIDDEN] == 1 and a[SKIP_UNCLASSIFIED] == 1
    assert s.single_passage_before == 2 and s.single_passage_after == 0  # 통짜 패시지 문서(HTML·flat)가 모두 해소된다
    state = (tmp_path / s.run_id / "state.json").read_text()
    assert '"apply": false' in state
    rows = [__import__("json").loads(line) for line in (tmp_path / s.run_id / "progress.jsonl").read_text().splitlines()]
    assert len(rows) == 9 and {r["action"] for r in rows} >= {RECHUNK_HTML, SKIP_UNCLASSIFIED}


async def test_apply_creates_structured_revisions_and_jobs_is_idempotent_and_keeps_ledger(corpus, tmp_path):
    s = await _run(apply=True, batch_size=3, report_dir=tmp_path)
    assert s.status == "completed" and not s.errors
    async with sessionmaker()() as db:
        html = await db.get(KbDocument, corpus["html"])
        cur = await _latest_revision(db, html.id)
        assert cur.rev_no == 2 and cur.parser_version.startswith("trafilatura-") and cur.parser_version.endswith("+parse-2")
        old = await db.scalar(select(KbRevision).where(KbRevision.document_id == html.id, KbRevision.rev_no == 1))
        assert cur.supersedes_id == old.id and old.body == FLAT, "이전 리비전과 그 패시지는 그대로 남는다(원장 불변)"
        assert "# 디플로이먼트" in cur.body or "## 롤링 업데이트" in cur.body, "헤딩 구조가 원본 HTML 에서 복구됐다"
        passages = list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == cur.id).order_by(KbPassage.ord)))
        assert len(passages) >= 2 and any(p.section_path for p in passages)
        assert await db.scalar(select(func.count()).select_from(KbPassage).where(KbPassage.revision_id == old.id)) == 1
        job = await db.scalar(select(KbPipelineJob).where(KbPipelineJob.revision_id == cur.id))
        assert job is not None and job.status == "registered"
        # 관측: 새 리비전이 현재다(event_time 이 이전 관측보다 뒤)
        obs = list(await db.scalars(select(KbObservation).where(KbObservation.document_id == html.id).order_by(KbObservation.event_time)))
        assert obs[-1].revision_id == cur.id and obs[-1].provenance["backfill"] == s.run_id

        flat = await _latest_revision(db, corpus["flat"])
        assert flat.parser_version == BACKFILL_PARSER_VERSION and flat.rev_no == 2
        flat_passages = list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == flat.id)))
        assert len(flat_passages) > 1 and all(len(p.body) <= 1200 for p in flat_passages)
        # 관측이 없던 문서에도 관측이 생겨 새 리비전이 현재다(롤백이 같은 방식으로 되돌릴 수 있다)
        upload = await _latest_revision(db, corpus["upload"])
        assert upload.rev_no == 2 and upload.parser_version == BACKFILL_PARSER_VERSION
        assert await db.scalar(select(KbObservation.id).where(KbObservation.document_id == corpus["upload"]))
        assert any(p.section_path for p in await db.scalars(select(KbPassage).where(KbPassage.revision_id == upload.id)))
        # enqueue_only: 같은 리비전에 작업만 붙었고 패시지가 없던 문서는 그 자리에서 구조 패시지가 생겼다
        nojob = await _latest_revision(db, corpus["nojob"])
        assert nojob.rev_no == 1 and await db.scalar(select(KbPipelineJob.id).where(KbPipelineJob.revision_id == nojob.id))
        nop = await _latest_revision(db, corpus["nopassage"])
        assert nop.rev_no == 1 and await db.scalar(select(func.count()).select_from(KbPassage).where(KbPassage.revision_id == nop.id)) >= 1
        # 건너뛴 문서는 아무것도 바뀌지 않는다
        for key in ("hidden", "legacy"):
            assert (await _latest_revision(db, corpus[key])).rev_no == 1
            assert await db.scalar(select(KbPipelineJob.id).where(KbPipelineJob.document_id == corpus[key])) is None
        assert (await _latest_revision(db, corpus["done"])).rev_no == 1

    after_first = await _snapshot()
    again = await _run(apply=True, batch_size=3, report_dir=tmp_path)
    assert await _snapshot() == after_first, "두 번째 실행은 아무 행도 만들지 않는다(멱등)"
    assert again.by_action[SKIP_DONE] == 7 and sum(again.by_action[a] for a in (RECHUNK_HTML, RECHUNK_SAME_BODY, ENQUEUE_ONLY)) == 0


async def test_pipeline_processes_backfilled_revisions_and_search_uses_the_new_structure(corpus, tmp_path):
    await _run(apply=True, report_dir=tmp_path)
    await _drain(_worker(), rounds=20)
    async with sessionmaker()() as db:
        jobs = list(await db.scalars(select(KbPipelineJob)))
        assert jobs and all(j.status in ("ready", "degraded") for j in jobs), [(j.status, j.hold_reason) for j in jobs]
        assert await db.scalar(select(func.count()).select_from(KbEntity)) >= 2
        assert await db.scalar(select(func.count()).select_from(KbAssertion)) >= 1
        # 회사 문서에서 나온 파생 지식은 work_only 로 격리된다(백필이 가시성을 넓히지 않는다)
        work_job = await db.scalar(select(KbPipelineJob).where(KbPipelineJob.document_id == corpus["work"]))
        assert work_job is not None
        assert not await db.scalar(
            select(func.count()).select_from(KbAssertion).where(KbAssertion.document_id == corpus["work"], KbAssertion.visibility == "global"))
        # 숨김·미분류 문서에서는 지식이 만들어지지 않았다
        assert not await db.scalar(select(func.count()).select_from(KbAssertion).where(KbAssertion.document_id.in_([corpus["hidden"], corpus["legacy"]])))


async def test_classify_legacy_makes_the_current_exposure_explicit_and_include_hidden_processes_hidden(corpus, tmp_path):
    s = await _run(apply=True, classify_legacy=True, include_hidden=True, report_dir=tmp_path)
    assert s.by_action[SKIP_UNCLASSIFIED] == 0 and s.by_action[SKIP_HIDDEN] == 0
    async with sessionmaker()() as db:
        row = await db.get(KbDocumentAccess, corpus["legacy"])
        # 미상 문서는 노출 확대 방지를 위해 work_only 로 안전 격리된다
        assert (row.scope, row.origin_scope, row.visibility, row.hidden) == ("work", "work", "work_only", False)
        assert (await db.get(KbDocumentAccess, corpus["hidden"])).hidden is True, "숨김 상태는 풀리지 않는다"


async def test_classify_legacy_does_not_expose_unprovenanced_documents(tmp_path):
    """--classify-legacy 는 출처 근거가 없는 문서를 personal/global 로 노출하지 않고 work_only 로 격리한다.

    근거가 확실한 research:personal 이나 collector personal 만 personal/global 로 인정한다.
    """
    async with sessionmaker()() as db:
        # 1. 미상 업로드 문서 (출처 근거 없음) -> work_only 격리
        res_unknown = await _legacy_doc(db, "upload:unknown_corp", "Confidential company data", kind="upload",
                                        access=False, observe=False)
        # 2. research:personal 문서 (근거 있음) -> personal/global
        res_res_p = await _legacy_doc(db, "research:personal:my_note", "My personal note", kind="web",
                                      access=False, observe=False)
        # 3. research:work 문서 -> work_only
        res_res_w = await _legacy_doc(db, "research:work:project_x", "Work project spec", kind="web",
                                      access=False, observe=False)
        # 4. collector personal 문서 (origin_meta 에 명시적 scope) -> personal/global
        res_col_p = await ingest_source(
            db, source_key="collector:personal_doc", data=b"# P\n\nPersonal blog post\n", kind="web",
            locator="https://blog.me/post", media_type="text/html", title="blog", lang="ko",
            parser_version="raw-v1", normalized_body="# P\n\nPersonal blog post\n",
            origin_meta={"via": "collector", "scope": "personal"},
        )
        await _single_passage(db, res_col_p.revision_id, "# P\n\nPersonal blog post\n")
        await db.commit()

    summary = await _run(apply=True, classify_legacy=True, report_dir=tmp_path)
    assert summary.by_action[SKIP_UNCLASSIFIED] == 0

    async with sessionmaker()() as db:
        acc_unknown = await db.get(KbDocumentAccess, res_unknown.document_id)
        assert (acc_unknown.scope, acc_unknown.origin_scope, acc_unknown.visibility) == ("work", "work", "work_only"), \
            "근거 없는 미상 문서는 회사자료 보호를 위해 work_only 로 격리되어야 한다"

        acc_res_p = await db.get(KbDocumentAccess, res_res_p.document_id)
        assert (acc_res_p.scope, acc_res_p.origin_scope, acc_res_p.visibility) == ("personal", "personal", "global"), \
            "research:personal 은 personal/global 로 허용된다"

        acc_res_w = await db.get(KbDocumentAccess, res_res_w.document_id)
        assert (acc_res_w.scope, acc_res_w.origin_scope, acc_res_w.visibility) == ("work", "work", "work_only")

        acc_col_p = await db.get(KbDocumentAccess, res_col_p.document_id)
        assert (acc_col_p.scope, acc_col_p.origin_scope, acc_col_p.visibility) == ("personal", "personal", "global"), \
            "collector personal 은 personal/global 로 허용된다"


async def test_limit_resume_and_stop_event_continue_where_they_left_off(corpus, tmp_path):
    first = await _run(apply=True, batch_size=2, limit=4, report_dir=tmp_path)
    assert first.status == "limit_reached" and first.examined == 4 and first.cursor
    partial = await _snapshot()
    rest = await _run(apply=True, batch_size=2, report_dir=tmp_path, run_id=first.run_id)
    assert rest.status == "completed"
    full = await _snapshot()
    assert full["kb_revisions"] > partial["kb_revisions"] or full["kb_pipeline_jobs"] > partial["kb_pipeline_jobs"]
    assert (await _run(apply=True, report_dir=tmp_path)).by_action[RECHUNK_SAME_BODY] == 0

    # 중단 신호: 현재 문서를 끝내고 커밋한 뒤 멈춘다
    stop = asyncio.Event()
    stop.set()
    stopped = await _run(apply=False, stop=stop)
    assert stopped.status == "stopped" and stopped.examined == 0


async def test_a_failing_document_is_isolated_reported_and_retried_next_run(corpus, tmp_path, monkeypatch):
    from ttakkari import kb_backfill

    real = kb_backfill._create_revision

    async def flaky(db, doc, prev, plan):
        if doc.id == corpus["upload"]:
            raise RuntimeError("boom")
        return await real(db, doc, prev, plan)

    monkeypatch.setattr(kb_backfill, "_create_revision", flaky)
    s = await _run(apply=True, batch_size=5, report_dir=tmp_path)
    assert s.status == "completed" and len(s.errors) == 1 and s.errors[0]["document_id"] == str(corpus["upload"])
    async with sessionmaker()() as db:
        assert (await _latest_revision(db, corpus["upload"])).rev_no == 1, "실패한 문서는 savepoint 로 되돌아갔다"
        assert (await _latest_revision(db, corpus["html"])).rev_no == 2, "다른 문서는 처리됐다"
    monkeypatch.setattr(kb_backfill, "_create_revision", real)
    retry = await _run(apply=True, report_dir=tmp_path)
    assert retry.by_action[RECHUNK_SAME_BODY] == 1 and not retry.errors  # 실패했던 upload 만 남았다


async def test_second_backfill_cannot_run_concurrently(corpus):
    async with sessionmaker()() as holder:
        assert await holder.scalar(text("select pg_try_advisory_lock(hashtextextended('kb3-backfill', 0))"))
        with pytest.raises(BackfillAlreadyRunning):
            await _run(apply=True)
        # dry-run 은 락이 필요 없다
        assert (await _run(apply=False)).examined == 9
        await holder.execute(text("select pg_advisory_unlock(hashtextextended('kb3-backfill', 0))"))


async def test_rollback_restores_previous_revisions_without_deleting_ledger(corpus, tmp_path):
    s = await _run(apply=True, report_dir=tmp_path)
    await _drain(_worker(), rounds=20)
    before = await _snapshot()
    plan = await rollback_backfill(sessionmaker(), run_id=s.run_id, apply=False)
    assert len(plan["reverted"]) == 4 and await _snapshot() == before  # 새 리비전을 쌓은 문서 넷(html·flat·upload·work)을 되돌린다는 계획
    done = await rollback_backfill(sessionmaker(), run_id=s.run_id, apply=True)
    assert len(done["reverted"]) == 4
    async with sessionmaker()() as db:
        for key in ("html", "flat", "upload", "work"):
            cur = await _latest_revision(db, corpus[key])
            assert cur.rev_no == 1, f"{key}: 이전 리비전이 다시 현재다"
            # 되돌린 리비전의 지식은 현재 지식에서 빠진다
            newer = await db.scalar(select(KbRevision).where(KbRevision.document_id == corpus[key], KbRevision.rev_no == 2))
            assert newer is not None, "원장의 새 리비전은 지우지 않는다"
            assert not await db.scalar(select(func.count()).select_from(KbAssertion).where(
                KbAssertion.revision_id == newer.id, KbAssertion.status == "active"))
    after = await _snapshot()
    assert after["kb_revisions"] == before["kb_revisions"] and after["kb_passages"] == before["kb_passages"]
    assert after["kb_observations"] == before["kb_observations"] + 4
    again = await rollback_backfill(sessionmaker(), run_id=s.run_id, apply=True)
    assert again["reverted"] == [] and len(again["skipped"]) == 4, "두 번 되돌리지 않는다(최신 관측이 롤백 관측이다)"
    # 되돌린 뒤 다시 백필하면 같은 새 리비전을 다시 현재로 만든다(재사용, 중복 리비전 없음)
    revisions_before = (await _snapshot())["kb_revisions"]
    await _run(apply=True, report_dir=tmp_path)
    assert (await _snapshot())["kb_revisions"] == revisions_before
    async with sessionmaker()() as db:
        assert (await _latest_revision(db, corpus["html"])).rev_no == 2


async def test_small_legacy_html_is_not_left_held_even_when_its_plain_text_is_a_single_planned_chunk(tmp_path):
    """평문이 우연히 한 청크라 패시지 경계가 계획과 같아도, 레거시 수집기 HTML 은 새 리비전이 필요하다
    (그대로 작업만 붙이면 parsing 이 collector_html_structure_revision_required 로 영구 held 가 된다)."""
    small_html = "<html><body><h1>파드</h1><p>파드(Pod)는 쿠버네티스에서 배포할 수 있는 가장 작은 단위이다. 컨테이너를 하나 이상 담는다.</p></body></html>"
    small_flat = "파드\n파드(Pod)는 쿠버네티스에서 배포할 수 있는 가장 작은 단위이다. 컨테이너를 하나 이상 담는다."
    async with sessionmaker()() as db:
        recoverable = await _legacy_doc(db, "collector:small", small_flat, raw=small_html.encode(), day=1)
        lost = await _legacy_doc(db, "collector:small-lost", small_flat + " 두 번째.", raw=small_html.encode() + b"<!-- other -->", day=2)
        await db.commit()
    from ttakkari.kb_store import originals_root

    async with sessionmaker()() as db:
        path = await db.scalar(text("select store_path from kb_sources where source_key = 'collector:small-lost'"))
    (originals_root() / path).unlink()

    dry = await _run(apply=False)
    assert dry.by_action == {RECHUNK_HTML: 1, RECHUNK_SAME_BODY: 1}
    await _run(apply=True)
    await _drain(_worker(), rounds=20)
    async with sessionmaker()() as db:
        jobs = {j.document_id: j for j in await db.scalars(select(KbPipelineJob))}
        assert set(jobs) == {recoverable.document_id, lost.document_id}
        # 새 리비전에 붙은 작업은 held 가 아니다(원본이 있으면 +parse-2 로, 없으면 본문 그대로 구조 청킹한 리비전으로 지식화)
        assert all(j.status in ("ready", "degraded") for j in jobs.values()), {str(k): (v.status, v.hold_reason) for k, v in jobs.items()}
        lost_rev = await _latest_revision(db, lost.document_id)
        assert lost_rev.parser_version == BACKFILL_PARSER_VERSION


async def test_enqueue_jobs_backfill_dry_run_and_idempotent_apply(corpus, tmp_path):
    from ttakkari.kb_backfill import run_enqueue_jobs_backfill

    # 1) dry-run: 쓰기 없음
    dry = await run_enqueue_jobs_backfill(sessionmaker(), apply=False, batch_size=3, report_dir=tmp_path)
    # corpus 9건 중: done 1건(job 있음), hidden 1건(스킵) -> 7건 계획
    assert dry.apply is False
    assert dry.examined == 9
    assert dry.skipped_has_job == 1
    assert dry.skipped_hidden == 1
    assert dry.enqueued == 7

    async with sessionmaker()() as db:
        # DB 에 새 작업이 생기지 않았음을 확인
        jobs_count = await db.scalar(select(func.count()).select_from(KbPipelineJob))
        assert jobs_count == 1  # corpus[done] 에 원래 있던 1건뿐

    # 2) apply: 실제 작업 큐잉
    done = await run_enqueue_jobs_backfill(sessionmaker(), apply=True, batch_size=3, report_dir=tmp_path)
    assert done.apply is True
    assert done.enqueued == 7
    assert done.skipped_has_job == 1
    assert done.skipped_hidden == 1

    async with sessionmaker()() as db:
        jobs_count_after = await db.scalar(select(func.count()).select_from(KbPipelineJob))
        assert jobs_count_after == 8  # 1 + 7

    # 3) 2회차 apply: 완전한 멱등성 (새 작업 0건)
    again = await run_enqueue_jobs_backfill(sessionmaker(), apply=True, batch_size=3, report_dir=tmp_path)
    assert again.enqueued == 0
    assert again.skipped_has_job == 8
    assert again.skipped_hidden == 1

    async with sessionmaker()() as db:
        jobs_count_final = await db.scalar(select(func.count()).select_from(KbPipelineJob))
        assert jobs_count_final == 8


async def test_enqueue_jobs_backfill_preserves_work_only_visibility_and_processes(tmp_path):
    from ttakkari.kb_backfill import run_enqueue_jobs_backfill

    async with sessionmaker()() as db:
        work_doc = await _legacy_doc(
            db, "upload:company_confidential", "# 사내 비밀\n\n대외비 프로젝트 명세입니다.",
            kind="upload", media="text/plain", parser="raw-v1", passages="structured",
            observe=False, scope="work", day=1,
        )
        await db.commit()

    # 작업 등록 백필 실행
    res = await run_enqueue_jobs_backfill(sessionmaker(), apply=True, report_dir=tmp_path)
    assert res.enqueued >= 1

    # work_only 접근 행이 유지되었는지 검증
    async with sessionmaker()() as db:
        access = await db.get(KbDocumentAccess, work_doc.document_id)
        assert access is not None
        assert access.scope == "work"
        assert access.visibility == "work_only"

        job = await db.scalar(select(KbPipelineJob).where(KbPipelineJob.document_id == work_doc.document_id))
        assert job is not None
        assert job.status in ("registered", "ready", "degraded")

    # 워커로 소진 후 추출된 엔터티/주장의 가시성이 work_only 로 보존되는지 검증
    await _drain(_worker(), rounds=10)
    async with sessionmaker()() as db:
        for entity in await db.scalars(select(KbEntity).where(KbEntity.tenant_id == "work")):
            assert entity.visibility == "work_only"

