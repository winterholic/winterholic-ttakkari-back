"""백필 리허설: 0071 시점 데이터(scripts/kb_v3_legacy_seed_0071.py)를 head(0073)로 올린 임시 DB 에서 백필을 돌리고 API 로 확인한다.

사용 전제(임시 pgvector Docker DB 만, 운영·공유 DB 금지)
 1. 0071 체크아웃 가상환경으로 migrate → kb_v3_legacy_seed_0071.py 로 시드
 2. 이 저장소(head) 가상환경으로 migrate(0071→0073) 후 이 스크립트 실행
환경: TTAKKARI_DATABASE_URL(임시 DB), TTAKKARI_HOME(시드 때와 같은 폴더: 원본 blob 이 거기 있다),
      TTAKKARI_CONTEXT_EMBEDDER=hash, TTAKKARI_KB_SEMANTIC_ENABLED=true(해시 임베딩으로 실제 pgvector 행을 만든다)

단계: 사전 수치 → 원본 일부 유실 → dry-run → apply → 두 번째 apply(멱등) → 파이프라인 소진 → 사후 수치 → API 확인 → 롤백 → 재적용.
결과 JSON 을 --out 에 쓰고 요약을 출력한다. 하나라도 기대가 어긋나면 종료 코드 1 이다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.kb_backfill import rollback_backfill, run_backfill
from ttakkari.kb_pipeline import (
    KbPipelineOrchestrator,
    KbPipelineWorker,
    get_default_worker,
)
from ttakkari.kb_store import originals_root
from ttakkari.main import app
from ttakkari.security.auth import issue_access_token

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("PASS" if ok else "FAIL"), name, detail)


async def scalar(sql: str):
    async with sessionmaker()() as db:
        return await db.scalar(text(sql))


async def counts() -> dict:
    async with sessionmaker()() as db:
        out = {}
        for label, sql in {
            "documents": "select count(*) from kb_documents",
            "revisions": "select count(*) from kb_revisions",
            "passages": "select count(*) from kb_passages",
            "single_passage_current_documents": (
                "select count(*) from kb_documents d where (select count(*) from kb_passages p join kb_revisions r on r.id = p.revision_id "
                "where r.document_id = d.id and r.id = (select r2.id from kb_revisions r2 where r2.document_id = d.id order by r2.rev_no desc limit 1)) = 1 "
                "and (select r3.char_count from kb_revisions r3 where r3.document_id = d.id order by r3.rev_no desc limit 1) > 1200"),
            "pipeline_jobs": "select count(*) from kb_pipeline_jobs",
            "entities": "select count(*) from kb_entities",
            "aliases": "select count(*) from kb_entity_aliases",
            "assertions": "select count(*) from kb_assertions",
            "relations_active": "select count(*) from kb_relations where status = 'active'",
            "relations_candidate": "select count(*) from kb_relations where status = 'candidate'",
            "relations_active_without_evidence": "select count(*) from kb_relations where status = 'active' and evidence_count < 1",
            "canonicals": "select count(*) from kb_entity_canonicals",
            "conflicts_open": "select count(*) from kb_conflicts where status = 'open'",
            "vectors": "select count(*) from kb_passage_vectors",
            "observations": "select count(*) from kb_observations",
        }.items():
            out[label] = await db.scalar(text(sql))
        out["jobs_by_status"] = {s: n for s, n in (await db.execute(text("select status, count(*) from kb_pipeline_jobs group by status"))).all()}
        return out


async def drain(worker: KbPipelineWorker, rounds: int = 200) -> int:
    pumped = 0
    for _ in range(rounds):
        n = await worker.pump()
        if worker._tasks:
            await asyncio.gather(*list(worker._tasks))
        pumped += n
        if n == 0:
            break
    return pumped


async def main(args) -> int:
    report: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    default_worker = get_default_worker()
    default_worker.pause_claiming()
    sf = sessionmaker()
    worker = KbPipelineWorker(session_factory=sf, orchestrator=KbPipelineOrchestrator(session_factory=sf, base_backoff_seconds=0.0))

    report["before"] = await counts()
    print("BEFORE", json.dumps(report["before"], ensure_ascii=False))
    check("전제: 통합 0074 head", await scalar("select version_num from alembic_version") == "0074_kb_v3_research_merge")
    check("전제: 시드는 36건, 1200자 넘는 통짜 패시지 문서 16건", report["before"]["documents"] == 36 and report["before"]["single_passage_current_documents"] == 16,
          f"documents={report['before']['documents']} single_big={report['before']['single_passage_current_documents']}")

    # 원본 일부 유실(원본 blob 이 없으면 헤딩 구조를 복구할 수 없다는 경로를 확인)
    async with sf() as db:
        paths = [r[0] for r in (await db.execute(text(
            "select s.store_path from kb_sources s where s.source_key like 'collector:%' order by s.created_at limit :n"), {"n": args.lose_originals})).all()]
    for rel in paths:
        (originals_root() / rel).unlink()
    report["originals_removed"] = len(paths)

    report_dir = Path(args.report_dir)
    t0 = time.time()
    dry = await run_backfill(sf, apply=False, batch_size=args.batch_size, report_dir=report_dir)
    report["dry_run"] = dry.as_dict() | {"seconds": round(time.time() - t0, 2)}
    print("DRY", json.dumps(report["dry_run"], ensure_ascii=False))
    after_dry = await counts()
    check("dry-run 은 아무것도 쓰지 않는다", after_dry == report["before"])

    t0 = time.time()
    applied = await run_backfill(sf, apply=True, batch_size=args.batch_size, report_dir=report_dir)
    report["apply"] = applied.as_dict() | {"seconds": round(time.time() - t0, 2)}
    print("APPLY", json.dumps(report["apply"], ensure_ascii=False))
    check("apply 완료·오류 없음", applied.status == "completed" and not applied.errors)
    check("dry-run 계획과 apply 결과의 행동 분포가 같다", dict(dry.by_action) == dict(applied.by_action))
    mid = await counts()
    again = await run_backfill(sf, apply=True, batch_size=args.batch_size, report_dir=report_dir)
    report["second_apply"] = again.as_dict()
    check("두 번째 apply 는 아무 행도 만들지 않는다(멱등)", await counts() == mid)

    t0 = time.time()
    pumped = await drain(worker)
    report["pipeline_seconds"] = round(time.time() - t0, 2)
    report["pipeline_jobs_processed"] = pumped
    report["after"] = await counts()
    print("AFTER", json.dumps(report["after"], ensure_ascii=False))
    a = report["after"]
    check("모든 작업이 끝났다(ready/degraded/held 만)", set(a["jobs_by_status"]) <= {"ready", "degraded", "held"}, str(a["jobs_by_status"]))
    hidden = await scalar("select count(*) from kb_document_access where hidden")
    check("통짜 패시지 문서가 해소됐다(숨김 처리된 문서는 건너뛰므로 제외)", a["single_passage_current_documents"] <= hidden, f"remaining={a['single_passage_current_documents']} hidden_skipped={hidden}")
    check("엔터티·주장·관계가 생겼다", a["entities"] > 0 and a["assertions"] > 0 and a["relations_active"] > 0, f"entities={a['entities']} assertions={a['assertions']} relations_active={a['relations_active']}")
    check("근거 없는 active 관계 0건", a["relations_active_without_evidence"] == 0)

    # ---- API 확인(웹 세션 = 통합 도서관) ----
    token, _ = issue_access_token()
    h = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        api: dict = {}
        summary = (await c.get("/api/assistant/kb/summary", headers=h)).json()
        api["summary_counts"] = summary["counts"]
        api["summary_pipeline"] = summary["pipeline"]
        api["summary_v3"] = summary["v3"]
        check("summary: v3 지표가 있고 통짜 패시지 0", summary["v3"]["single_passage_documents"] == 0 and summary["counts"]["entities"] > 0, str(summary["v3"]))
        docs = (await c.get("/api/assistant/kb/documents", headers=h, params={"limit": 100})).json()
        api["web_visible_documents"] = docs["total"]
        check("웹 통합 목록에 work_only 가 없다", docs["total"] == await scalar(
            "select count(*) from kb_documents d where exists (select 1 from kb_document_access a where a.document_id = d.id and a.visibility = 'global' and not a.hidden)"))
        s = (await c.get("/api/assistant/kb/search", headers=h, params={"q": "디플로이먼트", "mode": "hybrid", "limit": 10})).json()
        api["search"] = {"items": len(s["items"]), "coverage": s["coverage"], "matched_entities": [e["name"] for e in s["matched_entities"]],
                         "paths": sorted({p for i in s["items"] for p in i["match_paths"]}), "has_section_path": any(i["section_path"] for i in s["items"])}
        check("검색: 항목·정본 카드·구조 위치가 나온다", s["items"] and s["matched_entities"] and api["search"]["has_section_path"], json.dumps(api["search"], ensure_ascii=False))
        deployment = next((e for e in s["matched_entities"] if e["name"] == "Deployment"), None)
        check("검색: 한국어 질의가 Deployment 정본에 닿는다(별칭)", deployment is not None)
        doc_id = s["items"][0]["document"]["id"]
        d = (await c.get(f"/api/assistant/kb/documents/{doc_id}", headers=h)).json()
        api["document_detail"] = {"title": d["title"], "revisions": len(d["revisions"]), "passages": d["passages"]["total"], "entities": len(d["entities"]),
                                  "assertions": len(d["assertions"]), "relations": len(d["relations"]), "history": len(d["history"]),
                                  "pipeline_status": d["pipeline_status"], "domain": d["domain"]}
        check("문서 상세: 요약·개념·주장·근거·버전·처리 이력", d["summary"] and d["entities"] and d["assertions"] and d["history"] and d["revisions"], json.dumps(api["document_detail"], ensure_ascii=False))
        graph = (await c.get("/api/assistant/kb/ontology/graph", headers=h, params={"limit": 30})).json()
        api["graph_hubs"] = {"nodes": len(graph["nodes"]), "edges": len(graph["edges"]), "total_entities": graph["total_entities"]}
        check("그래프 허브: 노드와 active 에지가 비어 있지 않다", graph["nodes"] and graph["edges"], str(api["graph_hubs"]))
        hub = next((n for n in graph["nodes"] if n["name"] == "Deployment"), graph["nodes"][0])
        nb = (await c.get("/api/assistant/kb/ontology/graph", headers=h, params={"center_entity_id": hub["id"]})).json()
        api["graph_1hop"] = {"center": hub["name"], "nodes": len(nb["nodes"]), "edges": len(nb["edges"]), "predicates": sorted({e["predicate"] for e in nb["edges"]}),
                             "all_active": all(e["status"] == "active" for e in nb["edges"])}
        check("그래프 1-hop: 자동 승격된 active 관계로 비어 있지 않다", nb["edges"] and api["graph_1hop"]["all_active"] and all(e["evidence"] for e in nb["edges"]), str(api["graph_1hop"]))
        cand = (await c.get("/api/assistant/kb/ontology/graph", headers=h, params={"center_entity_id": hub["id"], "include_candidates": "true"})).json()
        api["graph_1hop_with_candidates"] = {"edges": len(cand["edges"]), "candidate_edges": sum(1 for e in cand["edges"] if e["candidate"])}
        jobs = (await c.get("/api/assistant/kb/jobs", headers=h, params={"limit": 100})).json()
        api["jobs"] = {"total": jobs["total"], "by_status": {s: sum(1 for j in jobs["items"] if j["status"] == s) for s in {j["status"] for j in jobs["items"]}}}
        check("jobs 목록: 6단계 상태·메트릭", jobs["items"] and all(len(j["stages"]) == 6 for j in jobs["items"]) and jobs["items"][0]["metrics"].get("passage_count") is not None)
        victim = next(j for j in jobs["items"] if j["status"] in ("degraded", "ready"))
        r1 = (await c.post(f"/api/assistant/kb/jobs/{victim['id']}/retry", headers=h, json={"from_stage": "indexing"})).json()
        check("retry(from_stage): 재큐잉되고 앞 단계는 유지", r1["retry"]["requeued"] and r1["current_stage"] == "indexing" and r1["stages"][0]["status"] == "succeeded")
        await drain(worker)
        r2 = (await c.get(f"/api/assistant/kb/jobs/{victim['id']}", headers=h)).json()
        check("retry 후 작업이 다시 끝난다", r2["status"] in ("ready", "degraded"), r2["status"])
        conflicts = (await c.get("/api/assistant/kb/conflicts", headers=h)).json()
        api["conflicts_open"] = conflicts["total"]
        report["api"] = api

    # ---- 롤백과 재적용 ----
    plan = await rollback_backfill(sf, run_id=applied.run_id, apply=False)
    done = await rollback_backfill(sf, run_id=applied.run_id, apply=True)
    report["rollback"] = {"planned": len(plan["reverted"]), "reverted": len(done["reverted"]), "skipped": len(done["skipped"])}
    cur_rev = await scalar(
        "select count(*) from (select d.id, (select r.rev_no from kb_revisions r where r.id = coalesce("
        "(select o.revision_id from kb_observations o where o.document_id = d.id order by o.event_time desc, o.recorded_at desc, o.id desc limit 1),"
        "(select r2.id from kb_revisions r2 where r2.document_id = d.id order by r2.rev_no desc limit 1))) rev_no from kb_documents d) x where rev_no > 1")
    check("롤백: 현재 리비전이 모두 원래(1)로 돌아갔다", cur_rev == 0, f"remaining_new_current={cur_rev}")
    check("롤백: 원장 행은 지워지지 않았다", await scalar("select count(*) from kb_revisions") == report["after"]["revisions"])
    re = await run_backfill(sf, apply=True, batch_size=args.batch_size, report_dir=report_dir)
    check("롤백 후 재적용: 새 리비전을 다시 만들지 않고 현재로 되돌린다", await scalar("select count(*) from kb_revisions") == report["after"]["revisions"] and not re.errors)
    await drain(worker)

    report["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]
    failed = [c for c in report["checks"] if not c["ok"]]
    report["failed"] = len(failed)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\nTOTAL {len(checks)} FAILED {len(failed)} -> {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--report-dir", required=True)
    ap.add_argument("--batch-size", type=int, default=5)
    ap.add_argument("--lose-originals", type=int, default=2)
    sys.exit(asyncio.run(main(ap.parse_args())))
