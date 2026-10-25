"""Collector Platform: stage handler for web/feed collection jobs (CP-S2-03/04/06).

One claimed job walks a frontier of URLs: discover (feed/sitemap/links) -> policy+robots+paced fetch -> raw preserve ->
extract -> normalize -> language -> exact/near duplicate -> L0 rules -> profile -> optional L1 -> decision -> envelope.
No LLM is called anywhere on this path.

Transactions: network and CPU work happen outside any DB transaction. A page's items, dedupe bands, fetch-cache
validators, outbox rows and the next cursor are committed together by JobContext.commit_work (fenced by the attempt
token), so a crash re-runs at most one page and never half-commits it. Dedupe decisions run inside that transaction
under a per-tenant/scope advisory lock, so two jobs cannot both become the canonical copy of one text.

Accounting invariant (checked by tests): discovered == accepted + held + blocked + failed + not_modified +
discovery + skipped_limit + len(frontier). `discovered` counts URLs that entered the frontier. URLs found but kept out
by the source policy are counted separately in `skipped_policy`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from sqlalchemy import func, select

from ttakkari.collector import (
    parse,
    quality,
    render,
    store,
    textproc,
    triage,
    triage_ml,
)
from ttakkari.collector import profiles as profiles_module
from ttakkari.collector.blobs import BlobQuotaExceeded, BlobStore, default_blob_store
from ttakkari.collector.contracts import (
    CollectorContractError,
    compute_payload_hash,
    parse_iso_datetime,
    validate_envelope,
)
from ttakkari.collector.fetcher import FetchError, FetchLimits, SafeFetcher
from ttakkari.collector.jobs import ClaimedJob, OutboxItem
from ttakkari.collector.library import effective_pool, in_library


def _job_pool(job: Any) -> str:
    return effective_pool(getattr(job, "pool", None), getattr(job, "scope", None))
from ttakkari.collector.netguard import NetGuard, UrlRejected, normalize_url
from ttakkari.collector.policy import (
    CollectRequest,
    SourcePolicySpec,
    policy_version_label,
)
from ttakkari.collector.polite import PolicyBlocked, PoliteFetcher
from ttakkari.collector.runner import HandlerResult, JobContext, register_handler
from ttakkari.collector_models import CollectorItem
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker

log = logging.getLogger(__name__)

PIPELINE_VERSION = "collector-pipeline-1"
PRODUCER_ID = "ttakkari-collector"
CHUNKER_VERSION = "whole-document-1"
PRODUCT_TOKEN = "TtakkariCollector"
PAGE_SIZE = 5
MISSION_BATCH_PAGES = 25
MAX_URL_TRIES = 3  # architecture 4: at most three attempts per URL
MAX_SEEN = 2000
MAX_NEST = 2  # feed/sitemap -> nested sitemap -> pages
STAGE_DISCOVER = "discover"
STAGE_PROCESS = "process"
EXPORT_SINK = "export"
COUNT_KEYS = ("accepted", "held", "blocked", "failed", "not_modified", "discovery", "skipped_limit", "skipped_policy")


def user_agent() -> str:
    contact = get_settings().collector_contact.strip()
    return f"{PRODUCT_TOKEN}/1.0" + (f" (+{contact})" if contact else "")


# ---- dependencies (replaced in tests) ------------------------------------------------------------------------

@dataclass
class Deps:
    polite: PoliteFetcher
    blobs: BlobStore
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


_deps: Deps | None = None
_default_fetcher: SafeFetcher | None = None


def configure(deps: Deps | None) -> None:
    global _deps
    _deps = deps


def get_deps() -> Deps:
    global _deps, _default_fetcher
    if _deps is None:
        _default_fetcher = SafeFetcher(NetGuard(), user_agent=user_agent(), limits=FetchLimits())
        _deps = Deps(PoliteFetcher(_default_fetcher, store.DbHostState(), product_token=PRODUCT_TOKEN), default_blob_store())
    return _deps


def install() -> None:
    """Register the stage handler with the consume loop. Idempotent."""
    register_handler(handle_job)


# ---- frontier cursor -----------------------------------------------------------------------------------------

def _entry(url: str, depth: int, *, parent_item: uuid.UUID | None = None, parent_url: str | None = None,
           nest: int = 0, rej: str | None = None) -> dict[str, Any]:
    return {"u": url, "d": depth, "pid": str(parent_item) if parent_item else None, "pu": parent_url, "n": nest,
            "t": 0, "rej": rej}


def _new_cursor(req: CollectRequest, spec: SourcePolicySpec) -> dict[str, Any]:
    frontier, seen = [], []
    for raw in req.seeds:
        try:
            parsed = normalize_url(raw, allowed_schemes=spec.schemes, allowed_ports=spec.ports)
        except UrlRejected as e:
            frontier.append(_entry(raw, 0, rej=e.code))
            continue
        key = store.url_key(parsed.url)[:16]
        if key in seen:
            continue
        seen.append(key)
        frontier.append(_entry(parsed.url, 0))
    return {"v": 1, "frontier": frontier, "seen": seen, "processed": 0, "discovered": len(frontier),
            "counts": dict.fromkeys(COUNT_KEYS, 0)}


# ---- per-URL work (no DB transaction held) --------------------------------------------------------------------

@dataclass
class Analysis:
    text: str
    normalized_steps: tuple[tuple[str, int], ...]
    truncated: bool
    extracted_sha256: str
    language: textproc.LanguageResult
    minhash: bytes | None
    l0: quality.L0Result
    profile_result: profiles_module.ProfileResult
    extracted: parse.ExtractedDocument
    l1: quality.L1Outcome


@dataclass
class Prepared:
    entry: dict[str, Any]
    item_id: uuid.UUID
    url: str
    url_key: str
    kind: str = "document"
    status: str = "failed"  # decided for non-document outcomes; documents are decided in the page transaction
    stage: str = "fetched"
    final_url: str | None = None
    http_status: int | None = None
    fetched_at: datetime | None = None
    raw_sha256: str | None = None
    raw_size: int | None = None
    raw_handle: str | None = None
    raw_unpreserved: bool = False
    media_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    redirects: tuple[str, ...] = ()
    robots: str = "not_applicable"
    errors: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    analysis: Analysis | None = None
    discovered: list[tuple[str, int, int]] = field(default_factory=list)  # (url, depth, nest)
    retry: tuple[float, str] | None = None  # (retry_after seconds, code) when the URL is deferred
    not_modified_of: uuid.UUID | None = None
    cache_hit: bool = False


def _err(stage: str, code: str, detail: str, *, retryable: bool = False, not_before: datetime | None = None) -> dict:
    return {"stage": stage, "code": code, "retryable": retryable,
            "not_before": not_before.isoformat() if not_before else None, "detail": detail or code}


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


async def _prepare(
    entry: dict[str, Any], job: ClaimedJob, spec: SourcePolicySpec, req: CollectRequest, deps: Deps
) -> Prepared:
    raw_url = entry["u"]
    item_id = uuid.uuid4()
    p = Prepared(entry, item_id, raw_url, store.url_key(raw_url))
    if entry.get("rej"):
        p.status, p.stage = "blocked", "discovered"
        p.reasons, p.errors = [entry["rej"]], [_err("fetch", entry["rej"], raw_url)]
        return p
    parsed = normalize_url(raw_url, allowed_schemes=spec.schemes, allowed_ports=spec.ports)
    source_key = job.envelope["source"]["key"]
    async with sessionmaker()() as db:
        cache = await store.get_fetch_cache(db, job.tenant_id, _job_pool(job), source_key, p.url_key)
    etag, last_modified = (cache.etag, cache.last_modified) if cache else (None, None)
    try:
        polite = await deps.polite.fetch(parsed, spec, etag=etag, last_modified=last_modified)
    except PolicyBlocked as e:
        p.status, p.stage, p.reasons, p.errors = "blocked", "fetch", [e.code], [_err("fetch", e.code, e.detail)]
        return p
    except UrlRejected as e:
        p.status, p.stage, p.reasons, p.errors = "blocked", "fetch", [e.code], [_err("fetch", e.code, e.detail)]
        return p
    except FetchError as e:
        return _fetch_failure(p, e, deps)
    res = polite.result
    p.robots, p.fetched_at, p.http_status = polite.robots, deps.clock(), res.status
    p.final_url, p.redirects = res.final_url, res.redirects
    if res.not_modified:
        p.status, p.stage, p.cache_hit = "not_modified", "fetched", True
        p.not_modified_of = cache.last_item_id if cache else None
        p.reasons = ["not_modified_etag"]
        p.etag, p.last_modified = res.etag or etag, res.last_modified or last_modified
        return p
    p.etag, p.last_modified = res.etag, res.last_modified
    body = res.body
    link_charset = res.charset
    p.raw_sha256, p.raw_size = hashlib.sha256(body).hexdigest(), len(body)
    p.media_type = res.media_type or "application/octet-stream"
    if cache is not None and cache.last_raw_sha256 == p.raw_sha256:
        # Same bytes as the latest fetch of this URL: not a new revision. (A -> B -> A is a new revision: B is the latest.)
        p.status, p.stage, p.cache_hit, p.not_modified_of = "not_modified", "fetched", True, cache.last_item_id
        p.reasons = ["not_modified_content_hash"]
        return p
    if cache is not None and cache.last_item_id is not None:
        p.signals["revision_of"] = str(cache.last_item_id)
    if spec.store_raw:
        p.raw_handle = await asyncio.to_thread(
            deps.blobs.put,
            body,
            lineage={
                "item_id": str(p.item_id),
                "job_id": str(job.id),
                "tenant_id": job.tenant_id,
                "scope": job.scope,
                "source_key": str((job.envelope.get("source") or {}).get("key", "")),
                "url": raw_url,
            },
        )
    else:
        p.raw_unpreserved = True
    detected = parse.detect_kind(body, res.media_type)
    p.signals["detected_kind"] = detected.kind
    if detected.kind == "unsupported":
        p.status, p.stage, p.reasons = "held", "fetched", [f"unsupported_media:{detected.reason}"]
        p.errors = [_err("extract", detected.reason or "unsupported_media", detected.media_type)]
        return p
    base = res.final_url
    if detected.kind in ("feed", "sitemap"):
        return await _discover(p, body, detected.kind, base, entry, spec)
    try:
        if detected.kind == "pdf":
            from ttakkari.collector.adapters import extract_pdf
            try:
                extracted = await extract_pdf(body)
            except (ValueError, TimeoutError) as exc:
                p.stage, p.status = "extracted", "held"
                p.reasons = [str(exc) if isinstance(exc, ValueError) else "pdf_parse_timeout"]
                p.errors = [_err("extract", p.reasons[0], "PDF 본문을 안전하게 추출하지 못했습니다.")]
                return p
        elif detected.kind == "html":
            # A script shell with no static text is rendered in an isolated browser; `body`/`link_charset` then follow the rendered DOM.
            extracted, body, link_charset = await render.extract_with_fallback(
                body, base, res.charset, p.signals, policy=spec, polite=deps.polite
            )
        else:
            extracted = await asyncio.to_thread(parse.extract_document, body, url=base, kind=detected.kind, charset=res.charset)
    except ValueError as e:
        p.stage, p.status = "extracted", "failed"
        p.errors, p.reasons = [_err("extract", str(e), "no main text could be extracted from the page")], [str(e)]
        if (p.signals.get("render") or {}).get("reason"):  # a script shell that could not be rendered says why
            p.reasons.append(p.signals["render"]["reason"])
        p.signals["decision_hint"] = "error"
        return p
    except Exception as e:  # noqa: BLE001  a parser crash on hostile markup is an item failure, not a job failure
        log.warning("extract crashed for %s: %s", base, type(e).__name__)
        p.stage, p.status = "extracted", "failed"
        p.errors, p.reasons = [_err("extract", "parser_error", type(e).__name__)], ["parser_error"]
        p.signals["decision_hint"] = "error"
        return p
    if detected.kind == "html" and entry["d"] < req.max_depth:
        links = await asyncio.to_thread(parse.extract_links, body, base, link_charset)
        p.discovered = [(u, entry["d"] + 1, 0) for u in links]
    p.analysis = await _analyze(extracted, base, res.last_modified, req, deps)
    p.stage = "scored"
    return p


def _fetch_failure(p: Prepared, e: FetchError, deps: Deps) -> Prepared:
    if e.retryable:
        wait = e.retry_after if e.retry_after else 0.0
        p.retry = (wait, e.code)
        p.errors = [_err("fetch", e.code, e.detail, retryable=True)]
        return p
    p.stage = "fetch"
    p.errors = [_err("fetch", e.code, e.detail)]
    p.reasons = [e.code]
    if e.code == "access_denied":
        p.status = "blocked"  # 401/403/CAPTCHA are not retried (architecture 4)
    elif e.code in ("response_too_large", "content_encoding_unsupported", "content_encoding_invalid"):
        p.status = "held"
    else:
        p.status = "failed"
    return p


async def _discover(p: Prepared, body: bytes, kind: str, base: str, entry: dict, spec: SourcePolicySpec) -> Prepared:
    try:
        if kind == "feed":
            urls = await asyncio.to_thread(parse.parse_feed, body, base)
            pages, nested = urls, []
        else:
            pages, nested = await asyncio.to_thread(parse.parse_sitemap, body, base)
    except Exception as e:  # noqa: BLE001  malformed XML is a held discovery item, the job continues
        p.kind, p.status, p.stage = "discovery", "held", "fetched"
        p.reasons, p.errors = [f"{kind}_unparseable"], [_err("discover", f"{kind}_unparseable", type(e).__name__)]
        return p
    p.kind, p.status, p.stage = "discovery", "discovered", "discovered"
    p.reasons = [f"{kind}_discovered"]
    nest = entry.get("n", 0)
    p.discovered = [(u, entry["d"], 0) for u in pages]
    if nest < MAX_NEST:
        p.discovered += [(u, entry["d"], nest + 1) for u in nested]
    p.signals.update({"discovered_pages": len(pages), "discovered_nested": len(nested)})
    return p


async def _analyze(
    extracted: parse.ExtractedDocument, url: str, last_modified_header: str | None, req: CollectRequest, deps: Deps
) -> Analysis:
    profile = profiles_module.get_profile(req.profile)
    params = profiles_module.resolve_params(profile, req.profile_params)

    def cpu() -> tuple[Any, ...]:
        norm = textproc.normalize_text(extracted.text)
        lang = textproc.identify_language(norm.text)
        sig = textproc.minhash_signature(norm.text)
        l0 = quality.l0_signals(norm.text, profile.l0_thresholds(params))
        return norm, lang, sig, l0

    norm, lang, sig, l0 = await asyncio.to_thread(cpu)
    result = profile.evaluate(
        profiles_module.ProfileInput(
            url=url, text=norm.text, title=extracted.title, published=extracted.published,
            published_precision=extracted.published_precision, last_modified=_parse_http_date(last_modified_header),
            fetched_at=deps.clock(), meta=extracted.meta,
        ),
        params,
    )
    l1 = await quality.run_l1(req.l1, norm.text, lang.language, textproc.NORMALIZER_VERSION)
    return Analysis(norm.text, norm.steps, norm.truncated or extracted.truncated, textproc.sha256_text(extracted.text), lang, sig, l0, result,
                    extracted, l1)


# ---- page transaction: dedupe, decision, envelope, persistence --------------------------------------------------

def _decide(p: Prepared, exact: CollectorItem | None, near: list[dict[str, Any]], req: CollectRequest, *, goal: str = "") -> tuple[str, list[str], list[dict]]:
    """(state, reason_codes, extra_errors). Only 'hold' reasons can come from quality; nothing here deletes a document."""
    errors: list[dict] = []
    analysis = p.analysis
    if analysis is None:
        if p.signals.get("decision_hint") == "error":
            return "error", p.reasons or ["extract_error"], errors
        return "hold", p.reasons or ["held"], errors
    if not analysis.text:
        errors.append(_err("extract", "empty_extraction", "text is empty after normalization"))
        return "error", ["empty_extraction"], errors
    holds = [*analysis.l0.flags, *analysis.profile_result.hold_reasons, *analysis.l1.hold_reasons]
    if exact is not None:
        holds.append("exact_duplicate")
    if req.l1.mode == "gate" and analysis.l1.error:
        holds.append("l1_unavailable")
    # C4: Lightweight multi-axis triage & goal relevance
    triage_outcome = triage.evaluate_triage(
        goal=goal, title=analysis.extracted.title or "", text=analysis.text,
        l0_signals=analysis.l0.signals, exact_duplicate=exact is not None,
        near_duplicates=near, truncated=analysis.truncated, ml=p.signals.get("ml"),
    )
    p.signals["triage"] = triage_outcome.signals
    if triage_outcome.action == "hold":
        holds.extend([r for r in triage_outcome.reasons if r not in holds])
    if holds:
        return "hold", holds, errors
    notes = ["l0_pass", *analysis.profile_result.notes, *analysis.l1.notes]
    if near:
        notes.append("near_duplicate_candidate")
    for r in triage_outcome.reasons:
        if r not in notes:
            notes.append(r)
    return "accept", notes, errors


def _job_goal(job: ClaimedJob) -> str:
    mission = (job.envelope.get("extensions") or {}).get("collector:mission")
    return (mission or {}).get("goal", "").strip() if mission else ""


async def _ml_page(job: ClaimedJob, prepared: list[Prepared]) -> None:
    """Stage-2 ML for one page with a single embedding child process, outside any DB transaction.
    Failures become an `unavailable` record on each document; the rule stage then decides alone."""
    targets = [p for p in prepared if p.analysis is not None and p.analysis.text]
    if not targets:
        return
    docs = [{"title": p.analysis.extracted.title or "", "text": p.analysis.text,
             "language": p.analysis.language.language} for p in targets]
    for p, result in zip(targets, await triage_ml.evaluate_documents(_job_goal(job), docs), strict=True):
        p.signals["ml"] = result


def _admit_precise(p: Prepared, pending_now: int) -> None:
    """Stage 3 state. The queue is capped per job; bounded evaluator status is recorded."""
    precise = p.signals.get("triage", {}).get("precise_eval")
    if not precise or not precise["needed"]:
        return
    from ttakkari.collector import precise_eval
    settings = get_settings()
    cap = settings.collector_triage_precise_queue_cap
    precise["cap"] = cap
    precise["engine"] = precise_eval.view_engine_status()
    if pending_now >= cap:
        precise["state"] = "skipped_cap"
        precise["reasons"] = [*precise["reasons"], "precise_queue_cap_reached"]


async def _dedupe(db, job: ClaimedJob, p: Prepared) -> tuple[CollectorItem | None, list[dict[str, Any]]]:
    a = p.analysis
    if a is None or not a.text:
        return None, []
    text_sha = textproc.sha256_text(a.text)
    exact = await store.canonical_by_text(db, job.tenant_id, _job_pool(job), text_sha, exclude_url_key=p.url_key)
    if exact is not None or a.minhash is None:
        return exact, []
    near: list[dict[str, Any]] = []
    for cand in await store.near_candidates(
        db, job.tenant_id, _job_pool(job), textproc.band_hashes(a.minhash), exclude_text_sha256=text_sha
    ):
        jac = textproc.estimated_jaccard(a.minhash, cand.minhash)
        if jac >= textproc.NEAR_THRESHOLD:
            near.append({"item_id": str(cand.id), "envelope_id": str(cand.envelope_id) if cand.envelope_id else None,
                         "cluster_id": cand.cluster_id, "jaccard": round(jac, 3), "url": cand.final_url or cand.url})
    near.sort(key=lambda n: -n["jaccard"])
    return None, near[:5]


def _measured_axis(value: float, scale: str, axis: dict[str, Any], *, query_id: str | None = None,
                   extra: list[str] = ()) -> dict[str, Any]:
    """Contract axis record. The validation state has no contract field, so it is stated in the evidence list."""
    return {
        "status": "measured", "value": value, "scale": scale, "model_id": axis["model_id"],
        "model_revision": axis["model_revision"], "preprocess_version": triage.PREPROCESS_VERSION,
        "calibration_id": None, "calibration_gold_hash": None, "calibrated_probability": None,
        "query_id": query_id, "evidence": [f"validation={axis['validation']}", axis["reason"][:200], *extra],
    }


def _fill_triage_axes(quality_axes: dict[str, dict[str, Any]], tri: dict[str, Any], goal: str) -> None:
    """Relevance: semantic cosine when the stage-2 model ran, else the lexical rule score. Education / spam come only
    from stage-2 heads. Axes an L1 scorer already measured are left alone."""
    axes = tri.get("axes", {})
    if goal and quality_axes.get("relevance", {}).get("status") == "unmeasured":
        query_id = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16]
        lexical = axes.get("relevance_lexical") or {
            "model_id": triage.MODEL_ID, "model_revision": triage.MODEL_REVISION, "validation": "unvalidated",
            "reason": "lexical rule heuristic",
        }
        sem = axes.get("topic_relevance_semantic")
        if sem and sem.get("status") == "measured":
            quality_axes["relevance"] = _measured_axis(
                float(sem["value"]), "similarity", sem, query_id=query_id,
                extra=[f"lexical_relevance={tri['scores']['relevance']:.3f}"])
        else:
            quality_axes["relevance"] = _measured_axis(
                float(tri["scores"]["relevance"]), "similarity", lexical, query_id=query_id,
                extra=[f"relevance={tri['scores']['relevance']:.3f}"])
    for name, key, scale in (("education", "information_value", "ordinal"), ("spam", "low_quality_likelihood", "raw_logit")):
        axis = axes.get(key)
        if axis and axis.get("status") == "measured" and quality_axes.get(name, {}).get("status") == "unmeasured":
            quality_axes[name] = _measured_axis(float(axis["value"]), scale, axis, extra=[f"axis={key}"])


def _build_envelope(
    job: ClaimedJob, spec: SourcePolicySpec, label: str, req: CollectRequest, p: Prepared, state: str,
    reasons: list[str], errors: list[dict], exact: CollectorItem | None, near: list[dict[str, Any]],
    cluster_id: str | None, now: datetime,
) -> dict[str, Any]:
    a = p.analysis
    src, access = job.envelope["source"], job.envelope["access"]
    envelope_id = uuid.uuid4()
    normalized = None
    published = None
    if a is not None and a.text:
        normalized = {
            "text": a.text, "sha256": textproc.sha256_text(a.text), "language": a.language.language,
            "parser_version": a.extracted.meta.get("parser_version", parse.PARSER_VERSION), "normalizer_version": textproc.NORMALIZER_VERSION,
            "chunker_version": CHUNKER_VERSION, "raw_alignment_handle": None, "truncated": a.truncated,
        }
        published = a.profile_result.published_at or a.extracted.published
    canonical_candidate = near[0]["envelope_id"] if near else (str(exact.envelope_id) if exact is not None else None)
    lineage = [f"job:{job.id}", f"item:{p.item_id}", f"raw:sha256:{p.raw_sha256}",
               f"seed:{p.url}" if not p.entry.get("pu") else f"discovered_from:{p.entry['pu']}"]
    if p.signals.get("revision_of"):
        lineage.append(f"revision_of:{p.signals['revision_of']}")
    ext: dict[str, Any] = {
        "collector:fetch": {
            "requested_url": p.url, "final_url": p.final_url, "http_status": p.http_status,
            "redirects": list(p.redirects), "etag": p.etag, "last_modified": p.last_modified, "robots": p.robots,
            "media_type": p.media_type,
            "canonical_url_candidate": a.extracted.canonical_url if a is not None else None,
        },
        "collector:lineage": {
            "pipeline_version": PIPELINE_VERSION, "raw_preserved": not p.raw_unpreserved,
            "extracted_sha256": a.extracted_sha256 if a is not None else None,
            "normalizer_steps": [list(s) for s in a.normalized_steps] if a is not None else [],
            "revision_of": p.signals.get("revision_of"),
        },
    }
    if p.signals.get("reprocessed_from"):
        ext["collector:lineage"]["reprocessed_from"] = p.signals["reprocessed_from"]
    mission = (job.envelope.get("extensions") or {}).get("collector:mission")
    if mission:
        ext["collector:mission"] = mission
        lineage.append(f"mission:{mission['id']}")
    quality_axes = quality.unmeasured_quality(textproc.NORMALIZER_VERSION)
    if a is not None:
        quality_axes = dict(a.l1.quality)
        if p.signals.get("triage"):
            ext["collector:triage"] = p.signals["triage"]
            _fill_triage_axes(quality_axes, p.signals["triage"], (mission or {}).get("goal", "").strip())
        if a.extracted.meta.get("adapter") == "pypdf":
            ext["collector:pdf"] = {"pages": json.loads(a.extracted.meta["pdf_pages"]),
                                    "total_pages": int(a.extracted.meta["pdf_total_pages"]),
                                    "page_offsets": "extracted_text_before_normalization"}
        if p.signals.get("render"):
            ext["collector:render"] = p.signals["render"]
        if a.extracted.title:
            ext["collector:page"] = {"title": a.extracted.title.strip()[:300]}
        ext["collector:l0"] = {"version": a.l0.version, "signals": a.l0.signals, "flags": list(a.l0.flags)}
        profile = profiles_module.get_profile(req.profile)
        ext["collector:profile"] = {
            "name": profile.name, "version": profile.version,
            "params": profiles_module.resolve_params(profile, req.profile_params),
            "signals": a.profile_result.signals,
        }
        ext["collector:l1"] = {"mode": req.l1.mode, "scorer": req.l1.scorer, "notes": list(a.l1.notes), "error": a.l1.error}
        ext["collector:language"] = {"confidence": a.language.confidence, "method": a.language.method,
                                     "version": textproc.LANGID_VERSION}
        if near:
            ext["collector:dedupe"] = {"near_candidates": near}
    errs = [*p.errors, *errors]
    return {
        "schema_version": "1.0.0",
        "envelope_id": str(envelope_id),
        "producer": {"id": PRODUCER_ID, "version": PIPELINE_VERSION},
        "recorded_at": now.isoformat(),
        "source": {
            "key": src["key"], "kind": "web", "url": p.final_url or p.url,
            "collected_at": (p.fetched_at or now).isoformat(),
            "published_at": published.isoformat() if published else None, "valid_from": None, "valid_to": None,
        },
        "access": {
            "tenant_id": job.tenant_id, "scope": access.get("scope", job.scope), "principal_ids": list(access["principal_ids"]),
            "sensitivity": access["sensitivity"],
        },
        "rights": spec.rights_block(label, p.fetched_at or now, p.robots),
        "raw": {
            "sha256": p.raw_sha256, "byte_size": p.raw_size, "media_type": p.media_type or "application/octet-stream",
            "blob_handle": p.raw_handle or f"unpreserved:sha256:{p.raw_sha256}",
        },
        "normalized": normalized,
        "quality": quality_axes,
        "fact_check_status": "unknown",
        "dedupe": {
            "exact_of": str(exact.envelope_id) if exact is not None and exact.envelope_id else None,
            "cluster_id": cluster_id, "canonical_candidate": canonical_candidate, "canonical_status": "unreviewed",
            "algorithm_version": textproc.DEDUPE_VERSION,
        },
        "decision": {"state": state, "reason_codes": reasons or ["unspecified"], "policy_version": label},
        "errors": errs,
        "lineage": lineage,
        "extensions": ext,
    }


STATE_FOR_STATUS = {"accepted": "accept", "held": "hold", "failed": "error", "blocked": "blocked"}


async def _persist(db, job: ClaimedJob, spec: SourcePolicySpec, label: str, req: CollectRequest, p: Prepared,
                   now: datetime) -> tuple[CollectorItem, OutboxItem | None]:
    a = p.analysis
    exact, near = (None, []) if a is None else await _dedupe(db, job, p)
    cluster_id = (near[0]["cluster_id"] or near[0]["item_id"]) if near else None
    status, reasons, extra_errors = p.status, p.reasons, []
    envelope: dict[str, Any] | None = None
    # An envelope exists whenever raw bytes exist for a document: accepted, held (incl. unsupported media) or error.
    if p.kind == "document" and p.raw_sha256 and (a is not None or p.status in ("held", "failed")):
        mission = (job.envelope.get("extensions") or {}).get("collector:mission")
        goal = mission.get("goal", "") if mission else ""
        state, reasons, extra_errors = _decide(p, exact, near, req, goal=goal)
        if p.signals.get("triage", {}).get("precise_eval", {}).get("needed"):
            pending_now = await db.scalar(select(func.count(CollectorItem.id)).where(
                CollectorItem.job_id == job.id,
                CollectorItem.signals["triage"]["precise_eval"]["state"].astext == "pending")) or 0
            _admit_precise(p, pending_now)
        status = {"accept": "accepted", "hold": "held", "error": "failed"}[state]
        try:
            envelope = validate_envelope(
                _build_envelope(job, spec, label, req, p, state, reasons, extra_errors, exact, near, cluster_id, now)
            )
        except CollectorContractError as e:
            # Never deliver something that fails the contract: keep the item, drop the envelope, say why.
            status = "failed"
            extra_errors.append(_err("envelope", "envelope_invalid", str(e)[:300]))
            reasons = [*reasons, "envelope_invalid"]
    errors = p.errors if envelope is not None else [*p.errors, *extra_errors]
    signals = dict(p.signals)
    if a is not None:
        signals["l0"] = {"signals": a.l0.signals, "flags": list(a.l0.flags), "version": a.l0.version}
        signals["profile"] = a.profile_result.signals
        if near:
            signals["near_candidates"] = near
    if p.not_modified_of:
        signals["not_modified_of"] = str(p.not_modified_of)
    source_key = job.envelope["source"]["key"]
    env_id = uuid.UUID(envelope["envelope_id"]) if envelope else None
    row = CollectorItem(
        id=p.item_id, job_id=job.id, tenant_id=job.tenant_id, scope=job.scope, source_key=source_key, kind=p.kind,
        status=status, stage=p.stage, url=p.url, url_key=p.url_key, final_url=p.final_url, depth=p.entry["d"],
        discovered_from=uuid.UUID(p.entry["pid"]) if p.entry.get("pid") else None, http_status=p.http_status,
        raw_sha256=p.raw_sha256, raw_size=p.raw_size, raw_handle=p.raw_handle, media_type=p.media_type,
        text_sha256=textproc.sha256_text(a.text) if a is not None and a.text else None,
        text_chars=len(a.text) if a is not None else None,
        language=a.language.language if a is not None else None,
        language_confidence=a.language.confidence if a is not None else None,
        minhash=a.minhash if a is not None and exact is None else None,
        exact_of=exact.id if exact is not None else None, cluster_id=cluster_id, profile=req.profile,
        decision_state=STATE_FOR_STATUS.get(status), reason_codes=reasons, errors=errors, signals=signals,
        envelope_id=env_id, envelope=envelope, payload_hash=compute_payload_hash(envelope) if envelope else None,
        parser_version=a.extracted.meta.get("parser_version", parse.PARSER_VERSION) if a is not None else None,
        normalizer_version=textproc.NORMALIZER_VERSION if a is not None else None, fetched_at=p.fetched_at,
        created_at=now,
    )
    db.add(row)
    await db.flush()
    if a is not None and a.minhash is not None and exact is None:
        store.add_bands(db, row.id, job.tenant_id, job.scope, textproc.band_hashes(a.minhash))
    if p.cache_hit:
        await store.touch_fetch_cache(db, job.tenant_id, _job_pool(job), source_key, p.url_key, p.etag, p.last_modified, now)
    elif p.raw_sha256 and status in ("accepted", "held", "discovered"):
        # Validators are kept only for outcomes a 304 may safely reuse. A failed extraction must be re-fetched.
        await store.put_fetch_cache(db, job.tenant_id, job.scope, source_key, p.url, etag=p.etag,
                                    last_modified=p.last_modified, raw_sha256=p.raw_sha256, item_id=row.id, at=now)
    outbox = OutboxItem(EXPORT_SINK, env_id, row.payload_hash) if status == "accepted" and env_id else None
    return row, outbox


def _advance(cursor: dict[str, Any], page: list[dict[str, Any]], prepared: list[Prepared], items: list[CollectorItem],
             spec: SourcePolicySpec, req: CollectRequest, *, preserve_frontier: bool = False) -> dict[str, Any]:
    """The cursor after this page: processed entries leave the frontier, deferred ones go to its tail, newly
    discovered URLs enter it (policy-filtered, de-duplicated, capped) and counters follow the final item statuses."""
    counts = dict(cursor["counts"])
    frontier = cursor["frontier"][len(page):]
    seen = list(cursor["seen"])
    seen_set = set(seen)
    processed = cursor["processed"]
    discovered = cursor["discovered"]
    deferred: list[dict[str, Any]] = []
    persisted = iter(items)
    for p in prepared:
        if p.retry is not None:
            entry = dict(p.entry)
            if p.retry[1] != "host_cooldown":
                entry["t"] += 1
            deferred.append(entry)
            continue
        item = next(persisted)
        processed += 1
        counts["discovery" if item.kind == "discovery" and item.status == "discovered" else item.status] += 1
        for url, depth, nest in p.discovered:
            try:
                parsed = normalize_url(url, allowed_schemes=spec.schemes, allowed_ports=spec.ports)
            except UrlRejected:
                counts["skipped_policy"] += 1
                continue
            if spec.check_url(parsed) is not None:
                counts["skipped_policy"] += 1
                continue
            key = store.url_key(parsed.url)[:16]
            if key in seen_set:
                continue
            seen_set.add(key)
            discovered += 1
            if len(seen) >= MAX_SEEN or (not preserve_frontier and processed + len(frontier) + len(deferred) >= req.max_pages):
                counts["skipped_limit"] += 1
                continue
            seen.append(key)
            frontier.append(_entry(parsed.url, depth, parent_item=p.item_id, parent_url=p.final_url or p.url, nest=nest))
    return {"v": 1, "frontier": frontier + deferred, "seen": seen, "processed": processed, "discovered": discovered,
            "counts": counts}


async def _finalize_page(
    db, job: ClaimedJob, spec: SourcePolicySpec, label: str, req: CollectRequest, cursor: dict[str, Any],
    page: list[dict[str, Any]], prepared: list[Prepared], now: datetime, result: dict[str, Any],
) -> tuple[dict[str, Any], list[OutboxItem]]:
    if any(p.analysis is not None for p in prepared):
        await store.lock_dedupe(db, job.tenant_id, job.scope)
    items: list[CollectorItem] = []
    outbox: list[OutboxItem] = []
    for p in prepared:
        if p.retry is not None:
            continue
        row, ob = await _persist(db, job, spec, label, req, p, now)
        items.append(row)
        if ob is not None:
            outbox.append(ob)
    new_cursor = _advance(cursor, page, prepared, items, spec, req,
                          preserve_frontier=bool(job.envelope.get("extensions", {}).get("collector:mission")))
    result["cursor"] = new_cursor
    return new_cursor, outbox


async def _storage_full(required_bytes: int = 0) -> bool:
    cap = get_settings().collector_blob_cap_bytes
    used_bytes = await asyncio.to_thread(get_deps().blobs.usage_bytes)
    if used_bytes >= cap or (required_bytes > 0 and used_bytes + required_bytes > cap):
        log.warning("collector storage cap reached used_bytes=%d cap_bytes=%d", used_bytes, cap)
        return True
    return False


def _order_quota_waiters(
    frontier: list[dict[str, Any]], *, used_bytes: int, cap_bytes: int
) -> tuple[list[dict[str, Any]], int]:
    """Prioritize quota retries with room, then fresh URLs; keep still-blocked URLs at the tail."""
    quota_ready: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for entry in frontier:
        required = entry.get("quota_bytes")
        if entry.get("t", 0) > 0 and isinstance(required, int):
            if used_bytes + required > cap_bytes:
                waiting.append(entry)
            else:
                quota_ready.append(entry)
        else:
            fresh.append(entry)
    return quota_ready + fresh + waiting, len(quota_ready) + len(fresh)


def _summary(cursor: dict[str, Any]) -> dict[str, Any]:
    return {"counts": cursor["counts"], "processed": cursor["processed"], "discovered": cursor["discovered"]}


async def handle_job(job: ClaimedJob, ctx: JobContext) -> HandlerResult:
    if getattr(job, "stage", None) == "mission_prepare":
        from ttakkari.collector import missions
        return await missions.prepare(job, ctx)
    deps = get_deps()
    source = job.envelope.get("source") or {}
    if source.get("kind") not in ("web", "feed"):
        return HandlerResult("quarantined", {"code": "unsupported_source_kind", "kind": source.get("kind")})
    async with sessionmaker()() as db:
        found = await store.active_policy(db, job.tenant_id, job.scope, source["key"])
    if found is None:
        return HandlerResult("policy_blocked", {"code": "source_policy_missing", "source_key": source["key"]})
    spec, version, digest = found
    if not spec.allows("store"):
        return HandlerResult("policy_blocked", {"code": "store_not_permitted", "source_key": source["key"]})
    try:
        req = CollectRequest.from_envelope(job.envelope, spec)
        if req.l1.scorer and req.l1.scorer.startswith("utility:"):
            from ttakkari.collector import quality_service
            async with sessionmaker()() as db:
                await quality_service.load_requested(db, job.tenant_id, job.scope, req.l1.scorer)
    except CollectorContractError as e:
        return HandlerResult("quarantined", {"code": "invalid_request", "detail": str(e)[:300]})
    except ValueError as e:
        return HandlerResult("quarantined", {"code": "quality_model_invalid", "detail": str(e)[:300]})
    label = policy_version_label(source["key"], version, digest)
    reprocess = (job.envelope.get("extensions") or {}).get("collector:reprocess")
    if reprocess is not None:
        if not spec.store_raw:
            return HandlerResult("policy_blocked", {"code": "raw_reprocessing_not_permitted"})
        try:
            original_id = uuid.UUID(str(reprocess["item_id"]))
            expected_raw_sha256 = str(reprocess["raw_sha256"])
        except (KeyError, TypeError, ValueError):
            return HandlerResult("quarantined", {"code": "invalid_reprocess_request"})
        return await _handle_reprocess(job, ctx, spec, label, req, original_id, expected_raw_sha256, deps)
    cursor = job.cursor if job.cursor and job.cursor.get("v") == 1 else _new_cursor(req, spec)
    mission = bool(job.envelope.get("extensions", {}).get("collector:mission"))
    batch_start = cursor["processed"]

    while cursor["frontier"] and cursor["processed"] < req.max_pages:
        # Move quota-blocked entries behind candidates that may dedupe to already durable orphans.
        # Stable partitioning scans the cursor once, preserves every URL, and does not increment
        # attempts or fetch a blocked entry. FileBlobStore.put remains the authoritative locked check.
        cap_bytes = get_settings().collector_blob_cap_bytes
        used_bytes = await asyncio.to_thread(deps.blobs.usage_bytes)
        ordered, ready_count = _order_quota_waiters(
            cursor["frontier"], used_bytes=used_bytes, cap_bytes=cap_bytes
        )
        if ready_count == 0:
            log.warning("collector storage cap waiting on %d frontier entries", len(cursor["frontier"]))
            return HandlerResult("retry_wait", {"code": "storage_cap_reached", **_summary(cursor)}, retry_after=3600)
        if ordered != cursor["frontier"]:
            cursor = {**cursor, "frontier": ordered}
        batch_remaining = MISSION_BATCH_PAGES - (cursor["processed"] - batch_start) if mission else PAGE_SIZE
        page = cursor["frontier"][: min(PAGE_SIZE, req.max_pages - cursor["processed"], ready_count, batch_remaining)]
        prepared: list[Prepared] = []
        for entry in page:
            try:
                p = await _prepare(entry, job, spec, req, deps)
            except BlobQuotaExceeded as exc:
                entry["t"] = entry.get("t", 0) + 1
                entry["quota_bytes"] = exc.requested_bytes
                log.warning(
                    "collector job paused at raw blob quota job_id=%s used_bytes=%d requested_bytes=%d cap_bytes=%d",
                    job.id, exc.used_bytes, exc.requested_bytes, exc.cap_bytes,
                )
                if prepared:
                    # Commit only the fully prepared prefix, leaving the rejected entry and tail in the frontier.
                    await _ml_page(job, prepared)
                    quota_page = page[:len(prepared)]
                    quota_result: dict[str, Any] = {}
                    now = deps.clock()

                    async def commit_prefix(
                        db, _cursor=cursor, _page=quota_page, _prepared=prepared, _now=now, _result=quota_result
                    ):
                        return await _finalize_page(
                            db, job, spec, label, req, _cursor, _page, _prepared, _now, _result
                        )

                    if await ctx.commit_work(commit_prefix, stage=STAGE_PROCESS) == "cancelled":
                        return HandlerResult("failed", {"code": "cancelled"})
                    cursor = quota_result["cursor"]
                elif await ctx.commit_page(cursor, stage=STAGE_PROCESS) == "cancelled":
                    return HandlerResult("failed", {"code": "cancelled"})
                return HandlerResult(
                    "retry_wait",
                    {"code": "storage_cap_reached", **_summary(cursor), "used_bytes": exc.used_bytes,
                     "requested_bytes": exc.requested_bytes, "cap_bytes": exc.cap_bytes},
                    retry_after=3600,
                )
            if p.retry is not None and p.retry[1] != "host_cooldown" and entry["t"] + 1 >= MAX_URL_TRIES:
                p.errors.append(_err("fetch", "retries_exhausted", f"{MAX_URL_TRIES} attempts: {p.retry[1]}"))
                p.status, p.stage, p.reasons, p.retry = "failed", "fetch", ["retries_exhausted", p.retry[1]], None
            prepared.append(p)
            await ctx.heartbeat()
        await _ml_page(job, prepared)
        await ctx.heartbeat()
        now = deps.clock()
        result: dict[str, Any] = {}

        async def work(db, _page=page, _prepared=prepared, _cursor=cursor, _now=now, _result=result):
            return await _finalize_page(db, job, spec, label, req, _cursor, _page, _prepared, _now, _result)

        if await ctx.commit_work(work, stage=STAGE_PROCESS) == "cancelled":
            return HandlerResult("failed", {"code": "cancelled"})
        cursor = result["cursor"]
        waits = [p.retry for p in prepared if p.retry is not None]
        if waits:
            return HandlerResult(
                "retry_wait", {"code": "fetch_deferred", "reason": waits[0][1], **_summary(cursor)},
                retry_after=max(w[0] for w in waits) or None,
            )
        if mission and cursor["frontier"] and cursor["processed"] < req.max_pages and cursor["processed"] - batch_start >= MISSION_BATCH_PAGES:
            return HandlerResult("yielded")

    if cursor["frontier"]:  # page limit reached with URLs left: counted, never silently dropped
        if mission:
            if await ctx.commit_page(cursor, stage="budget_reached") == "cancelled":
                return HandlerResult("failed", {"code": "cancelled"})
            return HandlerResult("partial", {"code": "mission_page_budget", "remaining_urls": len(cursor["frontier"]), **_summary(cursor)})
        left = len(cursor["frontier"])
        cursor = {**cursor, "frontier": [], "counts": {**cursor["counts"], "skipped_limit": cursor["counts"]["skipped_limit"] + left}}
    if await ctx.commit_page(cursor, stage="done") == "cancelled":
        return HandlerResult("failed", {"code": "cancelled"})
    counts = cursor["counts"]
    handled = sum(counts[k] for k in ("accepted", "held", "failed", "not_modified", "discovery"))
    if counts["failed"]:
        return HandlerResult("partial", {"code": "items_failed", **_summary(cursor)})
    if counts["blocked"] and not handled:
        return HandlerResult("policy_blocked", {"code": "all_items_blocked", **_summary(cursor)})
    return HandlerResult("succeeded")


async def _handle_reprocess(job, ctx, spec, label, req, original_id, expected_raw_sha256, deps) -> HandlerResult:
    """Re-run current extraction/scoring over a retained raw response; this path never opens a network connection."""
    async with sessionmaker()() as db:
        original = await db.scalar(select(CollectorItem).where(
            CollectorItem.id == original_id, CollectorItem.tenant_id == job.tenant_id,
            in_library(CollectorItem.scope),
        ))
        if original is None or original.kind != "document" or not original.raw_handle or not original.raw_sha256:
            return HandlerResult("quarantined", {"code": "reprocess_source_unavailable"})
        if original.raw_sha256 != expected_raw_sha256:
            return HandlerResult("quarantined", {"code": "reprocess_source_changed"})
        original_data = {
            "id": str(original.id), "url": original.final_url or original.url, "url_key": original.url_key,
            "raw_handle": original.raw_handle, "raw_sha256": original.raw_sha256, "raw_size": original.raw_size,
            "media_type": original.media_type, "http_status": original.http_status, "fetched_at": original.fetched_at,
            "robots": (original.envelope or {}).get("rights", {}).get("robots", "unknown"),
            "retain_until": (original.envelope or {}).get("rights", {}).get("retain_until"),
        }
    if original_data["retain_until"]:
        try:
            if parse_iso_datetime(original_data["retain_until"]) <= deps.clock():
                return HandlerResult("policy_blocked", {"code": "raw_retention_expired"})
        except CollectorContractError:
            return HandlerResult("quarantined", {"code": "invalid_source_retention"})
    current_retain_until = spec.retain_until(original_data["fetched_at"]) if original_data["fetched_at"] else None
    if current_retain_until and current_retain_until <= deps.clock():
        return HandlerResult("policy_blocked", {"code": "current_source_retention_expired"})
    try:
        body = await asyncio.to_thread(deps.blobs.get, original_data["raw_handle"])
    except (OSError, ValueError) as exc:
        return HandlerResult("quarantined", {"code": "raw_blob_unavailable", "detail": type(exc).__name__})
    raw_sha = hashlib.sha256(body).hexdigest()
    if raw_sha != original_data["raw_sha256"]:
        return HandlerResult("quarantined", {"code": "raw_blob_hash_mismatch"})

    entry = _entry(original_data["url"], 0, parent_item=original_id, parent_url=original_data["url"])
    p = Prepared(entry, uuid.uuid4(), original_data["url"], original_data["url_key"],
                 final_url=original_data["url"], http_status=original_data["http_status"],
                 fetched_at=original_data["fetched_at"], raw_sha256=raw_sha, raw_size=len(body),
                 raw_handle=original_data["raw_handle"], media_type=original_data["media_type"],
                 robots=original_data["robots"])
    p.signals["reprocessed_from"] = str(original_id)
    detected = parse.detect_kind(body, p.media_type)
    if detected.kind not in ("html", "text", "pdf"):
        p.status, p.stage = "held", "fetched"
        p.reasons = [f"unsupported_media:{detected.reason or detected.kind}"]
        p.errors = [_err("extract", detected.reason or "unsupported_media", detected.media_type)]
    else:
        try:
            if detected.kind == "pdf":
                from ttakkari.collector.adapters import extract_pdf
                extracted = await extract_pdf(body)
            else:
                extracted = await asyncio.to_thread(parse.extract_document, body, url=p.url, kind=detected.kind)
            p.analysis = await _analyze(extracted, p.url, None, req, deps)
            p.stage = "scored"
        except ValueError as exc:
            p.status, p.stage = "failed", "extracted"
            p.reasons = [str(exc)]
            p.errors = [_err("extract", str(exc), "no main text could be extracted from preserved raw data")]
            p.signals["decision_hint"] = "error"
        except Exception as exc:  # noqa: BLE001 hostile saved markup remains an item-level failure
            log.warning("reprocess extraction failed for item %s: %s", original_id, type(exc).__name__)
            p.status, p.stage = "failed", "extracted"
            p.reasons = ["parser_error"]
            p.errors = [_err("extract", "parser_error", type(exc).__name__)]
            p.signals["decision_hint"] = "error"

    cursor = {"v": 1, "frontier": [entry], "seen": [p.url_key[:16]], "processed": 0,
              "discovered": 1, "counts": dict.fromkeys(COUNT_KEYS, 0)}
    result: dict[str, Any] = {}

    async def work(db):
        return await _finalize_page(db, job, spec, label, req, cursor, [entry], [p], deps.clock(), result)

    if await ctx.commit_work(work, stage=STAGE_PROCESS) == "cancelled":
        return HandlerResult("cancelled")
    cursor = result["cursor"]
    if await ctx.commit_page(cursor, stage="done") == "cancelled":
        return HandlerResult("cancelled")
    counts = cursor["counts"]
    if counts["failed"]:
        return HandlerResult("partial", {"code": "reprocessed_item_failed", "counts": counts})
    return HandlerResult("succeeded")
