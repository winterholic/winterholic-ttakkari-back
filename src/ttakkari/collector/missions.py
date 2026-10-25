"""Intent-first collection over the existing acquisition and KB delivery contracts.

Plans describe intended coverage, never evidence of completed research. URL routing is
deterministic. Unregistered rights remain gaps; no client text can grant source rights.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on

from ttakkari.collector import control, jobs, store
from ttakkari.collector.contracts import (
    AuthContext,
    CollectorConflictError,
    CollectorContractError,
    JobStateError,
    jcs,
)
from ttakkari.collector.control_models import CollectorDelivery, CollectorSource
from ttakkari.collector.netguard import normalize_url
from ttakkari.collector.pipeline import COUNT_KEYS
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
    if mission.plan["mode"] == "research" or any(entry["adapter"] == "github" for entry in mission.plan["sources"]):
        await queue_preparation(db, mission)
    return mission


async def queue_preparation(db, mission: CollectionMission) -> None:
    """Internal orchestration job. No document rights or delivery outbox are manufactured.

    Only the server can create its stage and mission relation. It uses the same claim,
    cancellation, attempt fence and generation drain as acquisition jobs.
    """
    ident = uuid.uuid4()
    envelope = {"mission_id": str(mission.id)}
    job = CollectorJob(id=ident, tenant_id=mission.tenant_id, scope="personal",
                       source_key=f"mission-prepare:{mission.id}", request_key=f"prepare:{mission.id}",
                       payload_hash=hashlib.sha256(jcs(envelope).encode()).hexdigest(), envelope_id=uuid.uuid4(),
                       envelope=envelope, stage="mission_prepare", status="queued")
    db.add(job)
    await db.flush()
    db.add(CollectionMissionJob(mission_id=mission.id, job_id=ident))
    mission.status = "running"


async def reconcile_mission(db, mission: CollectionMission) -> None:
    """Evaluate budget, auto-split jobs exceeding policy limits, and fairly distribute remaining quota.

    Uses actual processed pages from finished jobs (reclaiming unused reservations) and ensures
    independent sources are not blocked by one greedy source.
    """
    if mission.status in ("cancelled", "paused"):
        return
    ctx = AuthContext(tenant_id=mission.tenant_id, scope="personal", principal_id="owner", kind="owner")
    if any(urlsplit(entry["url"]).hostname == "docs.python.org" for entry in mission.plan["sources"]):
        await register_python_docs(db, ctx)
    sources = list(await db.scalars(select(CollectorSource).where(
        CollectorSource.tenant_id == mission.tenant_id, CollectorSource.scope == ctx.scope,
        CollectorSource.enabled.is_(True))))

    linked = list(await db.scalars(select(CollectorJob).join(CollectionMissionJob,
        CollectionMissionJob.job_id == CollectorJob.id).where(CollectionMissionJob.mission_id == mission.id)))

    used_pages = 0
    active_sources = set()
    jobs_by_source: dict[str, list[CollectorJob]] = {}
    for job in linked:
        if job.stage == "mission_prepare":
            continue
        jobs_by_source.setdefault(job.source_key, []).append(job)
        if job.status in TERMINAL:
            used_pages += (job.cursor or {}).get("processed", 0)
        else:
            req_pages = (job.envelope.get("extensions", {}).get("collector:request") or {}).get("max_pages", 0)
            processed = (job.cursor or {}).get("processed", 0)
            used_pages += max(req_pages, processed)
            active_sources.add(job.source_key)

    remaining = max(0, mission.plan["budget"]["pages"] - used_pages)
    entries = mission.plan["sources"]
    gaps = [g for g in mission.gaps if g.get("reason") != "mission_page_budget"]

    need_work: list[dict[str, Any]] = []
    for entry in entries:
        if entry["adapter"] == "github":
            continue  # Expanded by the durable preparation job, never fetched as HTML.
        key = f"mission:{mission.id}:{hashlib.sha256(entry['url'].encode()).hexdigest()[:16]}"
        if key in active_sources:
            continue
        s_jobs = jobs_by_source.get(key, [])
        if not s_jobs:
            need_work.append({"kind": "new", "entry": entry, "key": key})
        else:
            last_job = max(s_jobs, key=lambda j: j.created_at)
            frontier = (last_job.cursor or {}).get("frontier", [])
            if last_job.status in TERMINAL and frontier:
                need_work.append({
                    "kind": "split_resume", "entry": entry, "key": key,
                    "last_job": last_job, "frontier": frontier,
                })

    queued = 0
    for index, item in enumerate(need_work):
        entry = item["entry"]
        parsed = input_url(entry["url"])
        policy = None
        for candidate in sources:
            if candidate.request and entry["url"] in candidate.request.get("seeds", []):
                current = await store.active_policy(db, ctx.tenant_id, ctx.scope, candidate.source_key)
                if current and current[0].allows("store") and not current[0].check_url(parsed):
                    policy = current[0]
                    break
        if not policy:
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

        fair_share = max(1, remaining // max(1, len(need_work) - index))
        pages = min(policy.max_pages_per_job, fair_share)

        if item["kind"] == "new":
            source, _ = await control.register_source(
                db, ctx, source_key=item["key"], title=mission.plan["title"], seeds=[entry["url"]], policy=policy.to_dict(),
                interval_minutes=mission.plan.get("schedule", {}).get("interval_minutes"),
                max_pages=pages, max_depth=min(policy.max_depth, mission.plan["budget"]["depth"]),
                profile="general", auto_import=True)
            await db.flush()
            job = await control.enqueue(db, source, extensions={"collector:mission": {
                "id": str(mission.id), "goal": mission.goal, "context_provenance": "user_provided",
                "reference": entry}})
            if not await db.scalar(select(CollectionMissionJob.mission_id).where(
                CollectionMissionJob.mission_id == mission.id, CollectionMissionJob.job_id == job.id
            )):
                db.add(CollectionMissionJob(mission_id=mission.id, job_id=job.id))
            remaining -= pages
            queued += 1
        elif item["kind"] == "split_resume":
            source = await db.scalar(select(CollectorSource).where(
                CollectorSource.tenant_id == mission.tenant_id, CollectorSource.source_key == item["key"]
            ))
            if not source:
                source, _ = await control.register_source(
                    db, ctx, source_key=item["key"], title=mission.plan["title"], seeds=[entry["url"]], policy=policy.to_dict(),
                    interval_minutes=mission.plan.get("schedule", {}).get("interval_minutes"),
                    max_pages=pages, max_depth=min(policy.max_depth, mission.plan["budget"]["depth"]),
                    profile="general", auto_import=True)
                await db.flush()

            from datetime import UTC, datetime
            now = datetime.now(UTC)
            ident = uuid.uuid4()
            envelope = control.intake_envelope(source, policy, now, ident)
            req_dict = dict(envelope["extensions"]["collector:request"])
            req_dict["max_pages"] = pages
            req_dict["seeds"] = [e.get("url") or e.get("u") for e in item["frontier"]]
            envelope["extensions"]["collector:request"] = req_dict
            envelope["extensions"]["collector:mission"] = {
                "id": str(mission.id), "goal": mission.goal, "context_provenance": "user_provided",
                "reference": entry, "parent_job_id": str(item["last_job"].id),
            }
            child_cursor = {
                "v": 1,
                "frontier": list(item["frontier"]),
                "seen": list((item["last_job"].cursor or {}).get("seen", [])),
                "processed": 0,
                "discovered": (item["last_job"].cursor or {}).get("discovered", len(item["frontier"])),
                "counts": dict.fromkeys(COUNT_KEYS, 0),
            }
            child_job = CollectorJob(
                id=ident,
                tenant_id=source.tenant_id,
                scope=source.scope,
                source_key=source.source_key,
                request_key=f"split:{item['last_job'].id}:{uuid.uuid4().hex[:8]}",
                payload_hash=hashlib.sha256(jcs(envelope).encode()).hexdigest(),
                envelope_id=ident,
                status="queued",
                stage="intake",
                envelope=envelope,
                cursor=child_cursor,
                not_before=now,
            )
            db.add(child_job)
            await db.flush()
            db.add(CollectionMissionJob(mission_id=mission.id, job_id=child_job.id))
            source.last_job_id = child_job.id
            remaining -= pages
            queued += 1

    mission.gaps = gaps
    if queued or active_sources:
        mission.status = "running"
    elif mission.plan["mode"] == "research" and not linked:
        mission.status = "discovery_pending"
    elif not linked and not queued:
        mission.status = "waiting_sources"
    elif linked and not active_sources and not queued:
        has_failure = gaps or any(j.status in ("partial", "failed", "cancelled") for j in linked)
        mission.status = "partial" if has_failure else "succeeded"


async def launch(db, mission: CollectionMission) -> None:
    """Queue existing policy-backed sources without fetching or running models in the API."""
    await reconcile_mission(db, mission)


async def register_python_docs(db, ctx):
    """Reviewed official grant, independent of user text; robots still applies at fetch."""
    key = "official:python-docs"
    await store._lock(db, "control-source", ctx.tenant_id, ctx.scope, key)
    if await db.scalar(select(CollectorSource.id).where(CollectorSource.tenant_id == ctx.tenant_id,
            CollectorSource.scope == ctx.scope, CollectorSource.source_key == key)):
        return  # A paused/revoked source is never silently recreated.
    await control.register_source(db, ctx, source_key=key, title="Python 공식 문서", seeds=["https://docs.python.org/3/"],
        policy={"allowed_hosts": ["docs.python.org"],
                "permissions": {"store": "allowed", "knowledge": "allowed", "export": "allowed", "train": "unknown"},
                "license_id": "PSF-2.0", "policy_url": "https://docs.python.org/3/license.html",
                "checked_at": "2026-10-25T05:22:17Z", "max_pages_per_job": 500, "max_depth": 3,
                "basis": "Official History and License: Python software and documentation are PSF License v2; preserve attribution and license URL. Code examples since 3.8.6 are also 0BSD.",
                "excluded_path_prefixes": ["/dev/", "/_static/", "/_images/"]},
        interval_minutes=None, max_pages=500, max_depth=3)
    await db.flush()


async def prepare(job, ctx):
    """Inventory outside transactions; persist progress, policy and child jobs atomically."""
    from ttakkari.collector import adapters
    from ttakkari.collector.runner import HandlerResult
    from ttakkari.db import sessionmaker

    async with sessionmaker()() as db:
        mission = await db.scalar(select(CollectionMission).join(CollectionMissionJob,
            CollectionMissionJob.mission_id == CollectionMission.id).where(
                CollectionMissionJob.job_id == job.id, CollectionMission.tenant_id == job.tenant_id))
        if mission is None or str(mission.id) != job.envelope.get("mission_id"):
            return HandlerResult("quarantined", {"code": "unbound_mission_preparation"})
        strategy = mission.plan
    done = list((job.cursor or {}).get("prepared", []))
    search_cursor = int((job.cursor or {}).get("search_cursor", 0))
    no_new = int((job.cursor or {}).get("no_new", 0))
    queries = strategy["queries"][:strategy["budget"]["searches"]]
    for query_index, query in enumerate(queries[search_cursor:] if no_new < 2 else [], start=search_cursor):
        try:
            candidates = await adapters.search_web(query)
        except Exception:  # noqa: BLE001
            candidates = []
        await ctx.heartbeat()
        progress = {}

        async def record_search(db, _candidates=candidates, _query=query, _index=query_index, _progress=progress, _no_new=no_new):
            owned = await jobs._fenced_job(db, job.id, job.attempt_token)
            if owned.cancel_requested:
                return owned.cursor, []
            current = await db.get(CollectionMission, mission.id, with_for_update=True)
            known = {source["url"] for source in current.plan["sources"]}
            additions = [candidate for candidate in _candidates if candidate["url"] not in known]
            _progress["no_new"] = _no_new + 1 if not additions else 0
            history = [*(current.plan.get("search_history") or []),
                       {"query": _query, "candidates": len(_candidates), "new_sources": len(additions), "provider": "duckduckgo_html"}]
            current.plan = {**current.plan, "sources": current.plan["sources"] + additions, "search_history": history,
                            "search_stop": "diminishing_returns" if _progress["no_new"] >= 2 else "query_budget" if _index + 1 == len(queries) else None}
            await launch(db, current)
            if not current.plan["sources"]:
                current.gaps = [{"reason": "no_search_candidates", "message": "검색에서 수집할 출처를 찾지 못했습니다."}]
            _progress["strategy"] = current.plan
            return {"prepared": list(done), "search_cursor": _index + 1, "no_new": _progress["no_new"]}, []

        if await ctx.commit_work(record_search, stage="mission_prepare") == "cancelled":
            return HandlerResult("failed", {"code": "cancelled"})
        strategy, no_new = progress["strategy"], progress["no_new"]
        search_cursor = query_index + 1
        if no_new >= 2:
            break
    for entry in strategy["sources"]:
        if entry["adapter"] != "github" or entry["url"] in done:
            continue
        try:
            inventory = await adapters.github_repository(entry["url"], limit=min(2000, strategy["budget"]["pages"]))
        except Exception as exc:  # noqa: BLE001
            # Metadata failure on one repository does not block subsequent independent sources; record as a gap
            done.append(entry["url"])
            async def record_metadata_failure(db, _entry=entry, _exc=exc, _done=tuple(done)):
                owned = await jobs._fenced_job(db, job.id, job.attempt_token)
                if owned.cancel_requested:
                    return owned.cursor, []
                current = await db.get(CollectionMission, mission.id, with_for_update=True)
                msg = f"GitHub 저장소 메타데이터 조회 실패: {_exc!s}"
                current.gaps = [*current.gaps, {"url": _entry["url"], "reason": "github_metadata_failed", "message": msg}]
                return {"prepared": list(_done), "search_cursor": search_cursor, "no_new": no_new}, []

            if await ctx.commit_work(record_metadata_failure, stage="mission_prepare") == "cancelled":
                return HandlerResult("failed", {"code": "cancelled"})
            continue
        await ctx.heartbeat()
        done.append(entry["url"])

        async def persist(db, _inventory=inventory, _entry=entry, _done=tuple(done)):
            owned = await jobs._fenced_job(db, job.id, job.attempt_token)
            if owned.cancel_requested:
                return owned.cursor, []
            current = await db.get(CollectionMission, mission.id, with_for_update=True)
            additions = _inventory["documents"]
            known = {source["url"] for source in current.plan["sources"]}
            current.plan = {**current.plan, "sources": current.plan["sources"] + [doc for doc in additions if doc["url"] not in known],
                            "repositories": [*(current.plan.get("repositories") or []),
                                             {k: v for k, v in _inventory.items() if k != "documents"}]}
            for doc in additions:
                await register_document_license(db, current, doc)
            await launch(db, current)
            if _inventory["coverage"].get("limited") or _inventory["coverage"].get("truncated"):
                current.gaps = [*current.gaps, {"url": _entry["url"], "reason": "repository_coverage_limited",
                                               "coverage": _inventory["coverage"]}]
            return {"prepared": list(_done), "search_cursor": search_cursor, "no_new": no_new}, []

        if await ctx.commit_work(persist, stage="mission_prepare") == "cancelled":
            return HandlerResult("failed", {"code": "cancelled"})
    return HandlerResult("succeeded")


async def register_document_license(db, mission, doc):
    """An observed file grant is scoped to that exact commit/file, never the raw host."""
    from datetime import UTC, datetime

    license_info = doc.get("license") or {}
    if license_info.get("permission") != "allowed":
        return
    parsed = input_url(doc["url"])
    key = f"license:{hashlib.sha256(doc['url'].encode()).hexdigest()}"
    exists = await db.scalar(select(CollectorSource.id).where(CollectorSource.tenant_id == mission.tenant_id,
        CollectorSource.scope == "personal", CollectorSource.source_key == key))
    if exists:
        return
    policy = {"allowed_hosts": [parsed.host], "allowed_path_prefixes": [urlsplit(doc["url"]).path],
              "permissions": {"store": "allowed", "knowledge": "allowed", "export": "allowed", "train": "unknown"},
              "license_id": license_info["id"], "policy_url": license_info["url"],
              "basis": f"GitHub commit-pinned license metadata: {license_info.get('metadata_sha')}; preserve source and license attribution.",
              "checked_at": datetime.now(UTC).isoformat(), "max_pages_per_job": 1, "max_depth": 0}
    await control.register_source(db, AuthContext(tenant_id=mission.tenant_id, scope="personal", principal_id="owner", kind="owner"),
        source_key=key, title=doc.get("path", doc["url"]), seeds=[doc["url"]], policy=policy,
        interval_minutes=None, max_pages=1)
    await db.flush()


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
    if state not in ("paused", "cancelled") and jobs and all(job.status in TERMINAL for job in jobs) and not counts["pending_delivery"]:
        latest_jobs = {}
        for j in jobs:
            latest_jobs[j.source_key] = max(latest_jobs.get(j.source_key, j), j, key=lambda x: x.created_at)
        has_failed_latest = any(j.status != "succeeded" for j in latest_jobs.values() if j.stage != "mission_prepare")
        state = "partial" if mission.gaps or counts["failed"] or counts["blocked"] or counts["not_imported"] or (counts["held"] and not counts["accepted"]) or has_failed_latest else "succeeded"
        if not counts["acquired"]:
            state = "partial"
    return {
        "id": str(mission.id), "goal": mission.goal, "title": mission.plan["title"], "status": state,
        "plan": mission.plan if detailed else {k: v for k, v in mission.plan.items() if k != "context"},
        "gaps": mission.gaps, "counts": counts, "results": results,
        "jobs": [{"job_id": str(job.id), "status": job.status, "stage": job.stage,
                  "error": job.last_error} for job in jobs],
        "knowledge_processing": {"storage": "verified" if counts["imported"] else "pending", "semantic": "unverified"},
        "created_at": mission.created_at.isoformat(), "updated_at": mission.updated_at.isoformat(),
    }


async def pause(db, mission_id: uuid.UUID, *, tenant_id: str = "owner") -> CollectionMission:
    await store._lock(db, "mission-control", tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == tenant_id
    ).with_for_update())
    if mission is None:
        raise ValueError("수집 임무를 찾을 수 없습니다.")
    if mission.status == "paused":
        return mission
    if mission.status in ("cancelled", "succeeded"):
        return mission
    mission.status = "paused"
    linked_jobs = list(await db.scalars(select(CollectorJob).join(
        CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
    ).where(CollectionMissionJob.mission_id == mission.id)))
    source_keys = {j.source_key for j in linked_jobs}
    if source_keys:
        sources = list(await db.scalars(select(CollectorSource).where(
            CollectorSource.tenant_id == tenant_id, CollectorSource.source_key.in_(source_keys)
        )))
        for s in sources:
            s.enabled = False
    await db.flush()
    return mission


async def resume(db, mission_id: uuid.UUID, *, tenant_id: str = "owner") -> CollectionMission:
    await store._lock(db, "mission-control", tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == tenant_id
    ).with_for_update())
    if mission is None:
        raise ValueError("수집 임무를 찾을 수 없습니다.")
    if mission.status == "running":
        return mission
    if mission.status == "cancelled":
        return mission
    mission.status = "running"
    linked_jobs = list(await db.scalars(select(CollectorJob).join(
        CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
    ).where(CollectionMissionJob.mission_id == mission.id)))
    source_keys = {j.source_key for j in linked_jobs}
    if source_keys:
        sources = list(await db.scalars(select(CollectorSource).where(
            CollectorSource.tenant_id == tenant_id, CollectorSource.source_key.in_(source_keys)
        )))
        for s in sources:
            s.enabled = True
    await db.flush()
    await reconcile_mission(db, mission)
    return mission


async def cancel(db, mission_id: uuid.UUID, *, tenant_id: str = "owner") -> CollectionMission:
    await store._lock(db, "mission-control", tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == tenant_id
    ).with_for_update())
    if mission is None:
        raise ValueError("수집 임무를 찾을 수 없습니다.")
    if mission.status == "cancelled":
        return mission
    mission.status = "cancelled"
    linked_jobs = list(await db.scalars(select(CollectorJob).join(
        CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
    ).where(CollectionMissionJob.mission_id == mission.id)))
    for job in linked_jobs:
        if job.status not in TERMINAL:
            try:
                await jobs.cancel(db, job.tenant_id, job.scope, job.id)
            except (CollectorContractError, JobStateError):
                pass
    source_keys = {j.source_key for j in linked_jobs}
    if source_keys:
        sources = list(await db.scalars(select(CollectorSource).where(
            CollectorSource.tenant_id == tenant_id, CollectorSource.source_key.in_(source_keys)
        )))
        for s in sources:
            s.enabled = False
            s.interval_minutes = None
    await db.flush()
    return mission


async def extend(db, mission_id: uuid.UUID, *, add_pages: int, tenant_id: str = "owner") -> CollectionMission:
    if not 1 <= add_pages <= 100_000:
        raise ValueError("추가할 수집 쪽수를 확인해 주세요 (1~100,000).")
    await store._lock(db, "mission-control", tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == tenant_id
    ).with_for_update())
    if mission is None:
        raise ValueError("수집 임무를 찾을 수 없습니다.")
    if mission.status == "cancelled":
        raise ValueError("중지된 임무는 확장할 수 없습니다. 다시 실행을 이용해 주세요.")
    mission.plan = {
        **mission.plan,
        "budget": {**mission.plan["budget"], "pages": mission.plan["budget"]["pages"] + add_pages},
    }
    mission.gaps = [g for g in mission.gaps if g.get("reason") != "mission_page_budget"]
    mission.status = "running"
    await db.flush()
    await reconcile_mission(db, mission)
    return mission


async def rerun(db, mission_id: uuid.UUID, *, tenant_id: str = "owner") -> CollectionMission:
    await store._lock(db, "mission-control", tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == tenant_id
    ).with_for_update())
    if mission is None:
        raise ValueError("수집 임무를 찾을 수 없습니다.")
    linked = list(await db.scalars(select(CollectorJob).join(
        CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
    ).where(CollectionMissionJob.mission_id == mission.id)))
    for job in linked:
        if job.status not in TERMINAL:
            try:
                await jobs.cancel(db, job.tenant_id, job.scope, job.id)
            except (CollectorContractError, JobStateError):
                pass
    mission.status = "queued"
    mission.gaps = []
    source_keys = {j.source_key for j in linked}
    if source_keys:
        sources = list(await db.scalars(select(CollectorSource).where(
            CollectorSource.tenant_id == tenant_id, CollectorSource.source_key.in_(source_keys)
        )))
        for s in sources:
            s.enabled = True
            s.last_job_id = None
    ctx = AuthContext(tenant_id=mission.tenant_id, scope="personal", principal_id="owner", kind="owner")
    entries = mission.plan["sources"]
    remaining = mission.plan["budget"]["pages"]
    active_sources = list(await db.scalars(select(CollectorSource).where(
        CollectorSource.tenant_id == mission.tenant_id, CollectorSource.scope == ctx.scope,
        CollectorSource.enabled.is_(True))))
    for index, entry in enumerate(entries):
        if entry["adapter"] == "github":
            continue
        key = f"mission:{mission.id}:{hashlib.sha256(entry['url'].encode()).hexdigest()[:16]}"
        parsed = input_url(entry["url"])
        policy = None
        for candidate in active_sources:
            if candidate.request and entry["url"] in candidate.request.get("seeds", []):
                current = await store.active_policy(db, ctx.tenant_id, ctx.scope, candidate.source_key)
                if current and current[0].allows("store") and not current[0].check_url(parsed):
                    policy = current[0]
                    break
        if not policy:
            for candidate in active_sources:
                current = await store.active_policy(db, ctx.tenant_id, ctx.scope, candidate.source_key)
                if current and current[0].allows("store") and not current[0].check_url(parsed):
                    policy = current[0]
                    break
        if policy and remaining > 0:
            fair_share = max(1, remaining // max(1, len(entries) - index))
            pages = min(policy.max_pages_per_job, fair_share)
            source = await db.scalar(select(CollectorSource).where(
                CollectorSource.tenant_id == ctx.tenant_id, CollectorSource.source_key == key
            ))
            if not source:
                source, _ = await control.register_source(
                    db, ctx, source_key=key, title=mission.plan["title"], seeds=[entry["url"]], policy=policy.to_dict(),
                    interval_minutes=mission.plan.get("schedule", {}).get("interval_minutes"),
                    max_pages=pages, max_depth=min(policy.max_depth, mission.plan["budget"]["depth"]),
                    profile="general", auto_import=True)
                await db.flush()
            job = await control.enqueue(db, source, extensions={"collector:mission": {
                "id": str(mission.id), "goal": mission.goal, "context_provenance": "user_provided",
                "reference": entry, "rerun": True}})
            if not await db.scalar(select(CollectionMissionJob.mission_id).where(
                CollectionMissionJob.mission_id == mission.id, CollectionMissionJob.job_id == job.id
            )):
                db.add(CollectionMissionJob(mission_id=mission.id, job_id=job.id))
            remaining -= pages
    if mission.plan["mode"] == "research" or any(entry["adapter"] == "github" for entry in mission.plan["sources"]):
        await queue_preparation(db, mission)
    mission.status = "running"
    await db.flush()
    return mission


async def update_schedule(db, mission_id: uuid.UUID, *, interval_minutes: int | None, tenant_id: str = "owner") -> CollectionMission:
    if interval_minutes is not None and not 1 <= interval_minutes <= 43_200:
        raise ValueError("수집 주기는 1분에서 30일(43,200분) 사이여야 합니다.")
    await store._lock(db, "mission-control", tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == tenant_id
    ).with_for_update())
    if mission is None:
        raise ValueError("수집 임무를 찾을 수 없습니다.")
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    mission.plan = {
        **mission.plan,
        "schedule": {
            "interval_minutes": interval_minutes,
            "updated_at": now.isoformat(),
        },
    }
    linked = list(await db.scalars(select(CollectorJob).join(
        CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
    ).where(CollectionMissionJob.mission_id == mission.id)))
    source_keys = {j.source_key for j in linked}
    if source_keys:
        sources = list(await db.scalars(select(CollectorSource).where(
            CollectorSource.tenant_id == tenant_id, CollectorSource.source_key.in_(source_keys)
        )))
        for s in sources:
            s.interval_minutes = interval_minutes
            s.next_run_at = now + timedelta(minutes=interval_minutes) if interval_minutes else None
            if interval_minutes and not s.enabled:
                s.enabled = True
    await db.flush()
    return mission
