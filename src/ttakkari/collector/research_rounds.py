"""Durable research rounds on the existing collector jobs.

Nothing here is a new queue or crawler. A round is another `mission_prepare` job of the same mission, claimed, fenced,
heartbeated and cancelled like every other job. It reads what the acquisition jobs actually collected, updates coverage
from that evidence, searches only what is still missing, and ends with an explicit stop reason. Provider failures are
recorded as failures (and retried by the job's own retry path), never folded into "no results".
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from ttakkari.collector import paper, research
from ttakkari.collector_models import (
    CollectionMission,
    CollectionMissionJob,
    CollectionMissionRun,
    CollectorItem,
    CollectorJob,
)

NO_NEW_STOP = 2  # consecutive searches without a new source before the first round gives up
FOLLOWUP_NO_NEW_STOP = 3  # follow-up queries were chosen on purpose, so they get a little more patience
_STATE_KEYS = {"run_seq": None, "round": 0, "search_cursor": 0, "no_new": 0, "searches_used": 0, "failed": [], "stop": None,
               "rounds": [], "accepted_total": 0, "model_calls": 0}


def state(plan: dict[str, Any], run_seq: int, seed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Research state of one run. A plan from before research rounds is seeded from the job cursor and the search history."""
    current = plan.get("research") or {}
    if current.get("run_seq") == run_seq:
        return {**_STATE_KEYS, **current}
    used = sum(1 for entry in plan.get("search_history") or [] if entry.get("run_seq", 1) == run_seq) if run_seq == 1 else 0
    seed = seed or {}
    return {**_STATE_KEYS, "run_seq": run_seq, "searches_used": used, "search_cursor": int(seed.get("search_cursor", 0)),
            "no_new": int(seed.get("no_new", 0))}


# ---- evidence ----------------------------------------------------------------------------------------------------

async def evidence(db, mission: CollectionMission, *, text_limit: int = 0, limit: int = 400) -> list[dict[str, Any]]:
    """Collected documents of the mission (all runs), one row per item. Text is bounded in SQL, never loaded whole."""
    from ttakkari.collector.control_models import CollectorDelivery

    page = CollectorItem.envelope["extensions"]["collector:page"]["title"].astext
    topics = CollectorItem.envelope["extensions"]["collector:mission"]["reference"]["topics"]
    columns = [CollectorItem.id, CollectorItem.url, CollectorItem.final_url, CollectorItem.status, CollectorItem.reason_codes,
               page.label("title"), topics.label("topics"), CollectorDelivery.detail["document_id"].astext.label("document_id")]
    if text_limit:
        columns.append(func.substr(CollectorItem.envelope["normalized"]["text"].astext, 1, text_limit).label("text"))
    rows = (await db.execute(
        select(*columns).join(CollectionMissionJob, CollectionMissionJob.job_id == CollectorItem.job_id)
        .outerjoin(CollectorDelivery, (CollectorDelivery.item_id == CollectorItem.id) & (CollectorDelivery.outcome == "imported"))
        .where(CollectionMissionJob.mission_id == mission.id, CollectorItem.tenant_id == mission.tenant_id,
               CollectorItem.kind == "document", CollectorItem.status.in_(("accepted", "held", "failed", "blocked")))
        .order_by(CollectorItem.created_at.desc()).limit(limit))).all()
    # A source found again by another topic's query gained that topic after its job started, so topics come from the plan.
    plan_topics = {source["url"]: source.get("topics") or [] for source in mission.plan.get("sources", [])}
    seen, out = set(), []
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        duplicate = "exact_duplicate" in (row.reason_codes or [])
        out.append({"id": str(row.id), "url": row.final_url or row.url, "requested_url": row.url,
                    "status": "duplicate" if duplicate and row.status == "accepted" else row.status,
                    "title": row.title or "",
                    "topics": sorted({*(row.topics if isinstance(row.topics, list) else []), *plan_topics.get(row.url, [])}),
                    "document_id": row.document_id, "text": (getattr(row, "text", None) or "")})
    return out


async def view_block(db, mission: CollectionMission) -> dict[str, Any] | None:
    """Live coverage for the mission view, recomputed from collected items on every read."""
    plan = mission.plan
    if not (plan.get("coverage") or plan.get("research") or plan.get("papers")):
        return None
    coverage = research.compute_coverage(plan, await evidence(db, mission))
    st = state(plan, mission.current_run_seq or 1)
    papers = plan.get("papers") or []
    return {"coverage": coverage, "remaining": research.coverage_gaps(coverage), "round": st["round"], "stop": st["stop"],
            "searches_used": st["searches_used"], "search_budget": plan["budget"].get("searches"), "model_calls": 0,
            "papers": {"resolved": len(papers), "fulltext": sum(p["fulltext"] == "allowed" for p in papers),
                       "metadata_only": sum(p["fulltext"] != "allowed" for p in papers)},
            "rounds": st["rounds"][-5:]}


# ---- searching ---------------------------------------------------------------------------------------------------

async def _run_providers(query: str, topic: str | None) -> dict[str, Any]:
    from ttakkari.collector import adapters

    providers: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    calls = [("duckduckgo_html", lambda: adapters.search_web(query))]
    if topic == "papers" and re.search(r"[A-Za-z]{3}", query):
        calls.append(("arxiv_api", lambda: paper.search_arxiv(query)))
    for name, call in calls:
        try:
            found = await call()
        except Exception as exc:  # noqa: BLE001  any provider failure is a recorded failure, never an empty result
            providers.append({"name": name, "status": "failed", **research.classify_search_error(exc)})
            continue
        providers.append({"name": name, "status": "ok", "candidates": len(found)})
        candidates += found
    failed = [p for p in providers if p["status"] == "failed"]
    return {"candidates": candidates, "providers": providers,
            "status": "failed" if len(failed) == len(providers) else "ok",
            "error": failed[0]["code"] if failed else None, "retryable": any(p["retryable"] for p in failed)}


async def search_phase(job, ctx, mission_id: uuid.UUID, done: list[str]):
    """Run the pending queries of the current run. Returns a HandlerResult only when the job must end early."""
    from ttakkari.collector import jobs, missions
    from ttakkari.collector.runner import HandlerResult
    from ttakkari.db import sessionmaker

    tried: set[int] = set()
    consecutive_failures = 0
    while True:
        async with sessionmaker()() as db:
            mission = await db.get(CollectionMission, mission_id)
            plan, run_seq = mission.plan, mission.current_run_seq or 1
        st = state(plan, run_seq, job.cursor)
        queries = plan.get("queries", [])
        retry = next((f["index"] for f in st["failed"] if f["retryable"] and f["index"] not in tried and f["index"] < len(queries)), None)
        index = retry if retry is not None else st["search_cursor"]
        patience = NO_NEW_STOP if st["round"] == 0 else FOLLOWUP_NO_NEW_STOP
        if (index >= len(queries) or st["no_new"] >= patience or st["searches_used"] >= plan["budget"]["searches"]
                or consecutive_failures >= 3):
            return None
        query = queries[index]
        topic = (plan.get("query_topics") or {}).get(query)
        outcome = await _run_providers(query, topic)
        await ctx.heartbeat()
        tried.add(index)
        consecutive_failures = consecutive_failures + 1 if outcome["status"] == "failed" else 0
        progress: dict[str, Any] = {}

        async def record(db, _outcome=outcome, _index=index, _query=query, _topic=topic, _run=run_seq, _progress=progress):
            owned = await jobs._fenced_job(db, job.id, job.attempt_token)
            if owned.cancel_requested:
                return owned.cursor, []
            current = await db.get(CollectionMission, mission_id, with_for_update=True)
            plan = current.plan
            st = state(plan, _run, job.cursor)
            known = {source["url"]: source for source in plan["sources"]}
            additions = []
            for candidate in _outcome["candidates"]:
                topics = [_topic] if _topic else []
                if candidate["url"] in known:
                    existing = known[candidate["url"]]
                    existing["topics"] = sorted({*(existing.get("topics") or []), *topics})
                    continue
                entry = {**candidate, "topics": topics}
                known[candidate["url"]] = entry
                additions.append(entry)
            st["searches_used"] += 1
            failed = [f for f in st["failed"] if f["index"] != _index]
            if _outcome["status"] == "failed":
                previous = next((f for f in st["failed"] if f["index"] == _index), {"attempts": 0})
                failed.append({"index": _index, "code": _outcome["error"], "retryable": _outcome["retryable"],
                               "attempts": previous["attempts"] + 1})
            else:
                st["no_new"] = 0 if additions else st["no_new"] + 1
            if _index == st["search_cursor"]:
                st["search_cursor"] += 1
            st["failed"] = failed
            history = [*(plan.get("search_history") or []),
                       {"query": _query, "candidates": len(_outcome["candidates"]), "new_sources": len(additions),
                        "provider": "duckduckgo_html", "providers": _outcome["providers"], "status": _outcome["status"],
                        "error": _outcome["error"], "run_seq": _run, "round": st["round"], "topic": _topic}]
            stop = ("diminishing_returns" if st["no_new"] >= (NO_NEW_STOP if st["round"] == 0 else FOLLOWUP_NO_NEW_STOP) else "search_budget" if st["searches_used"] >= plan["budget"]["searches"]
                    else "query_budget" if st["search_cursor"] >= len(plan["queries"]) and not failed else None)
            current.plan = {**plan, "sources": [*plan["sources"], *additions], "search_history": history, "research": st,
                            "search_stop": stop}
            await missions.launch(db, current)
            _progress["state"] = st
            current.gaps = _search_gaps(current.gaps, current.plan, st)
            return {"prepared": list(done), "search_cursor": st["search_cursor"], "no_new": st["no_new"]}, []

        if await ctx.commit_work(record, stage="mission_prepare") == "cancelled":
            return HandlerResult("failed", {"code": "cancelled"})


def _search_gaps(gaps: list[dict], plan: dict, st: dict) -> list[dict]:
    kept = [g for g in gaps if g.get("reason") not in ("search_provider_failed", "no_search_candidates")]
    if st["failed"]:
        codes = sorted({f["code"] for f in st["failed"]})
        kept.append({"reason": "search_provider_failed", "message": "검색 공급자 호출이 실패해 일부 검색어를 실행하지 못했습니다.",
                     "queries": len(st["failed"]), "codes": codes})
    elif not plan["sources"]:
        return [{"reason": "no_search_candidates", "message": "검색에서 수집할 출처를 찾지 못했습니다."}, *kept]
    return kept


# ---- follow-up round -----------------------------------------------------------------------------------------------

async def expansion_step(job, ctx, mission_id: uuid.UUID, round_no: int, done: list[str]):
    """Update coverage and context relations from collected evidence, then queue what is still missing."""
    from ttakkari.collector import jobs
    from ttakkari.collector.runner import HandlerResult
    from ttakkari.db import sessionmaker

    async with sessionmaker()() as db:
        mission = await db.get(CollectionMission, mission_id)
        plan, run_seq = mission.plan, mission.current_run_seq or 1
        items = await evidence(db, mission, text_limit=20_000)
        run = await db.get(CollectionMissionRun, (mission_id, run_seq))
        started = (run.started_at if run else mission.created_at)
    documents = [item for item in items if item["status"] == "accepted" and item["text"]]
    st = state(plan, run_seq, job.cursor)
    if st["round"] >= round_no:
        return None  # a retried attempt of a round that already committed
    coverage = research.compute_coverage(plan, items)
    accepted_total = sum(1 for item in items if item["status"] == "accepted")
    new_documents = accepted_total - st["accepted_total"] if st["round"] >= 1 else None
    budget_left = plan["budget"]["searches"] - st["searches_used"]
    plan_for_followups = {**plan, "research": st}
    found = research.followups(plan_for_followups, documents, coverage, budget_left=budget_left)
    unresolved = [f for f in st["failed"]]
    stop = "provider_failed" if unresolved else research.decide_stop(
        plan, {**st, "round": round_no - 1}, now=datetime.now(UTC), started_at=started, coverage=coverage,
        searches_used=st["searches_used"], new_documents=new_documents,
        followup_count=len(found["queries"]) + len(found["papers"]))
    claims = research.context_claims((plan.get("context") or {}).get("text", ""))
    targets = [{"kind": "kb_document" if item["document_id"] else "collected_document", "url": item["url"], "title": item["title"],
                "document_id": item["document_id"], "text": item["text"]} for item in documents]
    targets += [{"kind": "paper", "url": p["canonical_url"], "title": p["title"], "identifier": p["identifier"],
                 "text": " ".join(filter(None, [p["title"], p.get("abstract") or ""]))} for p in plan.get("papers") or []]
    links = research.link_context(claims, targets) if claims else None

    async def commit(db, _stop=stop):
        owned = await jobs._fenced_job(db, job.id, job.attempt_token)
        if owned.cancel_requested:
            return owned.cursor, []
        from ttakkari.collector import missions

        current = await db.get(CollectionMission, mission_id, with_for_update=True)
        plan = current.plan
        st = state(plan, run_seq, job.cursor)
        known_urls = {source["url"] for source in plan["sources"]}
        added_queries = [] if _stop else found["queries"]
        queries = [*plan["queries"], *(q for q, _, _ in added_queries)]
        query_topics = {**plan.get("query_topics", {}), **{q: topic for q, topic, _ in added_queries}}
        deferred = [q for q in plan.get("deferred_queries", []) if q not in {a for a, _, _ in added_queries}]
        papers_known = {p["identifier"].lower() for p in plan.get("papers") or []}
        new_papers = []
        for ref in found["papers"]:
            if len([s for s in plan["sources"] if s["adapter"] == "paper"]) + len(new_papers) >= research.MAX_PAPERS:
                break
            if ref["canonical_url"] in known_urls or ref["identifier"].lower() in papers_known:
                continue
            new_papers.append({"url": ref["canonical_url"], "adapter": "paper", "provenance": "reference_list",
                               "identifier": ref["identifier"], "topics": ref["topics"], "cited_by": ref["cited_by"]})
        round_log = {"round": round_no, "at": datetime.now(UTC).isoformat(), "queries": [q for q, _, _ in added_queries],
                     "reasons": sorted({r for _, _, r in added_queries}), "new_papers": len(new_papers),
                     "new_documents_since_previous": new_documents, "documents": accepted_total,
                     "duplicates": sum(1 for item in items if item["status"] == "duplicate"), "stop": _stop}
        st.update(round=round_no, no_new=0, accepted_total=accepted_total, stop=_stop, rounds=[*st["rounds"], round_log][-10:])
        gaps = [g for g in current.gaps if g.get("reason") != "research_coverage_incomplete"]
        if _stop and _stop != "coverage_satisfied" and research.coverage_gaps(coverage):
            gaps.append({"reason": "research_coverage_incomplete", "stop": _stop, "topics": research.coverage_gaps(coverage),
                         "message": "일부 영역은 충분히 확보하지 못한 채 탐색을 마쳤습니다."})
        if _stop == "provider_failed":
            gaps = [g for g in gaps if g.get("reason") != "search_provider_failed"] + [
                {"reason": "search_provider_failed", "message": "검색 공급자 호출이 실패해 탐색을 이어갈 수 없었습니다.",
                 "queries": len(st["failed"]), "codes": sorted({f["code"] for f in st["failed"]})}]
        current.plan = {**plan, "queries": queries, "query_topics": query_topics, "deferred_queries": deferred,
                        "sources": [*plan["sources"], *new_papers], "coverage": coverage, "research": st,
                        "glossary": {**(plan.get("glossary") or {}), **found["glossary"]},
                        **({"context_links": {**links, "computed_at": datetime.now(UTC).isoformat()}} if links else {})}
        current.gaps = gaps
        await missions.launch(db, current)
        return {"prepared": list(done), "search_cursor": st["search_cursor"], "no_new": 0}, []

    if await ctx.commit_work(commit, stage="mission_prepare") == "cancelled":
        return HandlerResult("failed", {"code": "cancelled"})
    return None


# ---- papers ----------------------------------------------------------------------------------------------------

async def paper_phase(job, ctx, mission_id: uuid.UUID, done: list[str]):
    """Resolve DOI/arXiv sources into metadata and an observed license. Without an open license: metadata only."""
    from ttakkari.collector import jobs, missions
    from ttakkari.collector.runner import HandlerResult
    from ttakkari.db import sessionmaker

    while True:
        async with sessionmaker()() as db:
            mission = await db.get(CollectionMission, mission_id)
            plan, run_seq = mission.plan, mission.current_run_seq or 1
        resolved = {p["identifier"].lower() for p in plan.get("papers") or []}
        failures = plan.get("paper_failures") or {}
        target = None
        for entry in plan["sources"]:
            ref = research.paper_reference_of_url(entry["url"]) if entry["adapter"] in ("paper", "paper_pdf") else None
            if ref is None or ref["identifier"].lower() in resolved:
                continue
            failure = failures.get(ref["identifier"].lower())
            if failure and (failure["run_seq"] == run_seq and not failure["retryable"] or failure["attempt"] == str(job.attempt_token)):
                continue
            target = (entry, ref)
            break
        if target is None:
            return None
        entry, ref = target
        record, failure = None, None
        try:
            record = await paper.resolve(ref)
        except Exception as exc:  # noqa: BLE001
            failure = research.classify_search_error(exc) | ({"code": exc.code} if isinstance(exc, paper.PaperError) else {})
            if isinstance(exc, paper.PaperError):
                failure["retryable"] = False
        await ctx.heartbeat()

        async def persist(db, _entry=entry, _ref=ref, _record=record, _failure=failure, _run=run_seq):
            owned = await jobs._fenced_job(db, job.id, job.attempt_token)
            if owned.cancel_requested:
                return owned.cursor, []
            current = await db.get(CollectionMission, mission_id, with_for_update=True)
            plan = current.plan
            key = _ref["identifier"].lower()
            gaps = [g for g in current.gaps if g.get("identifier", "").lower() != key]
            failures = {k: v for k, v in (plan.get("paper_failures") or {}).items() if k != key}
            papers = list(plan.get("papers") or [])
            sources = list(plan["sources"])
            if _failure is not None:
                failures[key] = {**_failure, "run_seq": _run, "attempt": str(job.attempt_token)}
                gaps.append({"url": _entry["url"], "identifier": _ref["identifier"], "reason": "paper_metadata_failed",
                             "message": "논문 메타데이터를 확인하지 못했습니다: " + str(_failure["code"])})
            else:
                papers.append({**{k: v for k, v in _record.items() if k != "pdf_url" or _record["fulltext"] == "allowed"},
                               "via": _entry.get("provenance"), "topics": _entry.get("topics") or []})
                doc = paper.document_for(_record, _entry.get("topics"))
                if doc is not None:
                    if doc["url"] not in {s["url"] for s in sources}:
                        sources.append(doc)
                    await missions.register_document_license(db, current, doc)
                else:
                    gaps.append({"url": _entry["url"], "identifier": _ref["identifier"], "reason": "paper_rights_unregistered",
                                 "message": _record["rights_note"]})
            current.plan = {**plan, "papers": papers[:research.MAX_PAPERS * 2], "sources": sources, "paper_failures": failures}
            current.gaps = gaps
            await missions.launch(db, current)
            return {"prepared": list(done), "search_cursor": state(current.plan, _run)["search_cursor"], "no_new": 0}, []

        if await ctx.commit_work(persist, stage="mission_prepare") == "cancelled":
            return HandlerResult("failed", {"code": "cancelled"})


def outstanding_failures(plan: dict[str, Any], run_seq: int) -> dict[str, Any]:
    st = state(plan, run_seq)
    retryable = [f for f in st["failed"] if f["retryable"]]
    papers = [k for k, v in (plan.get("paper_failures") or {}).items() if v.get("retryable") and v["run_seq"] == run_seq]
    return {"search": len(st["failed"]), "search_retryable": len(retryable), "papers_retryable": len(papers)}


# ---- scheduling the next round -------------------------------------------------------------------------------------

async def maybe_queue_round(db, mission: CollectionMission, run_seq: int, linked: list[CollectorJob], *, queued: int, active: set) -> None:
    """Queue the next follow-up round when this one is fully collected. Idempotent: one job per (run, round)."""
    from ttakkari.collector import missions

    plan = mission.plan
    if not research.needs_rounds(plan) or mission.status in ("paused", "cancelled") or queued or active:
        return
    st = state(plan, run_seq)
    prepares = [job for job in linked if job.stage == "mission_prepare"]
    if st["stop"] or st["round"] > research.MAX_ROUNDS or any(job.status not in missions.TERMINAL for job in prepares):
        return
    if plan["mode"] == "research" and not prepares:
        return  # the initial preparation job is queued right after the first launch
    if plan["mode"] == "mixed" and not linked:
        return  # no job has run yet, so there is nothing to relate the context to
    round_no = st["round"] + 1
    key = f"prepare:{mission.id}:{run_seq}:r{round_no}"
    if await db.scalar(select(CollectorJob.id).where(CollectorJob.tenant_id == mission.tenant_id, CollectorJob.scope == "personal",
                                                    CollectorJob.source_key == f"mission-prepare:{mission.id}", CollectorJob.request_key == key)):
        current = {**plan, "research": {**st, "stop": "round_job_failed"}}
        mission.plan = current  # a round job that ended without committing must not be re-queued forever
        return
    await missions.queue_preparation(db, mission, run_seq, round_no=round_no)
