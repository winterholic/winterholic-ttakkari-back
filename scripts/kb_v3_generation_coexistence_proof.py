"""구 세대(0071) 코드가 0072/0073 스키마 위에서 계속 쓰고 읽을 수 있는지 실DB로 증명한다.

세대 배포에서 새 세대 마이그레이션은 구 세대가 서비스 중일 때 온라인으로 적용된다(deploy/generation-contract.json 의
online_migration_from). 따라서 0072/0073 이 적용된 DB 에 구 세대 코드가 쓰는 모든 DML 이 통과해야 한다.
이 스크립트는 0071 체크아웃의 가상환경으로 돌리고, 0071 에 있는 API 만 쓴다(KbDocumentAccess 에 origin_scope/visibility 를
넘기지 않는 구 writer 포함). 임시 DB 에만 쓴다.

출력은 항목별 PASS/FAIL 이고, 하나라도 FAIL 이면 종료 코드 1 이다.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from ttakkari import kb_library as lib
from ttakkari.collector.control_models import KbDocumentAccess, KbObservation
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument, KbPassage, KbRevision
from ttakkari.kb_store import ingest_source, segment_revision

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, detail)


async def attempt(name: str, fn) -> None:
    try:
        detail = await fn()
        record(name, True, str(detail or ""))
    except Exception as exc:  # noqa: BLE001 - 증명 스크립트는 어떤 실패든 보고한다
        record(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")


async def main() -> int:
    tag = uuid.uuid4().hex[:8]

    async def old_register(scope: str):
        async with sessionmaker()() as db:
            res = await lib.register_text(
                db, title=f"구세대 {scope} {tag}", text=f"구 세대 코드가 쓴 {scope} 문서 {tag}\n\n두 번째 문단 {tag}",
                source_key=None, lang="ko", locator=None, scope=scope,
            )
            await db.commit()
            scope_, origin, vis = (await db.execute(text(
                "select scope, origin_scope, visibility from kb_document_access where document_id = :d"), {"d": res.document_id})).one()
            expected = ("personal", "global") if scope == "personal" else ("work", "work_only")
            if (origin, vis) != expected:
                raise AssertionError(f"파생값 {origin}/{vis} != {expected}")
            return f"scope={scope_} origin_scope={origin} visibility={vis} passages={res.passage_count}"

    await attempt("0071 register_text(personal) → 접근 행 트리거 파생", lambda: old_register("personal"))
    await attempt("0071 register_text(work) → 접근 행 트리거 파생", lambda: old_register("work"))

    async def old_collector_shape():
        # kb_delivery.deliver_item 과 같은 DML 순서: ingest_source → KbDocumentAccess(scope만) → segment → observation
        async with sessionmaker()() as db:
            text_body = "\n".join(f"줄 {i} 구 세대 수집기 문서 {tag}" for i in range(40))
            res = await ingest_source(
                db, source_key=f"collector:coexist:{tag}", data=text_body.encode(), kind="web",
                locator=f"https://example.test/{tag}", media_type="text/html", title=f"수집 {tag}", lang="ko",
                parser_version="trafilatura-2.0.0+parse-1", normalized_body=text_body, origin_meta={"via": "collector"},
            )
            db.add(KbDocumentAccess(document_id=res.document_id, tenant_id="owner", scope="personal"))
            await segment_revision(db, res.revision_id)
            db.add(KbObservation(document_id=res.document_id, revision_id=res.revision_id, envelope_id=uuid.uuid4(),
                                 event_time=datetime.now(UTC), provenance={"proof": "0071"}))
            await db.commit()
            vis = (await db.execute(text("select visibility from kb_document_access where document_id = :d"),
                                    {"d": res.document_id})).scalar_one()
            if vis != "global":
                raise AssertionError(f"personal 수집 행이 {vis} 로 파생됨")
            return f"visibility={vis}"

    await attempt("0071 수집 배달 모양 쓰기(source/doc/rev/passage/access/observation)", old_collector_shape)

    async def old_rerevision_of_new_generation_doc():
        # 새 세대가 파이프라인 잡을 붙인 문서에 구 세대가 새 리비전을 쌓는다(잡 FK RESTRICT 는 삽입에 영향이 없어야 한다).
        async with sessionmaker()() as db:
            doc_id = await db.scalar(
                select(KbDocument.id).where(text("exists (select 1 from kb_pipeline_jobs j where j.document_id = kb_documents.id)"))
                .limit(1)
            )
            if doc_id is None:
                return "파이프라인 잡이 붙은 문서 없음(건너뜀: 새 세대 처리 전 상태)"
            doc = await db.get(KbDocument, doc_id)
            res = await ingest_source(
                db, source_key=doc.doc_key, data=f"새 리비전 {tag}".encode(), kind="upload", media_type="text/plain",
                title=doc.title, lang="ko",
            )
            await segment_revision(db, res.revision_id)
            await db.commit()
            return f"rev_no={res.rev_no}"

    await attempt("0071 이 잡 있는 문서에 새 리비전 쓰기", old_rerevision_of_new_generation_doc)

    async def old_hide_and_show():
        async with sessionmaker()() as db:
            doc_ids = list(await db.scalars(select(KbDocumentAccess.document_id).where(
                KbDocumentAccess.scope == "personal").limit(3)))
            await db.execute(update(KbDocumentAccess).where(KbDocumentAccess.document_id.in_(doc_ids)).values(hidden=True))
            row = await db.get(KbDocumentAccess, doc_ids[0])
            row.hidden = False
            await db.commit()
            return f"updated={len(doc_ids)}"

    await attempt("0071 접근 행 hidden UPDATE(collect_control 모양)", old_hide_and_show)

    async def old_reads():
        async with sessionmaker()() as db:
            out = []
            for scope in ("personal", "work"):
                summary = await lib.summary(db, scope=scope)
                listing = await lib.list_documents(db, q=None, limit=100, offset=0, scope=scope)
                hits = await lib.search_passages(db, query="쿠버네티스", limit=5, offset=0, document_id=None,
                                                 current_only=True, scope=scope)
                first = listing["items"][0]["id"] if listing["items"] else None
                if first:
                    await lib.get_document(db, uuid.UUID(first), revision_id=None, passage_limit=5, passage_offset=0,
                                           scope=scope)
                out.append(f"{scope}: docs={summary['counts']['documents']} list={listing['total']} hits={len(hits['items'])}")
            return "; ".join(out)

    await attempt("0071 읽기(summary/list/search/get_document) 양 범위", old_reads)

    async def old_reject_work_global():
        # 구 코드가 범위 위반 행을 만들 수 없는지(트리거가 거부) — 실패해야 PASS 가 아니다: 구 코드는 visibility 를 넘기지 않으므로
        # work 범위 행은 항상 work_only 로 파생되어야 한다.
        async with sessionmaker()() as db:
            res = await ingest_source(db, source_key=f"work:coexist:{tag}", data=f"회사 {tag}".encode(), kind="upload",
                                      title="회사", lang="ko")
            db.add(KbDocumentAccess(document_id=res.document_id, tenant_id="owner", scope="work"))
            await db.commit()
            origin, vis = (await db.execute(text(
                "select origin_scope, visibility from kb_document_access where document_id = :d"), {"d": res.document_id})).one()
            if (origin, vis) != ("work", "work_only"):
                raise AssertionError(f"work 행이 {origin}/{vis} 로 파생됨")
            return f"origin_scope={origin} visibility={vis}"

    await attempt("0071 work 접근 행은 work_only 로만 파생(누출 없음)", old_reject_work_global)

    async def old_evidence():
        from ttakkari.kb_models import KbEvidence

        async with sessionmaker()() as db:
            passage = await db.scalar(select(KbPassage).where(text("length(body) > 20")).order_by(KbPassage.created_at.desc()).limit(1))
            # 재실행해도 겹치지 않도록 아직 없는 구간을 고른다.
            for start in range(0, min(10, len(passage.body) - 10)):
                taken = await db.scalar(select(KbEvidence.id).where(
                    KbEvidence.passage_id == passage.id, KbEvidence.span_start == start, KbEvidence.span_end == start + 10))
                if taken is None:
                    break
            else:
                return "이 패시지의 후보 구간이 모두 사용됨(건너뜀)"
            db.add(KbEvidence(passage_id=passage.id, span_start=start, span_end=start + 10,
                              sha256=__import__("hashlib").sha256(passage.body[start:start + 10].encode()).hexdigest(),
                              note=f"proof {tag}"))
            await db.commit()
            return "evidence insert ok"

    await attempt("0071 KbEvidence 삽입(불변 트리거 유지)", old_evidence)

    failed = [r for r in results if not r[1]]
    print(f"\nTOTAL {len(results)} FAILED {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except DBAPIError as exc:  # pragma: no cover
        print("FATAL", exc)
        code = 2
    sys.exit(code)
