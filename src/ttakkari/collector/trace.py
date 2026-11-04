"""What a mission is doing, read from what it has actually stored.

The screen needs three answers: what the owner asked, how much of it is met, and where each document went.
Nothing here is recorded separately; every row is derived from the durable tables (jobs, items, deliveries,
the KB pipeline), so it cannot disagree with them and costs no extra writes on the collection path.
A requirement's state comes from evidence counts, never from a model saying "done".
"""
from __future__ import annotations

import uuid
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select

from ttakkari.collector import catalog, missions
from ttakkari.collector_models import CollectionMissionJob, CollectorItem, CollectorJob
from ttakkari.kb_pipeline.types import PIPELINE_STAGES
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage

EVENT_LIMIT = 300
DISCARD_LIMIT = 150

GRADE_LABEL = {
    "standard": "공식 표준", "research": "연구·이론", "vendor": "기업·기관 권장", "practice": "경험·실무 글",
}
_GRADE_OF_KIND = {"standard": "standard", "ux_research": "research", "design_system": "vendor",
                  "enterprise_ui": "vendor", "public_sector": "vendor"}
_RESEARCH_HOSTS = ("arxiv.org", "doi.org", "acm.org", "springer.com", "wiley.com", "sciencedirect.com", "ieee.org", "semanticscholar.org")


def evidence_grade(url: str) -> str:
    """How much weight a source carries, from where it came. Collector classification, not a factual verdict."""
    host = (urlsplit(url).hostname or "").lower()
    target = catalog.target_for_host(host)
    if target:
        return _GRADE_OF_KIND.get(target.kind, "vendor")
    if host.endswith((".gov", ".gov.uk", "w3.org")):
        return "standard"
    if any(host == h or host.endswith("." + h) for h in _RESEARCH_HOSTS):
        return "research"
    return "practice"


def _state_of_coverage(entry: dict[str, Any]) -> str:
    return {"covered": "met", "thin": "partial", "unavailable": "blocked", "sources_found": "active",
            "no_candidates": "empty", "unexplored": "pending"}.get(entry.get("state", "unexplored"), "pending")


async def ontology_progress(db, document_ids: list[str]) -> dict[str, Any]:
    """Per-stage status of the KB's own knowledge pipeline for the documents this mission delivered."""
    ids = [uuid.UUID(value) for value in document_ids if value]
    stages = {name: Counter() for name in PIPELINE_STAGES}
    if ids:
        rows = (await db.execute(
            select(KbPipelineStage.stage, KbPipelineStage.status, func.count())
            .join(KbPipelineJob, KbPipelineJob.id == KbPipelineStage.job_id)
            .where(KbPipelineJob.document_id.in_(ids)).group_by(KbPipelineStage.stage, KbPipelineStage.status))).all()
        for stage, status, count in rows:
            stages.setdefault(stage, Counter())[status] += count
    return {"documents": len(ids), "stages": [{"stage": name, "states": dict(stages[name])} for name in PIPELINE_STAGES]}


def _rules(view: dict[str, Any], ontology: dict[str, Any], jobs: list[CollectorJob]) -> list[dict[str, Any]]:
    counts = view["counts"]
    imported = counts.get("imported", 0)
    failed_jobs = [job for job in jobs if job.status == "failed"]
    unexplained = [job for job in failed_jobs if not (job.last_error or {}).get("message") and not (job.last_error or {}).get("code")]
    budget = view["plan"]["budget"]
    used = sum(run.get("pages_used", 0) for run in view.get("runs") or [] if run.get("run_seq") == view["run_seq"])
    sem = {s["stage"]: s["states"] for s in ontology["stages"]}
    extracted = sum(sem.get("extracting", {}).get(state, 0) for state in ("succeeded", "degraded"))
    triage = view.get("triage") or {}
    return [
        {"key": "library", "label": "수집한 자료를 지식 도서관에 저장", "status": "met" if imported else "pending",
         "detail": f"적재 {imported}건"},
        {"key": "source", "label": "원문 출처(URL)를 함께 보존", "status": "met" if imported and all(r.get("url") for r in view["results"] if r.get("document_id")) else "pending",
         "detail": "적재된 자료마다 출처 URL이 연결돼 있는지 확인"},
        {"key": "judge", "label": "경량 모델이 품질을 판별하고 불확실한 자료는 보류", "status": "met" if (triage.get("accepted") or counts.get("held")) else "pending",
         "detail": f"통과 {triage.get('accepted', 0)} · 보류 {counts.get('held', 0)} · 모델 성능 {'검증됨' if (triage.get('policy') or {}).get('verified') else '미검증(보수 모드)'}"},
        {"key": "failures", "label": "수집 실패와 접근 제한을 숨기지 않고 기록", "status": "unmet" if unexplained else ("met" if (failed_jobs or view["gaps"] or counts.get("blocked")) else "pending"),
         "detail": f"실패 작업 {len(failed_jobs)}건(원인 미기록 {len(unexplained)}) · 막힌 출처 {len(view['gaps'])}곳"},
        {"key": "budget", "label": "무관한 페이지를 무제한 수집하지 않음(예산 준수)", "status": "met" if used <= budget["pages"] else "unmet",
         "detail": f"이번 회차 {used}/{budget['pages']}쪽"},
        {"key": "ontology", "label": "온톨로지(개념·주장·엔터티) 추출", "status": "met" if extracted else "pending",
         "detail": (f"추출 단계 통과 {extracted}/{ontology['documents']}건" if ontology["documents"] else "적재된 문서 없음")},
    ]


def _stage_rows(view: dict[str, Any], ontology: dict[str, Any]) -> list[dict[str, Any]]:
    counts = view["counts"]
    plan = view["plan"]
    searches = len(plan.get("search_history") or [])
    sem = {s["stage"]: s["states"] for s in ontology["stages"]}
    done = sum(sem.get("canonicalizing", {}).get(state, 0) for state in ("succeeded", "degraded"))
    return [
        {"key": "discover", "label": "탐색", "count": len(plan.get("sources") or []), "detail": f"출처 후보 · 검색 {searches}회"},
        {"key": "acquire", "label": "획득", "count": counts.get("acquired", 0), "detail": f"접근 제한 {counts.get('blocked', 0)} · 실패 {counts.get('failed', 0)}"},
        {"key": "judge", "label": "판별", "count": counts.get("accepted", 0), "detail": f"보류 {counts.get('held', 0)} · 중복 {counts.get('duplicate', 0)}"},
        {"key": "load", "label": "도서관 적재", "count": counts.get("imported", 0), "detail": f"전달 대기 {counts.get('pending_delivery', 0)}"},
        {"key": "ontology", "label": "온톨로지", "count": done, "detail": f"대상 {ontology['documents']}건"},
    ]


async def build(db, mission) -> dict[str, Any]:
    view = await missions.view(db, mission)
    document_ids = [row["document_id"] for row in view["results"] if row.get("document_id")]
    ontology = await ontology_progress(db, document_ids)
    job_rows = (await db.execute(select(CollectorJob, CollectionMissionJob.run_seq).join(
        CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id)
        .where(CollectionMissionJob.mission_id == mission.id, CollectorJob.tenant_id == mission.tenant_id)
        .order_by(CollectorJob.created_at))).all()
    jobs = [job for job, _ in job_rows]
    plan = view["plan"]
    coverage = (view.get("research") or {}).get("coverage") or plan.get("coverage") or []
    requirements = [{"group": "target" if str(c.get("key", "")).startswith("t:") else "topic", "key": c.get("key"), "label": c["topic"],
                     "status": _state_of_coverage(c), "documents": c.get("documents", 0), "sources": c.get("sources", 0),
                     "kind": catalog.KIND_LABEL.get(c.get("kind", ""), "")} for c in coverage]

    events: list[dict[str, Any]] = []
    for index, entry in enumerate(plan.get("search_history") or []):
        events.append({"at": None, "order": index, "stage": "discover", "kind": "search",
                       "subject": entry.get("query"), "message": f"검색 {entry.get('status', 'ok')} · 후보 {entry.get('candidates', 0)} · 새 출처 {entry.get('new_sources', 0)}"
                       + (f" · {entry['error']}" if entry.get("error") else "")})
    for job, run_seq in job_rows:
        error = job.last_error or {}
        message = f"{job.stage} 작업 {job.status}" + (f" · {error['code']}" if error.get("code") else "")
        if error.get("message"):
            message += f" · {error['message']}"
        events.append({"at": job.updated_at.isoformat(), "order": 0, "stage": "acquire", "kind": "job", "subject": job.source_key,
                       "message": message, "run_seq": run_seq, "failed": job.status == "failed", "where": error.get("where")})
    items = (await db.execute(
        select(CollectorItem.id, CollectorItem.url, CollectorItem.status, CollectorItem.reason_codes, CollectorItem.created_at,
               CollectorItem.http_status, CollectorItem.text_chars,
               CollectorItem.envelope["extensions"]["collector:page"]["title"].astext.label("title"))
        .join(CollectionMissionJob, CollectionMissionJob.job_id == CollectorItem.job_id)
        .where(CollectionMissionJob.mission_id == mission.id, CollectorItem.tenant_id == mission.tenant_id, CollectorItem.kind == "document")
        .order_by(CollectorItem.created_at.desc()).limit(EVENT_LIMIT))).all()
    discarded = []
    for item in items:
        reasons = list(item.reason_codes or [])
        events.append({"at": item.created_at.isoformat(), "order": 0, "stage": "judge" if item.status in ("accepted", "held") else "acquire",
                       "kind": "item", "subject": item.url, "status": item.status,
                       "message": f"{item.status}" + (f" · {', '.join(reasons[:3])}" if reasons else "") + (f" · {item.text_chars}자" if item.text_chars else "")})
        if item.status in ("held", "failed", "blocked") and len(discarded) < DISCARD_LIMIT:
            discarded.append({"item_id": str(item.id), "url": item.url, "title": item.title or "", "status": item.status, "reasons": reasons,
                              "http_status": item.http_status, "grade": evidence_grade(item.url)})
    for gap in view["gaps"]:
        events.append({"at": None, "order": 10_000, "stage": "acquire", "kind": "gap", "subject": gap.get("url"),
                       "message": f"{gap['reason']} · {gap.get('message', '')}".strip(" ·")})
    events.sort(key=lambda e: (e["at"] is None, e["at"] or "", e["order"]), reverse=True)

    grades = Counter(evidence_grade(row["url"]) for row in view["results"] if row.get("document_id"))
    return {
        "id": view["id"], "title": view["title"], "goal": view["goal"], "status": view["status"], "run_seq": view["run_seq"],
        "targets": plan.get("targets") or [],
        "requirements": requirements, "rules": _rules(view, ontology, jobs), "stages": _stage_rows(view, ontology),
        "ontology": ontology,
        "jobs": {"total": len(jobs), "failed": sum(job.status == "failed" for job in jobs),
                 "running": sum(job.status == "running" for job in jobs), "queued": sum(job.status in ("queued", "retry_wait") for job in jobs)},
        "grades": {GRADE_LABEL[key]: grades.get(key, 0) for key in GRADE_LABEL},
        "events": events[:EVENT_LIMIT], "discarded": discarded,
    }
