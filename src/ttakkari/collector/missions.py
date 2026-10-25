"""Intent-first collection over the existing acquisition and KB delivery contracts.

Plans describe intended coverage, never evidence of completed research. URL routing is
deterministic. Unregistered rights remain gaps; no client text can grant source rights.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on

from ttakkari.collector import control, store
from ttakkari.collector.contracts import AuthContext, CollectorConflictError, jcs
from ttakkari.collector.control_models import CollectorDelivery, CollectorSource
from ttakkari.collector.netguard import normalize_url
from ttakkari.collector_models import (
    CollectionMission,
    CollectionMissionJob,
    CollectorItem,
    CollectorJob,
    CollectorOutbox,
)

URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}\"']+")
TERMINAL = {"succeeded", "partial", "failed", "cancelled", "policy_blocked", "quarantined"}


def input_url(raw: str):
    # Parsing may retain a nonstandard public port. Only the server source policy and
    # DNS-pinned fetcher decide whether it can actually be contacted.
    port = urlsplit(raw).port
    return normalize_url(raw, allowed_schemes=("http", "https"), allowed_ports=(port,) if port else (80, 443))


def plan(goal: str, *, context: str = "", urls: list[str] | None = None,
         max_pages: int = 200, max_depth: int = 3) -> dict:
    if not goal.strip() or "\x00" in goal + context:
        raise ValueError("수집 목적을 입력해 주세요.")
    if len(goal) > 20_000 or len(context) > 500_000:
        raise ValueError("입력이 너무 깁니다.")
    if not 1 <= max_pages <= 100_000 or not 0 <= max_depth <= 12:
        raise ValueError("수집 범위를 확인해 주세요.")
    found = [*(urls or []), *URL_PATTERN.findall(goal), *URL_PATTERN.findall(context)]
    sources = []
    seen = set()
    for raw in found:
        # Natural-language punctuation is not part of a copied reference URL.
        url = input_url(raw.rstrip(".,;:!?)）。，")).url
        if url in seen:
            continue
        seen.add(url)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        adapter = ("github" if host == "github.com" and len(parsed.path.strip("/").split("/")) >= 2
                   else "paper_pdf" if parsed.path.lower().endswith(".pdf") or host in {"arxiv.org", "doi.org"}
                   else "website")
        sources.append({"url": url, "adapter": adapter, "provenance": "user_reference"})
    if len(sources) > 100:
        raise ValueError("한 임무에는 참고 주소를 100개까지 넣을 수 있습니다.")
    topic = URL_PATTERN.sub("", goal).strip()[:300]
    research = not sources
    dimensions = ["핵심 이론·용어", "논문·원전", "구현·실무 사례", "설계 패턴·한계"]
    return {
        "version": 1, "title": goal.strip().splitlines()[0][:100],
        "mode": "research" if research else "mixed" if context else "sources", "goal": goal.strip(),
        "sources": sources,
        "context": {"text": context, "provenance": "user_provided", "fact_check_status": "unverified"},
        "queries": [f"{topic} {suffix}" for suffix in ("이론 논문", "research paper", "open source implementation", "patterns case study")] if research else [],
        "coverage": [{"topic": name, "state": "unexplored"} for name in dimensions] if research else [],
        "steps": (["하위 주제와 검색어 구성", "독립 출처 탐색"] if research else ["문서 구조와 참고 자료 확인"])
                 + ["적절한 수집기로 자료 확보", "관련성·품질·중복 평가", "지식 도서관 적재와 결과 확인"],
        "budget": {"pages": max_pages, "depth": max_depth, "searches": 16, "seconds": 1800},
        "stop_conditions": ["임무 예산 소진", "허용 범위 탐색 종료", "연속 탐색에서 유의미한 신규 자료 없음"],
    }


async def create(db, *, goal: str, context: str = "", urls: list[str] | None = None,
                 max_pages: int = 200, max_depth: int = 3, request_key: str | None = None) -> CollectionMission:
    strategy = plan(goal, context=context, urls=urls, max_pages=max_pages, max_depth=max_depth)
    digest = hashlib.sha256(jcs(strategy).encode()).hexdigest()
    key = request_key or str(uuid.uuid4())
    await store._lock(db, "mission-create", "owner", key)
    existing = await db.scalar(select(CollectionMission).where(CollectionMission.tenant_id == "owner", CollectionMission.request_key == key))
    if existing:
        if existing.input_hash != digest:
            raise CollectorConflictError("request key already has a different mission")
        return existing
    mission = CollectionMission(id=uuid.uuid4(), tenant_id="owner", request_key=key,
                                input_hash=digest, goal=goal.strip(), plan=strategy, gaps=[], status="queued")
    db.add(mission)
    await db.flush()
    await launch(db, mission)
    return mission


async def launch(db, mission: CollectionMission) -> None:
    """Queue existing policy-backed sources without fetching or running models in the API."""
    ctx = AuthContext(tenant_id=mission.tenant_id, scope="personal", principal_id="owner", kind="owner")
    sources = list(await db.scalars(select(CollectorSource).where(
        CollectorSource.tenant_id == mission.tenant_id, CollectorSource.scope == ctx.scope,
        CollectorSource.enabled.is_(True))))
    gaps, queued = [], 0
    remaining = mission.plan["budget"]["pages"]
    for entry in mission.plan["sources"]:
        parsed = input_url(entry["url"])
        policy = None
        for candidate in sources:
            current = await store.active_policy(db, ctx.tenant_id, ctx.scope, candidate.source_key)
            if current and current[0].allows("store") and not current[0].check_url(parsed):
                policy = current[0]
                break
        if not policy:
            gaps.append({"url": entry["url"], "reason": "source_policy_required", "message": "출처의 보관·이용 권한을 확인해야 합니다."})
            continue
        if entry["adapter"] not in ("website", "paper_pdf"):
            gaps.append({"url": entry["url"], "reason": "adapter_pending", "message": "전용 수집기 연결이 필요합니다."})
            continue
        if remaining <= 0:
            gaps.append({"url": entry["url"], "reason": "mission_page_budget", "message": "임무의 수집 예산에 도달했습니다."})
            continue
        pages = min(remaining, policy.max_pages_per_job)
        key = f"mission:{mission.id}:{hashlib.sha256(entry['url'].encode()).hexdigest()[:16]}"
        source, _ = await control.register_source(
            db, ctx, source_key=key, title=mission.plan["title"], seeds=[entry["url"]], policy=policy.to_dict(),
            interval_minutes=None, max_pages=pages, max_depth=min(policy.max_depth, mission.plan["budget"]["depth"]),
            profile="general", auto_import=True)
        await db.flush()  # Apply source defaults before enqueue checks enabled/auto-import.
        job = await control.enqueue(db, source, extensions={"collector:mission": {
            "id": str(mission.id), "goal": mission.goal, "context_provenance": "user_provided"}})
        db.add(CollectionMissionJob(mission_id=mission.id, job_id=job.id))
        remaining -= pages
        queued += 1
    mission.gaps = gaps
    mission.status = "running" if queued else "discovery_pending" if mission.plan["mode"] == "research" else "waiting_sources"


async def view(db, mission: CollectionMission, *, detailed: bool = True) -> dict:
    jobs = list(await db.scalars(select(CollectorJob).join(CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id)
                                 .where(CollectionMissionJob.mission_id == mission.id, CollectorJob.tenant_id == mission.tenant_id)))
    counts = dict.fromkeys(("discovered", "acquired", "accepted", "held", "failed", "blocked", "duplicate", "imported", "pending_delivery", "not_imported"), 0)
    for job in jobs:
        counts["discovered"] += (job.cursor or {}).get("discovered", 0)
    results = []
    if jobs:
        filters = (CollectorItem.job_id.in_([job.id for job in jobs]), CollectorItem.tenant_id == mission.tenant_id)
        measures = {name: CollectorItem.status == name for name in ("accepted", "held", "failed", "blocked")}
        measures.update(acquired=CollectorItem.raw_sha256.is_not(None), duplicate=CollectorItem.reason_codes.contains(["exact_duplicate"]))
        totals = (await db.execute(select(*(func.count(CollectorItem.id).filter(condition).label(name)
                                            for name, condition in measures.items())).where(*filters))).one()
        counts.update(dict(totals._mapping))
        counts["imported"] = await db.scalar(select(func.count(CollectorItem.id.distinct())).join(
            CollectorDelivery, CollectorDelivery.item_id == CollectorItem.id).where(*filters, CollectorDelivery.outcome == "imported")) or 0
        counts["pending_delivery"] = await db.scalar(select(func.count(CollectorItem.id.distinct())).join(
            CollectorOutbox, CollectorOutbox.envelope_id == CollectorItem.envelope_id).where(*filters,
                CollectorOutbox.status == "pending", CollectorOutbox.tenant_id == mission.tenant_id)) or 0
        counts["not_imported"] = max(0, counts["accepted"] - counts["imported"] - counts["pending_delivery"])
        if detailed:
            # Select bounded metadata in SQL: polling a large mission must never load all normalized bodies.
            rows = (await db.execute(select(
                CollectorItem.id, CollectorItem.url, CollectorItem.final_url, CollectorItem.status,
                CollectorItem.reason_codes,
                CollectorItem.envelope["extensions"]["collector:page"]["title"].astext.label("title"),
                CollectorDelivery.outcome, CollectorDelivery.revision_id,
                CollectorDelivery.detail["document_id"].astext.label("document_id"),
            ).outerjoin(CollectorDelivery, CollectorDelivery.item_id == CollectorItem.id).where(*filters)
                .ext(distinct_on(CollectorItem.id)).order_by(CollectorItem.id, CollectorDelivery.created_at.desc().nulls_last()).limit(100))).all()
            results = [{"id": str(row.id), "url": row.final_url or row.url, "title": row.title or row.url,
                        "status": row.status, "reasons": row.reason_codes, "delivery": row.outcome,
                        "document_id": row.document_id, "revision_id": str(row.revision_id) if row.revision_id else None} for row in rows]
    state = mission.status
    if jobs and all(job.status in TERMINAL for job in jobs) and not counts["pending_delivery"]:
        state = "partial" if mission.gaps or counts["failed"] or counts["blocked"] or counts["not_imported"] or (counts["held"] and not counts["accepted"]) or any(j.status != "succeeded" for j in jobs) else "succeeded"
    return {
        "id": str(mission.id), "goal": mission.goal, "title": mission.plan["title"], "status": state,
        "plan": mission.plan if detailed else {k: v for k, v in mission.plan.items() if k != "context"},
        "gaps": mission.gaps, "counts": counts, "results": results,
        "jobs": [{"job_id": str(job.id), "status": job.status, "stage": job.stage,
                  "error": job.last_error} for job in jobs],
        "knowledge_processing": {"storage": "verified" if counts["imported"] else "pending", "semantic": "unverified"},
        "created_at": mission.created_at.isoformat(), "updated_at": mission.updated_at.isoformat(),
    }
