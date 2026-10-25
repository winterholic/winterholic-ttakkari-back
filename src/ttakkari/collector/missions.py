"""Intent-first collection over the existing acquisition and KB delivery contracts.

Plans describe intended coverage, never evidence of completed research. URL routing is
deterministic. Unregistered rights remain gaps; no client text can grant source rights.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on

from ttakkari.collector import (
    control,
    jobs,
    precise_eval,
    research_rounds,
    store,
    triage,
    triage_ml,
)
from ttakkari.collector import research as research_logic
from ttakkari.collector.contracts import (
    AuthContext,
    CollectorConflictError,
    CollectorContractError,
    JobStateError,
    jcs,
)
from ttakkari.collector.control_models import CollectorDelivery, CollectorSource
from ttakkari.collector.library import LIBRARY_SCOPE, effective_pool, in_library
from ttakkari.collector.netguard import normalize_url
from ttakkari.collector.pipeline import COUNT_KEYS
from ttakkari.collector_models import (
    CollectionMission,
    CollectionMissionJob,
    CollectionMissionRun,
    CollectorFetchCache,
    CollectorItem,
    CollectorJob,
    CollectorOutbox,
)
from ttakkari.config import get_settings

URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}\"']+")
TERMINAL = {"succeeded", "partial", "failed", "cancelled", "policy_blocked", "quarantined"}
log = logging.getLogger(__name__)


def source_key_for(mission_id: uuid.UUID, url: str) -> str:
    return f"mission:{mission_id}:{hashlib.sha256(url.encode()).hexdigest()[:16]}"


async def get_run(db, mission: CollectionMission, seq: int | None = None, *, trigger: str = "initial") -> CollectionMissionRun:
    """The run row, created on first use for missions that predate run tracking. Caller holds the mission lock."""
    seq = seq or mission.current_run_seq or 1
    run = await db.get(CollectionMissionRun, (mission.id, seq), with_for_update=True, populate_existing=True)
    if run is None:
        run = CollectionMissionRun(mission_id=mission.id, run_seq=seq, trigger=trigger, started_at=datetime.now(UTC),
                                   page_budget=mission.plan["budget"]["pages"], extra_pages=0)
        db.add(run)
        await db.flush()
    return run


async def begin_run(db, mission: CollectionMission, trigger: str) -> CollectionMissionRun:
    """Start a new run that receives the mission's configured per-run budget. Caller holds the mission lock."""
    seq = (mission.current_run_seq or 1) + 1
    mission.current_run_seq = seq
    await db.flush()
    return await get_run(db, mission, seq, trigger=trigger)


def needs_preparation(strategy: dict) -> bool:
    """Search, repository inventory and paper resolution run in a durable preparation job, not in the API request."""
    return strategy["mode"] == "research" or any(entry["adapter"] in ("github", "paper") for entry in strategy["sources"])


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
                   else "paper" if research_logic.paper_reference_of_url(url) and not parsed.path.startswith("/pdf/") and not parsed.path.lower().endswith(".pdf")
                   else "paper_pdf" if parsed.path.lower().endswith(".pdf") or host in {"arxiv.org", "doi.org"}
                   else "website")
        sources.append({"url": url, "adapter": adapter, "provenance": "user_reference"})
    if len(sources) > 100:
        raise ValueError("한 임무에는 참고 주소를 100개까지 넣을 수 있습니다.")
    research = not sources
    parts = research_logic.initial_plan_parts(goal, context) if research else {}
    return {
        "version": 1, "title": goal.strip().splitlines()[0][:100],
        "mode": "research" if research else "mixed" if context else "sources", "goal": goal.strip(),
        "sources": sources,
        "context": {"text": context, "provenance": "user_provided", "fact_check_status": "unverified"},
        "queries": parts.get("queries", []), "coverage": parts.get("coverage", []),
        **({k: parts[k] for k in ("topics", "query_topics", "deferred_queries")} if research else {}),
        "steps": (["하위 주제와 한·영 검색어 구성", "독립 출처 탐색", "수집 자료의 참고문헌·새 키워드로 추가 탐색"] if research
                  else ["문서 구조와 참고 자료 확인"] + (["수집 자료와 사용자 맥락의 관계 확인"] if context else []))
                 + ["적절한 수집기로 자료 확보", "관련성·품질·중복 평가", "지식 도서관 적재와 결과 확인"],
        "budget": {"pages": max_pages, "depth": max_depth, "searches": 16, "seconds": 1800, "rounds": research_logic.MAX_ROUNDS,
                   "papers": research_logic.MAX_PAPERS, "model_calls": 0},
        "stop_conditions": ["임무 예산 소진", "허용 범위 탐색 종료", "연속 탐색에서 유의미한 신규 자료 없음", "모든 영역 확보"],
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
    await get_run(db, mission, 1)
    await launch(db, mission)
    if needs_preparation(mission.plan):
        await queue_preparation(db, mission)
    return mission


async def queue_preparation(db, mission: CollectionMission, run_seq: int | None = None, *, round_no: int = 0) -> None:
    """Internal orchestration job. No document rights or delivery outbox are manufactured.

    Only the server can create its stage and mission relation. It uses the same claim,
    cancellation, attempt fence and generation drain as acquisition jobs.
    """
    ident = uuid.uuid4()
    run_seq = run_seq or mission.current_run_seq or 1
    envelope = {"mission_id": str(mission.id), **({"round": round_no} if round_no else {})}
    job = CollectorJob(id=ident, tenant_id=mission.tenant_id, scope=LIBRARY_SCOPE,
                       source_key=f"mission-prepare:{mission.id}",
                       request_key=(f"prepare:{mission.id}:{run_seq}:r{round_no}" if round_no else
                                    f"prepare:{mission.id}" if run_seq == 1 else f"prepare:{mission.id}:{run_seq}"),
                       payload_hash=hashlib.sha256(jcs(envelope).encode()).hexdigest(), envelope_id=uuid.uuid4(),
                       envelope=envelope, stage="mission_prepare", status="queued")
    db.add(job)
    await db.flush()
    db.add(CollectionMissionJob(mission_id=mission.id, job_id=ident, run_seq=run_seq))
    mission.status = "running"


async def refresh_cursor(db, source: CollectorSource, prior: CollectorJob | None) -> dict[str, Any] | None:
    """Frontier of an incremental refresh, built only from state the collector already keeps.

    Order: what the previous run left unfetched, seeds never fetched, then fetched URLs stalest first. Each URL
    is fetched conditionally through the existing fetch cache (ETag / Last-Modified / raw hash), so unchanged
    documents become `not_modified` items that never reach the outbox. An unchanged page yields no links, which
    is why fetched URLs are listed explicitly instead of being rediscovered from the seeds."""
    from ttakkari.collector.pipeline import MAX_SEEN, _entry

    cached = (await db.execute(
        select(CollectorFetchCache.url, CollectorItem.depth)
        .outerjoin(CollectorItem, CollectorItem.id == CollectorFetchCache.last_item_id)
        .where(CollectorFetchCache.tenant_id == source.tenant_id, CollectorFetchCache.scope == source.scope,
               CollectorFetchCache.source_key == source.source_key)
        .order_by(CollectorFetchCache.last_fetched_at.asc()).limit(MAX_SEEN))).all()
    leftover = list(((prior.cursor or {}).get("frontier") or []) if prior is not None else [])
    if not cached and not leftover:
        return None
    frontier: list[dict[str, Any]] = []
    keys: set[str] = set()

    def add(entry: dict[str, Any]) -> None:
        key = store.url_key(entry["u"])[:16]
        if key not in keys:
            keys.add(key)
            frontier.append(entry)

    for entry in leftover:
        add({**entry, "u": entry.get("u") or entry.get("url")})
    fetched = {store.url_key(row.url)[:16] for row in cached}
    for seed in source.request.get("seeds", []):
        if store.url_key(seed)[:16] not in fetched:
            add(_entry(seed, 0))
    for row in cached:
        add(_entry(row.url, row.depth or 0))
    return {"v": 1, "frontier": frontier, "seen": sorted(keys), "processed": 0, "discovered": len(frontier),
            "counts": dict.fromkeys(COUNT_KEYS, 0)}


async def reconcile_mission(db, mission: CollectionMission) -> None:
    """Evaluate the current run's budget, auto-split jobs exceeding policy limits, and fairly distribute the rest.

    Only jobs of the current run are summed: each run receives the mission's per-run budget anew, and within a
    run no path (reconcile, extend, split, resume) can allocate beyond it because all of them serialize here.
    Uses actual processed pages from finished jobs (reclaiming unused reservations) and ensures
    independent sources are not blocked by one greedy source.
    """
    mission_id = mission.id
    await store._lock(db, "mission-control", mission.tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == mission.tenant_id
    ).with_for_update().execution_options(populate_existing=True))
    if mission is None or mission.status in ("cancelled", "paused"):
        return
    ctx = AuthContext(tenant_id=mission.tenant_id, scope=LIBRARY_SCOPE, principal_id="owner", kind="owner")
    if any(urlsplit(entry["url"]).hostname == "docs.python.org" for entry in mission.plan["sources"]):
        await register_python_docs(db, ctx)
    sources = list(await db.scalars(select(CollectorSource).where(
        CollectorSource.tenant_id == mission.tenant_id, in_library(CollectorSource.scope),
        CollectorSource.enabled.is_(True))))

    run = await get_run(db, mission)
    run_seq = run.run_seq
    run_budget = run.page_budget + run.extra_pages
    rows = (await db.execute(select(CollectorJob, CollectionMissionJob.run_seq).join(CollectionMissionJob,
        CollectionMissionJob.job_id == CollectorJob.id).where(CollectionMissionJob.mission_id == mission.id))).all()
    linked = [job for job, seq in rows if seq == run_seq]
    latest_by_source: dict[str, CollectorJob] = {}
    for job, _ in rows:
        if job.stage != "mission_prepare":
            latest_by_source[job.source_key] = max(latest_by_source.get(job.source_key, job), job, key=lambda j: j.created_at)

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

    remaining = max(0, run_budget - used_pages)
    entries = mission.plan["sources"]
    gaps = [g for g in mission.gaps if g.get("reason") != "mission_page_budget"]

    need_work: list[dict[str, Any]] = []
    for entry in entries:
        if entry["adapter"] in ("github", "paper"):
            continue  # Expanded by the durable preparation job, never fetched as HTML.
        key = source_key_for(mission.id, entry["url"])
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
        mission_ext = {"id": str(mission.id), "goal": mission.goal, "context_provenance": "user_provided",
                       "reference": entry, "run_seq": run_seq}

        source = await db.scalar(select(CollectorSource).where(
            CollectorSource.tenant_id == mission.tenant_id, CollectorSource.scope == ctx.scope,
            CollectorSource.source_key == item["key"]))
        if source is None:
            source, _ = await control.register_source(
                db, ctx, source_key=item["key"], title=mission.plan["title"], seeds=[entry["url"]], policy=policy.to_dict(),
                interval_minutes=mission.plan.get("schedule", {}).get("interval_minutes"),
                max_pages=pages, max_depth=min(policy.max_depth, mission.plan["budget"]["depth"]),
                profile="general", auto_import=True)
            await db.flush()

        if item["kind"] == "new":
            try:
                async with db.begin_nested():
                    job = await control.enqueue(
                        db, source, extensions={"collector:mission": mission_ext}, max_pages=pages,
                        cursor=await refresh_cursor(db, source, latest_by_source.get(item["key"])))
            except JobStateError as exc:
                gaps.append({"url": entry["url"], "reason": "daily_page_budget" if "daily" in str(exc) else "source_unavailable",
                             "message": "지금은 이 출처를 수집할 수 없습니다: " + str(exc)[:200]})
                continue
            remaining -= pages
            queued += 1
        elif item["kind"] == "split_resume":
            now = datetime.now(UTC)
            ident = uuid.uuid4()
            envelope = control.intake_envelope(source, policy, now, ident)
            req_dict = dict(envelope["extensions"]["collector:request"])
            req_dict["max_pages"] = pages
            req_dict["seeds"] = [e.get("url") or e.get("u") for e in item["frontier"]]
            envelope["extensions"]["collector:request"] = req_dict
            envelope["extensions"]["collector:mission"] = {**mission_ext, "parent_job_id": str(item["last_job"].id)}
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
                scope=LIBRARY_SCOPE,
                pool=effective_pool(source.pool, source.scope),
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
            db.add(CollectionMissionJob(mission_id=mission.id, job_id=child_job.id, run_seq=run_seq))
            source.last_job_id = child_job.id
            remaining -= pages
            queued += 1

    mission.gaps = list({(g.get("url"), g.get("reason")): g for g in gaps}.values())
    if queued or active_sources:
        mission.status = "running"
    elif mission.plan["mode"] == "research" and not linked:
        mission.status = "discovery_pending"
    elif not linked and not queued:
        mission.status = "waiting_sources"
    elif linked and not active_sources and not queued:
        has_failure = gaps or any(j.status in ("partial", "failed", "cancelled") for j in linked)
        mission.status = "partial" if has_failure else "succeeded"
    await research_rounds.maybe_queue_round(db, mission, run_seq, linked, queued=queued, active=active_sources)


async def launch(db, mission: CollectionMission) -> None:
    """Queue existing policy-backed sources without fetching or running models in the API."""
    await reconcile_mission(db, mission)


async def register_python_docs(db, ctx):
    """Reviewed official grant, independent of user text; robots still applies at fetch."""
    key = "official:python-docs"
    await store._lock(db, "control-source", ctx.tenant_id, key)
    if await db.scalar(select(CollectorSource.id).where(CollectorSource.tenant_id == ctx.tenant_id,
            in_library(CollectorSource.scope), CollectorSource.source_key == key)):
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
    round_no = int(job.envelope.get("round", 0))
    if round_no and (early := await research_rounds.expansion_step(job, ctx, mission.id, round_no, done)):
        return early
    if early := await research_rounds.search_phase(job, ctx, mission.id, done):
        return early
    async with sessionmaker()() as db:  # the search phase committed new sources, so repository inventory sees the current plan
        fresh = await db.get(CollectionMission, mission.id)
        strategy = fresh.plan
        progress_state = research_rounds.state(strategy, fresh.current_run_seq or 1, job.cursor)
    search_cursor, no_new = progress_state["search_cursor"], progress_state["no_new"]
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
    if early := await research_rounds.paper_phase(job, ctx, mission.id, done):
        return early
    async with sessionmaker()() as db:
        fresh = await db.get(CollectionMission, mission.id)
        pending = research_rounds.outstanding_failures(fresh.plan, fresh.current_run_seq or 1)
    if pending["search"] or pending["papers_retryable"]:
        # A failed provider call is not an empty result: retry through the job's own backoff, and report partial if it never recovers.
        if (pending["search_retryable"] or pending["papers_retryable"]) and job.attempts < job.max_attempts:
            return HandlerResult("retry_wait", {"code": "search_provider_failed", **pending}, retry_after=120)
        return HandlerResult("partial", {"code": "search_provider_failed", **pending})
    return HandlerResult("succeeded")


async def register_document_license(db, mission, doc):
    """An observed file grant is scoped to that exact commit/file, never the raw host."""
    license_info = doc.get("license") or {}
    if license_info.get("permission") != "allowed":
        return
    parsed = input_url(doc["url"])
    key = f"license:{hashlib.sha256(doc['url'].encode()).hexdigest()}"
    exists = await db.scalar(select(CollectorSource.id).where(CollectorSource.tenant_id == mission.tenant_id,
        in_library(CollectorSource.scope), CollectorSource.source_key == key))
    if exists:
        return
    policy = {"allowed_hosts": [parsed.host], "allowed_path_prefixes": [urlsplit(doc["url"]).path],
              "schemes": [parsed.scheme], "ports": [parsed.port],
              "permissions": {"store": "allowed", "knowledge": "allowed", "export": "allowed", "train": "unknown"},
              "license_id": license_info["id"], "policy_url": license_info["url"],
              "basis": license_info.get("basis") or f"GitHub commit-pinned license metadata: {license_info.get('metadata_sha')}; preserve source and license attribution.",
              "checked_at": datetime.now(UTC).isoformat(), "max_pages_per_job": 1, "max_depth": 0}
    await control.register_source(db, AuthContext(tenant_id=mission.tenant_id, scope=LIBRARY_SCOPE, principal_id="owner", kind="owner"),
        source_key=key, title=doc.get("path", doc["url"]), seeds=[doc["url"]], policy=policy,
        interval_minutes=None, max_pages=1)
    await db.flush()


def _ml_summary(stage2_status: dict[str, int]) -> dict:
    """Which stage-2 model ran, its recorded validation per axis and how many documents it actually covered."""
    artifact = triage_ml.load_artifact()
    heads = {}
    if artifact is not None:
        heads = {name: {"status": block["validation"]["status"], "reason": block["validation"]["reason"],
                        "metrics": block["validation"]["metrics"], "label_source": block["validation"]["label_source"]}
                 for name, block in {**artifact["heads"], "topic_relevance_semantic": artifact["relevance"]}.items()}
    return {"version": triage_ml.VERSION, "artifact_digest": (artifact or {}).get("digest"),
            "embedding": (artifact or {}).get("embedding", {}).get("model"), "documents_by_status": stage2_status,
            "axes_validation": heads, "validated_languages": sorted(triage_ml.VALIDATED_LANGUAGES),
            "unvalidated_effect": "recorded only; unvalidated axes never change a decision"}


async def view(db, mission: CollectionMission, *, detailed: bool = True) -> dict:
    job_rows = (await db.execute(select(CollectorJob, CollectionMissionJob.run_seq).join(CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id)
                                 .where(CollectionMissionJob.mission_id == mission.id, CollectorJob.tenant_id == mission.tenant_id))).all()
    jobs = [job for job, _ in job_rows]
    run_of = {job.id: seq for job, seq in job_rows}
    counts = dict.fromkeys(("discovered", "acquired", "accepted", "held", "failed", "blocked", "duplicate", "imported", "pending_delivery", "not_imported", "review_needed", "auto_accepted", "auto_held", "precise_waiting", "precise_unresolved"), 0)
    for job in jobs:
        counts["discovered"] += (job.cursor or {}).get("discovered", 0)
    results = []
    triage_reasons: dict[str, int] = {}
    precise_counts = {"pending": 0, "processing": 0, "unresolved": 0, "skipped_cap": 0, "evaluated": 0, "failed": 0, "budget_denied": 0}
    stage2_status: dict[str, int] = {}
    if jobs:
        filters = (CollectorItem.job_id.in_([job.id for job in jobs]), CollectorItem.tenant_id == mission.tenant_id)
        measures = {name: CollectorItem.status == name for name in ("accepted", "held", "failed", "blocked")}
        measures.update(acquired=CollectorItem.raw_sha256.is_not(None), duplicate=CollectorItem.reason_codes.contains(["exact_duplicate"]),
                        review_needed=CollectorItem.reason_codes.contains(["review_needed"]))
        totals = (await db.execute(select(*(func.count(CollectorItem.id).filter(condition).label(name)
                                            for name, condition in measures.items())).where(*filters))).one()
        counts.update(dict(totals._mapping))
        counts["auto_accepted"] = max(0, counts["accepted"] - counts["review_needed"])
        counts["auto_held"] = counts["held"]
        counts["imported"] = await db.scalar(select(func.count(CollectorItem.id.distinct())).join(
            CollectorDelivery, CollectorDelivery.item_id == CollectorItem.id).where(*filters, CollectorDelivery.outcome == "imported")) or 0
        counts["pending_delivery"] = await db.scalar(select(func.count(CollectorItem.id.distinct())).join(
            CollectorOutbox, CollectorOutbox.envelope_id == CollectorItem.envelope_id).where(*filters,
                CollectorOutbox.status == "pending", CollectorOutbox.tenant_id == mission.tenant_id)) or 0
        counts["not_imported"] = max(0, counts["accepted"] - counts["imported"] - counts["pending_delivery"])
        precise_state = CollectorItem.signals["triage"]["precise_eval"]["state"].astext
        precise_counts.update({state: n for state, n in (await db.execute(
            select(precise_state, func.count(CollectorItem.id)).where(*filters, precise_state.in_(tuple(precise_counts)))
            .group_by(precise_state))).all()})
        counts["precise_waiting"] = precise_counts["pending"] + precise_counts["processing"]
        counts["precise_unresolved"] = precise_counts["unresolved"]
        stage2_col = CollectorItem.signals["triage"]["stage2"]["status"].astext
        stage2_status = {k or "not_run": n for k, n in (await db.execute(
            select(stage2_col, func.count(CollectorItem.id)).where(*filters, CollectorItem.kind == "document")
            .group_by(stage2_col))).all()}
        if detailed:
            # Select bounded metadata in SQL: polling a large mission must never load all normalized bodies.
            rows = (await db.execute(select(
                CollectorItem.id, CollectorItem.url, CollectorItem.final_url, CollectorItem.status,
                CollectorItem.reason_codes, CollectorItem.signals,
                CollectorItem.envelope["extensions"]["collector:page"]["title"].astext.label("title"),
                CollectorDelivery.outcome, CollectorDelivery.revision_id,
                CollectorDelivery.detail["document_id"].astext.label("document_id"),
            ).outerjoin(CollectorDelivery, CollectorDelivery.item_id == CollectorItem.id).where(*filters)
                .ext(distinct_on(CollectorItem.id)).order_by(CollectorItem.id, CollectorDelivery.created_at.desc().nulls_last()).limit(100))).all()
            for row in rows:
                item_reasons = row.reason_codes or []
                triage_sig = (row.signals or {}).get("triage") or {}
                for r in item_reasons:
                    if r.startswith("triage:") or r in ("review_needed", "exact_duplicate"):
                        triage_reasons[r] = triage_reasons.get(r, 0) + 1
                results.append({
                    "id": str(row.id), "url": row.final_url or row.url, "title": row.title or row.url,
                    "status": row.status, "reasons": item_reasons,
                    "triage": {
                        "status": triage_sig.get("triage_status", "auto_held" if row.status == "held" else "auto_accepted"),
                        "scores": triage_sig.get("scores", {}),
                        "reasons": triage_sig.get("reasons", item_reasons),
                        "mode": triage_sig.get("mode", "active"),
                        "verified": triage_sig.get("verified", False),
                        "axes": {name: {k: axis.get(k) for k in ("value", "scale", "model_id", "model_revision",
                                                                "validation", "reason", "status") if k in axis}
                                 for name, axis in (triage_sig.get("axes") or {}).items()},
                        "validation": triage_sig.get("validation"),
                        "stage2": triage_sig.get("stage2"),
                        "precise_eval": triage_sig.get("precise_eval"),
                    },
                    "delivery": row.outcome,
                    "document_id": row.document_id, "revision_id": str(row.revision_id) if row.revision_id else None
                })
    runs = await run_views(db, mission, job_rows, detailed=detailed)
    state = mission.status
    if state not in ("paused", "cancelled") and jobs and all(job.status in TERMINAL for job in jobs) and not counts["pending_delivery"]:
        latest_jobs = {}
        for j in jobs:
            latest_jobs[j.source_key] = max(latest_jobs.get(j.source_key, j), j, key=lambda x: x.created_at)
        has_failed_latest = any(j.status != "succeeded" for j in latest_jobs.values() if j.stage != "mission_prepare")
        state = "partial" if mission.gaps or counts["failed"] or counts["blocked"] or counts["not_imported"] or (counts["held"] and not counts["accepted"]) or has_failed_latest else "succeeded"
        if not counts["acquired"]:
            state = "partial"

    engine_state = precise_eval.view_engine_status()  # never probes the harness qwen slot
    triage_summary = {
        "accepted": counts["auto_accepted"],
        "review_needed": counts["review_needed"],
        "held": counts["held"],
        "reasons": triage_reasons,
        "policy": {
            "mode": "conservative",
            "model": triage.MODEL_ID,
            "revision": triage.MODEL_REVISION,
            "status": "active",
            "verified": False,
        },
        "ml": _ml_summary(stage2_status),
        "precise_eval": {
            **precise_counts, "cap_per_job": get_settings().collector_triage_precise_queue_cap,
            "engine": engine_state.get("status", "unavailable"), "engine_detail": engine_state.get("detail"),
            "note": "bounded local qwen evaluator for uncertain candidates",
        },
    }

    return {
        "id": str(mission.id), "goal": mission.goal, "title": mission.plan["title"], "status": state,
        "plan": mission.plan if detailed else {k: v for k, v in mission.plan.items() if k != "context"},
        "run_seq": mission.current_run_seq or 1, "runs": runs,
        "gaps": mission.gaps, "counts": counts, "triage": triage_summary, "results": results,
        "jobs": [{"job_id": str(job.id), "status": job.status, "stage": job.stage, "run_seq": run_of[job.id],
                  "error": job.last_error} for job in jobs],
        "knowledge_processing": {"storage": "verified" if counts["imported"] else "pending", "semantic": "unverified"},
        "research": await research_rounds.view_block(db, mission) if detailed else None,
        "created_at": mission.created_at.isoformat(), "updated_at": mission.updated_at.isoformat(),
    }


async def unhold_item(db, mission_id: uuid.UUID, item_id: uuid.UUID) -> dict:
    """Unhold (recover) a held document in the mission and enqueue for KB delivery."""
    from datetime import UTC, datetime

    from ttakkari.collector import store
    from ttakkari.collector.control_models import CollectorReview

    item = await db.scalar(
        select(CollectorItem)
        .join(CollectionMissionJob, CollectionMissionJob.job_id == CollectorItem.job_id)
        .join(CollectionMission, CollectionMission.id == CollectionMissionJob.mission_id)
        .where(CollectionMissionJob.mission_id == mission_id, CollectionMission.tenant_id == "owner",
               CollectorItem.tenant_id == CollectionMission.tenant_id, CollectorItem.id == item_id)
    )
    if item is None:
        raise ValueError("item_not_found")
    if item.status != "held":
        return {"item_id": str(item.id), "status": item.status, "message": "item_not_held"}

    if not item.envelope_id or not item.envelope or not item.payload_hash or not item.text_sha256:
        raise ValueError("item_not_importable")
    rights = item.envelope.get("rights") or {}
    if rights.get("permission") != "allowed" or "knowledge" not in (rights.get("purposes") or []):
        raise ValueError("knowledge_permission_missing")
    if rights.get("retain_until") is not None:
        raise ValueError("kb_import_requires_unlimited_retention_policy")
    active = await store.active_policy(db, item.tenant_id, item.scope, item.source_key)
    if active is None or not active[0].allows("store") or not active[0].allows("knowledge"):
        raise ValueError("knowledge_permission_missing")

    now = datetime.now(UTC)
    item.status = "accepted"
    item.decision_state = "accept"
    reasons = list(item.reason_codes or [])
    if "manual_unhold" not in reasons:
        reasons.append("manual_unhold")
    item.reason_codes = reasons
    signals = dict(item.signals or {})
    signals["unhold_override"] = {"at": now.isoformat(), "previous_status": "held"}
    item.signals = signals

    review = CollectorReview(
        item_id=item.id,
        decision="accept",
        reason="Manual unhold: utility review only; factuality unverified.",
        actor="owner",
        text_sha256=item.text_sha256,
    )
    db.add(review)

    outbox_created = False
    if item.envelope_id:
        existing_outbox = await db.scalar(
            select(CollectorOutbox).where(
                CollectorOutbox.tenant_id == item.tenant_id,
                CollectorOutbox.sink == "knowledge",
                CollectorOutbox.scope == item.scope,
                CollectorOutbox.envelope_id == item.envelope_id,
            )
        )
        if not existing_outbox:
            outbox = CollectorOutbox(
                job_id=item.job_id,
                tenant_id=item.tenant_id,
                scope=item.scope,
                sink="knowledge",
                envelope_id=item.envelope_id,
                payload_hash=item.payload_hash,
                status="pending",
            )
            db.add(outbox)
            outbox_created = True

    await db.flush()
    return {
        "item_id": str(item.id),
        "status": "accepted",
        "delivery_queued": outbox_created,
        "reasons": item.reason_codes,
        "review": {"decision": "accept", "meaning": "document_utility_review_only", "factuality": "unverified"},
    }


async def run_views(db, mission: CollectionMission, job_rows, *, detailed: bool) -> list[dict]:
    """Per-run budget use and results, newest first. `collected` is new or changed content, `unchanged` is
    a conditional fetch that matched the stored copy (never delivered again), `imported` is a KB receipt."""
    current = mission.current_run_seq or 1
    stored = {run.run_seq: run for run in await db.scalars(select(CollectionMissionRun).where(
        CollectionMissionRun.mission_id == mission.id))}
    if current not in stored:
        stored[current] = CollectionMissionRun(mission_id=mission.id, run_seq=current, trigger="initial", started_at=mission.created_at,
                                               page_budget=mission.plan["budget"]["pages"], extra_pages=0)
    pages: dict[int, int] = {}
    active: set[int] = set()
    for job, seq in job_rows:
        if job.stage != "mission_prepare":
            pages[seq] = pages.get(seq, 0) + (job.cursor or {}).get("processed", 0)
        if job.status not in TERMINAL:
            active.add(seq)
    collected: dict[int, int] = {}
    unchanged: dict[int, int] = {}
    imported: dict[int, int] = {}
    if job_rows:
        item_filters = (CollectionMissionJob.mission_id == mission.id, CollectorItem.tenant_id == mission.tenant_id)
        for seq, gathered, same in (await db.execute(select(
            CollectionMissionJob.run_seq,
            func.count(CollectorItem.id).filter(CollectorItem.raw_sha256.is_not(None), CollectorItem.status != "not_modified"),
            func.count(CollectorItem.id).filter(CollectorItem.status == "not_modified"),
        ).join(CollectorItem, CollectorItem.job_id == CollectionMissionJob.job_id).where(*item_filters)
                .group_by(CollectionMissionJob.run_seq))).all():
            collected[seq], unchanged[seq] = gathered, same
        for seq, count in (await db.execute(select(
            CollectionMissionJob.run_seq, func.count(CollectorItem.id.distinct()),
        ).join(CollectorItem, CollectorItem.job_id == CollectionMissionJob.job_id).join(
            CollectorDelivery, CollectorDelivery.item_id == CollectorItem.id).where(
                *item_filters, CollectorDelivery.outcome == "imported").group_by(CollectionMissionJob.run_seq))).all():
            imported[seq] = count
    ordered = sorted(stored.values(), key=lambda run: run.run_seq, reverse=True)
    return [{"run_seq": run.run_seq, "trigger": run.trigger, "started_at": run.started_at.isoformat(),
             "page_budget": run.page_budget + run.extra_pages, "pages_used": pages.get(run.run_seq, 0),
             "collected": collected.get(run.run_seq, 0), "unchanged": unchanged.get(run.run_seq, 0),
             "imported": imported.get(run.run_seq, 0), "in_progress": run.run_seq in active}
            for run in ordered[: 20 if detailed else 1]]


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
                await jobs.cancel(db, job.tenant_id, "personal", job.id)
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
    """Raise the budget of the current run only; later runs start from the configured per-run budget again."""
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
    run = await get_run(db, mission)
    run.extra_pages += add_pages
    mission.gaps = [g for g in mission.gaps if g.get("reason") != "mission_page_budget"]
    mission.status = "running"
    await db.flush()
    await reconcile_mission(db, mission)
    return mission


async def rerun(db, mission_id: uuid.UUID, *, tenant_id: str = "owner") -> CollectionMission:
    """Start a new run with a fresh per-run budget. Fetching stays conditional, so unchanged documents are not reloaded."""
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
                await jobs.cancel(db, job.tenant_id, "personal", job.id)
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
    run = await begin_run(db, mission, "rerun")
    await reconcile_mission(db, mission)
    if needs_preparation(mission.plan):
        await queue_preparation(db, mission, run.run_seq)
    mission.status = "running"
    await db.flush()
    return mission


async def schedule_due_runs(*, now: datetime | None = None) -> int:
    """Start due scheduled runs of missions. Returns the number of runs started."""
    from ttakkari.db import sessionmaker

    now = now or datetime.now(UTC)
    async with sessionmaker()() as db:
        keys = list(await db.scalars(select(CollectorSource.source_key).where(
            CollectorSource.enabled.is_(True), CollectorSource.interval_minutes.is_not(None),
            CollectorSource.next_run_at <= now, CollectorSource.source_key.startswith("mission:"))
            .order_by(CollectorSource.next_run_at).limit(50)))
    due: list[uuid.UUID] = []
    for key in keys:
        try:
            mission_id = uuid.UUID(key.split(":")[1])
        except (IndexError, ValueError):
            continue
        if mission_id not in due:
            due.append(mission_id)
    started = 0
    for mission_id in due[:10]:
        try:
            async with sessionmaker()() as db:
                started += await start_scheduled_run(db, mission_id, now=now)
                await db.commit()
        except Exception:  # one mission's failure must not stop other missions or the control loop
            log.exception("scheduled mission run failed mission_id=%s", mission_id)
    return started


async def start_scheduled_run(db, mission_id: uuid.UUID, *, now: datetime, tenant_id: str = "owner") -> int:
    """One scheduled tick of a mission: a new run, or a recorded skip while the previous run is still in progress."""
    await store._lock(db, "mission-control", tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == tenant_id
    ).with_for_update().execution_options(populate_existing=True))
    if mission is None:
        return 0
    sources = list(await db.scalars(select(CollectorSource).where(
        CollectorSource.tenant_id == tenant_id, CollectorSource.scope == "personal",
        CollectorSource.source_key.startswith(f"mission:{mission_id}:")).order_by(CollectorSource.id).with_for_update()))
    schedule = mission.plan.get("schedule") or {}
    interval = schedule.get("interval_minutes")

    def advance() -> None:
        for source in sources:
            if source.enabled:
                source.interval_minutes = interval
                source.next_run_at = now + timedelta(minutes=interval) if interval else None

    if mission.status in ("paused", "cancelled") or not interval:
        advance()
        return 0
    # The due list is read before taking the mission lock. Another scheduler tick may have
    # advanced these source rows while this tick waited, so only a still-due row may start a run.
    if not any(source.enabled and source.interval_minutes is not None and source.next_run_at is not None
               and source.next_run_at <= now for source in sources):
        return 0
    in_progress = await db.scalar(select(func.count()).select_from(CollectorJob).join(
        CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id).where(
            CollectionMissionJob.mission_id == mission.id, CollectorJob.status.not_in(TERMINAL)))
    if in_progress:
        mission.plan = {**mission.plan, "schedule": {
            **schedule, "skipped_runs": int(schedule.get("skipped_runs", 0)) + 1,
            "last_skip": {"at": now.isoformat(), "run_seq": mission.current_run_seq, "reason": "previous_run_in_progress"}}}
        advance()
        return 0
    run = await begin_run(db, mission, "scheduled")
    mission.gaps = []
    await reconcile_mission(db, mission)
    if needs_preparation(mission.plan):
        await queue_preparation(db, mission, run.run_seq)
    advance()
    await db.flush()
    return 1


async def update_schedule(db, mission_id: uuid.UUID, *, interval_minutes: int | None, tenant_id: str = "owner") -> CollectionMission:
    if interval_minutes is not None and not 1 <= interval_minutes <= 43_200:
        raise ValueError("수집 주기는 1분에서 30일(43,200분) 사이여야 합니다.")
    await store._lock(db, "mission-control", tenant_id, str(mission_id))
    mission = await db.scalar(select(CollectionMission).where(
        CollectionMission.id == mission_id, CollectionMission.tenant_id == tenant_id
    ).with_for_update())
    if mission is None:
        raise ValueError("수집 임무를 찾을 수 없습니다.")
    now = datetime.now(UTC)
    mission.plan = {
        **mission.plan,
        "schedule": {
            **(mission.plan.get("schedule") or {}),
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
