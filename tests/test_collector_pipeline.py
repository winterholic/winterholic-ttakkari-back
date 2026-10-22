"""CP-S2-03/04/06: the stage handler end to end on a disposable PostgreSQL and a local fixture web server.

Covers policy gating (unknown deny), robots, discovery, raw preservation, extraction, normalization, language,
exact/near duplicates, L0 rules, per-job evaluation profiles, the L1 hook, retry/cooldown resume, page atomicity,
cancellation and scope isolation. No LLM and no public network.
"""
from __future__ import annotations

import ast
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from collector_support import (
    FixtureServer,
    article_html,
    loopback_guard,
    prose,
    recorder,
)
from sqlalchemy import func, select, update

from ttakkari.collector import jobs, pipeline, quality, runner, store
from ttakkari.collector.blobs import FileBlobStore
from ttakkari.collector.contracts import (
    AuthContext,
    compute_payload_hash,
    validate_envelope,
)
from ttakkari.collector.fetcher import SafeFetcher
from ttakkari.collector.intake import submit_envelope
from ttakkari.collector.policy import SourcePolicySpec
from ttakkari.collector.polite import PoliteFetcher
from ttakkari.collector_models import (
    CollectorHostState,
    CollectorItem,
    CollectorJob,
    CollectorOutbox,
)
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker

record = recorder()
FIXTURES = Path(__file__).resolve().parents[1] / "docs/design/collector-platform/contracts/fixtures"
PERSONAL = AuthContext(tenant_id="fixture-tenant", scope="personal", principal_id="fixture-owner")
WORK = AuthContext(tenant_id="fixture-tenant", scope="work", principal_id="fixture-owner")
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
LONG_KO = prose("ko", 1, 8)
LONG_EN = prose("en", 1, 8)


class FixedClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Env:
    server: FixtureServer
    deps: pipeline.Deps
    clock: FixedClock
    blobs: FileBlobStore
    fetcher: SafeFetcher


@pytest.fixture
async def env(tmp_path):
    async with FixtureServer() as server:
        clock = FixedClock()
        fetcher = SafeFetcher(loopback_guard("docs.test", "other.test"), user_agent="TtakkariCollector/1.0 (+test)")
        blobs = FileBlobStore(tmp_path / "blobs")
        deps = pipeline.Deps(
            PoliteFetcher(fetcher, store.DbHostState(), product_token="TtakkariCollector", clock=clock, sleep=clock.sleep),
            blobs, clock,
        )
        pipeline.configure(deps)
        runner.register_handler(pipeline.handle_job)
        server.page("/robots.txt", "User-agent: *\nAllow: /\n", "text/plain")
        yield Env(server, deps, clock, blobs, fetcher)
        await fetcher.aclose()
        pipeline.configure(None)
        runner.register_handler(None)


def spec(env: Env, **over) -> SourcePolicySpec:
    d = {
        "allowed_hosts": ["docs.test"], "schemes": ["http"], "ports": [env.server.port],
        "permissions": {"store": "allowed"}, "license_id": "CC-BY-4.0", "policy_url": "https://example.test/terms",
        "checked_at": "2026-10-21T00:00:00+09:00", "basis": "fixture terms section 1", "min_delay_seconds": 0.0,
        "max_pages_per_job": 50, "max_depth": 0,
    }
    d.update(over)
    return SourcePolicySpec.from_dict(d)


async def register(auth: AuthContext, source_key: str, policy: SourcePolicySpec) -> None:
    async with sessionmaker()() as db:
        await store.register_policy(db, auth.tenant_id, auth.scope, source_key, policy)
        await db.commit()


async def submit(env: Env, auth: AuthContext, source_key: str, paths, *, kind: str = "web", request: dict | None = None,
                 raw_urls: list[str] | None = None, request_key: str | None = None, scope_claim: bool = True) -> uuid.UUID:
    envelope = json.loads((FIXTURES / "valid-accept.json").read_text(encoding="utf-8"))
    envelope["envelope_id"] = str(uuid.uuid4())
    seeds = raw_urls if raw_urls is not None else [env.server.url(p) for p in paths]
    envelope["source"].update({"key": source_key, "kind": kind, "url": seeds[0] if seeds else None})
    envelope["access"]["scope"] = auth.scope
    envelope["rights"]["purposes"] = ["store", "train"]  # a client claim the collector must not copy
    envelope["extensions"]["collector:request"] = {"seeds": seeds, **(request or {})}
    async with sessionmaker()() as db:
        result = await submit_envelope(db, auth, request_key or f"req-{uuid.uuid4().hex[:8]}", envelope)
        await db.commit()
    return result.job_id


async def run(job_id: uuid.UUID, n: int = 1) -> CollectorJob:
    await runner.consume_once(limit=n)
    return await job_row(job_id)


async def job_row(job_id: uuid.UUID) -> CollectorJob:
    async with sessionmaker()() as db:
        job = await db.get(CollectorJob, job_id)
        db.expunge(job)
        return job


async def items(job_id: uuid.UUID) -> list[CollectorItem]:
    async with sessionmaker()() as db:
        rows = (await db.scalars(select(CollectorItem).where(CollectorItem.job_id == job_id).order_by(CollectorItem.created_at, CollectorItem.url))).all()
        for r in rows:
            db.expunge(r)
        return list(rows)


def stored_raw_files(root: Path) -> list[Path]:
    """Regular files under a blob root other than the empty quota lock (raw blobs, stage files, lineage manifests)."""
    if not root.exists():
        return []
    return sorted(
        f for f in root.rglob("*")
        if f.is_file() and not (f.parent == root and f.name == ".quota.lock" and f.stat().st_size == 0)
    )


def by_path(rows: list[CollectorItem]) -> dict[str, CollectorItem]:
    return {"/" + r.url.split("/", 3)[3]: r for r in rows}


def assert_accounting(job: CollectorJob) -> None:
    c, cur = job.cursor["counts"], job.cursor
    assert cur["discovered"] == (
        c["accepted"] + c["held"] + c["blocked"] + c["failed"] + c["not_modified"] + c["discovery"]
        + c["skipped_limit"] + len(cur["frontier"])
    ), cur


def paragraphs(*texts: str) -> list[str]:
    return list(texts)


async def outbox_rows(job_id: uuid.UUID) -> list[CollectorOutbox]:
    async with sessionmaker()() as db:
        return list((await db.scalars(select(CollectorOutbox).where(CollectorOutbox.job_id == job_id))).all())


# ---- the main path ---------------------------------------------------------------------------------------------

async def test_end_to_end_exact_and_near_duplicates_raw_preservation_and_rights(env):
    base_ko = LONG_KO + " 마지막 문장은 문서 A의 고유한 끝맺음입니다 하나 둘 셋 넷."
    near_ko = base_ko.replace("마지막 문장은 문서 A의 고유한", "마지막 문장은 문서 D의 약간 다른")
    env.server.page("/a", article_html("서울 날씨", [base_ko]))
    env.server.page("/b", article_html("Committee report", [LONG_EN]))
    env.server.page("/c", article_html("서울 날씨", [base_ko], extra_head='<meta name="copy" content="mirror"/>'))
    env.server.page("/d", article_html("서울 날씨 수정본", [near_ko]))
    await register(PERSONAL, "src-main", spec(env))
    job_id = await submit(env, PERSONAL, "src-main", ["/a", "/b", "/c", "/d"])
    job = await run(job_id)
    rows = by_path(await items(job_id))

    assert job.status == "succeeded" and job.stage == "done", job.last_error
    assert_accounting(job)
    a, b, c, d = rows["/a"], rows["/b"], rows["/c"], rows["/d"]
    assert (a.status, b.status) == ("accepted", "accepted")
    assert (a.language, b.language) == ("ko", "en")
    # exact duplicate of a different URL: kept as its own occurrence, held, linked to the canonical envelope
    assert c.status == "held" and c.exact_of == a.id and "exact_duplicate" in c.reason_codes
    assert c.envelope["dedupe"]["exact_of"] == str(a.envelope_id) and c.raw_sha256 != a.raw_sha256  # different markup, same text
    assert c.envelope_id is not None and c.minhash is None
    # near duplicate: a candidate link, never dropped
    assert d.status == "accepted" and d.cluster_id == str(a.id) and "near_duplicate_candidate" in d.reason_codes
    assert d.envelope["dedupe"]["canonical_candidate"] == str(a.envelope_id) and d.signals["near_candidates"][0]["jaccard"] >= 0.8

    for r in (a, b, c, d):
        validate_envelope(r.envelope)  # contract-valid, not just built
        assert compute_payload_hash(r.envelope) == r.payload_hash
        assert r.envelope["rights"]["purposes"] == ["store"], "rights come from the source policy, not the request"
        assert r.envelope["rights"]["permission"] == "allowed" and r.envelope["rights"]["robots"] == "allowed"
        assert r.envelope["rights"]["policy_version"].startswith("src-main@v1+")
        assert r.envelope["access"]["scope"] == "personal" and r.envelope["decision"]["policy_version"] == r.envelope["rights"]["policy_version"]
        assert r.envelope["raw"]["blob_handle"] == r.raw_handle
        raw = env.blobs.get(r.raw_handle)
        assert hashlib.sha256(raw).hexdigest() == r.raw_sha256 == r.envelope["raw"]["sha256"] and len(raw) == r.raw_size
        assert r.envelope["normalized"]["sha256"] == r.text_sha256 and r.normalizer_version == "norm-1"
        assert r.envelope["quality"]["education"]["status"] == "unmeasured" and r.envelope["quality"]["education"]["value"] is None
        assert r.envelope["fact_check_status"] == "unknown"
        assert "collector:request" not in r.envelope["extensions"]
    # delivery: accepted documents only, each with the hash of the envelope that was stored
    outbox = await outbox_rows(job_id)
    exported = {o.envelope_id: o for o in outbox if o.sink == "export"}
    assert set(exported) == {a.envelope_id, b.envelope_id, d.envelope_id}
    assert all(exported[r.envelope_id].payload_hash == r.payload_hash for r in (a, b, d))
    assert len([o for o in outbox if o.sink == "default"]) == 1
    for name, r in (("a", a), ("b", b), ("c", c), ("d", d)):
        lin = r.envelope["extensions"]["collector:lineage"]
        record("lineage", {
            "item": name, "url_path": r.url.split("/", 3)[3], "status": r.status, "decision": r.decision_state,
            "reason_codes": r.reason_codes, "raw_sha256": r.raw_sha256, "raw_size": r.raw_size,
            "extracted_sha256": lin["extracted_sha256"], "text_sha256": r.text_sha256, "normalizer_steps": lin["normalizer_steps"],
            "parser_version": r.parser_version, "normalizer_version": r.normalizer_version, "language": r.language,
            "exact_of_item": "a" if r.exact_of == a.id else None, "cluster_root_is_a": r.cluster_id == str(a.id),
            "dedupe": r.envelope["dedupe"]["algorithm_version"], "quality_axes_measured": sum(v["status"] == "measured" for v in r.envelope["quality"].values()),
            "payload_hash": r.payload_hash,
        })
    record("pipeline", {"case": "exact+near duplicates, raw preserved", "expected": "a,b,d accepted; c held exact_duplicate", "observed": "ok", "passed": True})


async def test_text_is_only_nfc_normalized_and_the_chain_is_hash_linked(env):
    text = "Café 일기 " + "가나다라마바사 " * 40 + "\u200b끝"
    env.server.page("/n", article_html("Note", [text]))
    await register(PERSONAL, "src-n", spec(env))
    job_id = await submit(env, PERSONAL, "src-n", ["/n"])
    job = await run(job_id)
    (row,) = await items(job_id)
    assert job.status == "succeeded"
    norm = row.envelope["normalized"]["text"]
    assert "Café" in norm and "\u200b" not in norm and "́" not in norm  # composed, zero width removed
    lineage = row.envelope["extensions"]["collector:lineage"]
    ops = {op for op, _ in lineage["normalizer_steps"]}
    assert "nfc" in ops
    assert lineage["extracted_sha256"] != row.text_sha256  # the pre-normalization text is hash-linked
    assert row.envelope["lineage"][2] == f"raw:sha256:{row.raw_sha256}"


# ---- policy gates ----------------------------------------------------------------------------------------------

async def test_missing_policy_and_unverified_store_purpose_are_denied_without_network(env):
    env.server.page("/a", article_html("A", [LONG_EN]))
    job_id = await submit(env, PERSONAL, "src-none", ["/a"])
    job = await run(job_id)
    assert job.status == "policy_blocked" and job.last_error["code"] == "source_policy_missing"
    await register(PERSONAL, "src-unknown", spec(env, permissions={"store": "unknown", "export": "allowed"}))
    job2 = await run(await submit(env, PERSONAL, "src-unknown", ["/a"]))
    assert job2.status == "policy_blocked" and job2.last_error["code"] == "store_not_permitted"
    await register(PERSONAL, "src-denied", spec(env, permissions={"store": "denied"}))
    job3 = await run(await submit(env, PERSONAL, "src-denied", ["/a"]))
    assert job3.status == "policy_blocked"
    assert env.server.hits == []
    assert await items(job_id) == []


async def test_policy_versions_are_append_only_and_jobs_pin_the_active_version(env):
    env.server.page("/a", article_html("A", [LONG_EN]))
    await register(PERSONAL, "src-v", spec(env, basis="terms v1"))
    await register(PERSONAL, "src-v", spec(env, basis="terms v1"))  # identical: no new version
    await register(PERSONAL, "src-v", spec(env, basis="terms v2 (updated)"))
    async with sessionmaker()() as db:
        spec_, version, _ = await store.active_policy(db, "fixture-tenant", "personal", "src-v")
        count = await db.scalar(select(func.count()).select_from(store.CollectorSourcePolicy))
    assert version == 2 and count == 2 and spec_.basis == "terms v2 (updated)"
    job_id = await submit(env, PERSONAL, "src-v", ["/a"])
    await run(job_id)
    (row,) = await items(job_id)
    assert row.envelope["rights"]["policy_version"].startswith("src-v@v2+") and row.envelope["rights"]["basis"] == "terms v2 (updated)"


async def test_robots_disallow_blocks_every_item_and_the_job_reports_policy_blocked(env):
    env.server.page("/robots.txt", "User-agent: *\nDisallow: /\n", "text/plain")
    env.server.page("/a", article_html("A", [LONG_EN]))
    await register(PERSONAL, "src-r", spec(env))
    job_id = await submit(env, PERSONAL, "src-r", ["/a", "/b"])
    job = await run(job_id)
    rows = await items(job_id)
    assert job.status == "policy_blocked" and job.last_error["code"] == "all_items_blocked"
    assert {r.status for r in rows} == {"blocked"} and all("robots_disallowed" in r.reason_codes for r in rows)
    assert env.server.count("/a") == 0 and env.server.count("/b") == 0 and rows[0].envelope is None
    assert_accounting(job)


async def test_seed_outside_policy_is_a_blocked_item_not_a_silent_skip(env):
    env.server.page("/ok", article_html("Ok", [LONG_EN]))
    await register(PERSONAL, "src-o", spec(env, excluded_path_prefixes=["/admin"]))
    raw = [env.server.url("/ok"), env.server.url("/admin/x"), "file:///etc/passwd", "http://user:pw@docs.test/", env.server.url("/ok", "evil.test")]
    job_id = await submit(env, PERSONAL, "src-o", [], raw_urls=raw)
    job = await run(job_id)
    rows = await items(job_id)
    reasons = sorted(r.reason_codes[0] for r in rows if r.status == "blocked")
    assert reasons == ["host_not_allowed", "path_excluded_by_policy", "scheme_not_allowed", "userinfo_not_allowed"]
    assert job.cursor["counts"]["accepted"] == 1 and job.status == "succeeded"
    assert_accounting(job)


async def test_request_cannot_widen_the_policy_and_invalid_requests_are_quarantined(env):
    await register(PERSONAL, "src-w", spec(env, max_pages_per_job=3, max_depth=0, allowed_profiles=["general"]))
    cases = [
        {"max_pages": 4}, {"max_depth": 1}, {"profile": "news"}, {"profile_params": {"nope": 1}},
        {"unknown_field": 1}, {"l1": {"mode": "gate", "scorer": "x", "hold_below": {"bogus": 1}}},
        {"l1": {"mode": "off", "hold_below": {"education": 0.5}}},
    ]
    for request in cases:
        job = await run(await submit(env, PERSONAL, "src-w", ["/a"], request=request))
        assert job.status == "quarantined" and job.last_error["code"] == "invalid_request", request
    unsupported = await run(await submit(env, PERSONAL, "src-w", ["/a"], kind="file"))
    assert unsupported.status == "quarantined" and unsupported.last_error["code"] == "unsupported_source_kind"
    assert env.server.hits == []


# ---- evaluation profiles (selected per job) --------------------------------------------------------------------

async def test_news_profile_judges_freshness_and_general_profile_does_not(env):
    env.server.page("/fresh", article_html("Fresh", [LONG_EN], published="2026-10-20T09:00:00+09:00"))
    env.server.page("/stale", article_html("Stale", [LONG_EN + " old report details."], published="2026-08-01"))
    env.server.page("/undated", article_html("Undated", [LONG_EN + " undated report details."]))
    for key in ("src-news", "src-general"):
        await register(PERSONAL, key, spec(env))
    news = await run(await submit(env, PERSONAL, "src-news", ["/fresh", "/stale", "/undated"], request={"profile": "news"}))
    got = by_path(await items(news.id))
    assert got["/fresh"].status == "accepted" and got["/fresh"].envelope["source"]["published_at"] is not None
    assert got["/stale"].status == "held" and got["/stale"].reason_codes == ["news_stale"]
    assert got["/undated"].status == "held" and got["/undated"].reason_codes == ["news_published_at_unknown"]
    prof = got["/fresh"].envelope["extensions"]["collector:profile"]
    assert prof["name"] == "news" and prof["signals"]["age_days"] is not None and prof["params"]["max_age_days"] == 14
    # same pages, different purpose: nothing is held for freshness
    general = await run(await submit(env, PERSONAL, "src-general", ["/fresh", "/stale", "/undated"], request={"profile": "general"}))
    assert {r.status for r in await items(general.id)} == {"accepted"}
    # a per-job override widens the window within the profile's declared bounds
    env.server.page("/stale2", article_html("Stale2", [LONG_EN + " other old report."], published="2026-08-01"))
    wide = await run(await submit(env, PERSONAL, "src-news", ["/stale2"], request={"profile": "news", "profile_params": {"max_age_days": 365}}))
    assert (await items(wide.id))[0].status == "accepted"
    bad = await run(await submit(env, PERSONAL, "src-news", ["/stale2"], request={"profile": "news", "profile_params": {"max_age_days": 0}}))
    assert bad.status == "quarantined"
    record("profile", {"case": "news vs general on identical pages", "expected": "news holds stale/undated, general accepts", "observed": "ok", "passed": True})


async def test_tech_docs_profile_records_versions_and_holds_unversioned_pages(env):
    body = [prose("en", 40, 6)]
    env.server.page("/docs/3.13/library/os", article_html("os module", body + ["os 3.13 specifics."]))
    env.server.page("/docs/guide", article_html("Guide", body + ["guide specifics without any version."]))
    env.server.page("/docs/latest/api", article_html("API", body + ["latest api specifics."]))
    env.server.page("/docs/text", article_html("Text", ["Version: 2.4.1 of the reference manual covers the following topics.", prose("en", 41, 6)]))
    await register(PERSONAL, "src-docs", spec(env))
    job = await run(await submit(env, PERSONAL, "src-docs", ["/docs/3.13/library/os", "/docs/guide", "/docs/latest/api", "/docs/text"], request={"profile": "tech_docs"}))
    got = by_path(await items(job.id))
    sig = {p: r.envelope["extensions"]["collector:profile"]["signals"] for p, r in got.items()}
    assert got["/docs/3.13/library/os"].status == "accepted" and sig["/docs/3.13/library/os"]["version_hint"] == "3.13" and sig["/docs/3.13/library/os"]["version_source"] == "url"
    assert got["/docs/guide"].status == "held" and got["/docs/guide"].reason_codes == ["tech_docs_version_unknown"]
    assert got["/docs/latest/api"].status == "accepted" and sig["/docs/latest/api"]["latest_alias_url"] is True
    assert "tech_docs_latest_alias" in got["/docs/latest/api"].reason_codes
    assert got["/docs/text"].status == "accepted" and sig["/docs/text"]["version_hint"] == "2.4.1" and sig["/docs/text"]["version_source"] == "text"
    env.server.page("/docs/guide2", article_html("Guide 2", [prose("en", 42, 6)]))
    relaxed = await run(await submit(env, PERSONAL, "src-docs", ["/docs/guide2"], request={"profile": "tech_docs", "profile_params": {"require_version": False}}))
    assert (await items(relaxed.id))[0].status == "accepted"


async def test_l0_rules_hold_but_never_delete(env):
    env.server.page("/short", article_html("Short", ["Too short to judge."]))
    env.server.page("/spam", article_html("Spam", ["buy now cheap pills " * 60]))
    env.server.page("/links", article_html("Links", [" ".join(f"http://x.test/{i}" for i in range(80))]))
    await register(PERSONAL, "src-l0", spec(env))
    job = await run(await submit(env, PERSONAL, "src-l0", ["/short", "/spam", "/links"]))
    got = by_path(await items(job.id))
    assert "l0_too_short" in got["/short"].reason_codes and "l0_repetitive" in got["/spam"].reason_codes
    assert "l0_link_heavy" in got["/links"].reason_codes
    for r in got.values():
        assert r.status == "held" and r.envelope is not None and r.raw_handle is not None  # recoverable, not dropped
        assert env.blobs.get(r.raw_handle)
    assert not [o for o in await outbox_rows(job.id) if o.sink == "export"]  # held documents are not delivered


# ---- failure modes are explicit ---------------------------------------------------------------------------------

async def test_unsupported_empty_missing_and_forbidden_responses_are_recorded_not_skipped(env):
    env.server.page("/pdf", b"%PDF-1.7 fake pdf bytes " * 20, "application/pdf")
    env.server.page("/empty", b"", "text/html")
    env.server.page("/scripts", "<html><body><script>var a=1;</script></body></html>")

    async def forbidden(request):
        return web.Response(status=403, text="captcha")

    env.server.routes["/forbidden"] = forbidden
    await register(PERSONAL, "src-f", spec(env))
    job = await run(await submit(env, PERSONAL, "src-f", ["/pdf", "/empty", "/missing", "/forbidden", "/scripts"]))
    got = by_path(await items(job.id))
    assert got["/pdf"].status == "held" and got["/pdf"].reason_codes == ["unsupported_media:pdf_not_supported"] and got["/pdf"].envelope["decision"]["state"] == "hold"
    assert env.blobs.get(got["/pdf"].raw_handle).startswith(b"%PDF-")  # preserved even though it is not parsed
    assert got["/empty"].status == "failed" and got["/empty"].errors[0]["code"] == "empty_response" and got["/empty"].envelope is None
    assert got["/missing"].status == "failed" and got["/missing"].errors[0]["code"] == "not_found"
    assert got["/forbidden"].status == "blocked" and got["/forbidden"].reason_codes == ["access_denied"]
    assert got["/scripts"].status == "failed" and got["/scripts"].envelope["decision"]["state"] == "error"
    assert got["/scripts"].envelope["errors"][0]["code"] == "empty_extraction"
    assert job.status == "partial" and job.last_error["code"] == "items_failed"
    assert_accounting(job)


async def test_html_served_as_text_plain_and_binary_as_html_are_held_as_mismatches(env):
    env.server.page("/lie", "<html><body><p>" + LONG_EN + "</p></body></html>", "text/plain")
    env.server.page("/png", b"\x89PNG\r\n\x1a\n" + b"\0" * 400, "text/html")
    await register(PERSONAL, "src-m", spec(env))
    job = await run(await submit(env, PERSONAL, "src-m", ["/lie", "/png"]))
    got = by_path(await items(job.id))
    assert got["/lie"].reason_codes == ["unsupported_media:media_type_mismatch"]
    assert got["/png"].reason_codes == ["unsupported_media:binary_content"]
    assert job.status == "succeeded"


async def test_store_raw_false_keeps_hash_only_and_says_so(env):
    env.server.page("/a", article_html("A", [LONG_EN]))
    await register(PERSONAL, "src-nr", spec(env, store_raw=False))
    job = await run(await submit(env, PERSONAL, "src-nr", ["/a"]))
    (row,) = await items(job.id)
    assert row.raw_handle is None and row.envelope["raw"]["blob_handle"] == f"unpreserved:sha256:{row.raw_sha256}"
    assert row.envelope["extensions"]["collector:lineage"]["raw_preserved"] is False
    # usage_bytes() takes the quota lock, so an empty root legitimately holds a 0-byte `.quota.lock`. What must never
    # exist is raw content: no shard directory, no blob/stage file, no lineage manifest, and no byte of the page.
    assert stored_raw_files(env.blobs.root) == []
    assert not any((env.blobs.root / name).exists() for name in (".lineage", row.raw_sha256[:2]))
    assert all(b"<html" not in f.read_bytes().lower() for f in env.blobs.root.rglob("*") if f.is_file())


async def test_store_raw_true_leaves_the_blob_the_scan_looks_for(env):
    # Positive control for the store_raw=False scan above: the same helper must see a real preserved blob.
    page = article_html("A", [LONG_EN])
    env.server.page("/a", page)
    await register(PERSONAL, "src-yr", spec(env, store_raw=True))
    job = await run(await submit(env, PERSONAL, "src-yr", ["/a"]))
    (row,) = await items(job.id)
    files = stored_raw_files(env.blobs.root)
    (blob,) = [f for f in files if f.name == row.raw_sha256 and ".lineage" not in f.parts]
    assert blob.read_bytes() == page.encode() and len(files) > 1  # blob plus its lineage manifest


# ---- discovery -------------------------------------------------------------------------------------------------

async def test_feed_sitemap_and_link_discovery_respect_policy_depth_and_page_limit(env):
    for i in range(1, 6):
        env.server.page(f"/p{i}", article_html(f"Page {i}", [LONG_EN + f" unique marker number {i} for page {i}."]))
    env.server.page("/feed.xml", f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
        <item><link>{env.server.url('/p1')}</link></item><item><link>{env.server.url('/p2')}</link></item>
        <item><link>{env.server.url('/p1')}</link></item><item><link>{env.server.url('/p1', 'other.test')}</link></item>
        <item><link>{env.server.url('/admin/secret')}</link></item></channel></rss>""", "application/rss+xml")
    env.server.page("/sitemap.xml", f"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>{env.server.url('/sitemap2.xml')}</loc></sitemap></sitemapindex>""", "application/xml")
    env.server.page("/sitemap2.xml", f"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>{env.server.url('/p3')}</loc></url><url><loc>{env.server.url('/p4')}</loc></url></urlset>""", "application/xml")
    env.server.page("/hub", "<html><body><p>" + LONG_EN + " hub</p><a href='/p5'>five</a><a href='/doc.pdf'>pdf</a><a href='#frag'>f</a></body></html>")
    await register(PERSONAL, "src-d", spec(env, excluded_path_prefixes=["/admin"], max_depth=1))

    feed = await run(await submit(env, PERSONAL, "src-d", ["/feed.xml"]))
    rows = by_path(await items(feed.id))
    assert rows["/feed.xml"].kind == "discovery" and rows["/feed.xml"].status == "discovered"
    assert {p for p in rows if p.startswith("/p")} == {"/p1", "/p2"}  # duplicate link folded, off-host and excluded path not fetched
    assert feed.cursor["counts"]["skipped_policy"] == 2 and feed.cursor["counts"]["discovery"] == 1
    assert rows["/p1"].discovered_from == rows["/feed.xml"].id and rows["/p1"].envelope["lineage"][3].startswith("discovered_from:")
    assert_accounting(feed)

    sitemap = await run(await submit(env, PERSONAL, "src-d", ["/sitemap.xml"]))
    assert {p for p in by_path(await items(sitemap.id)) if p.startswith("/p")} == {"/p3", "/p4"}
    assert_accounting(sitemap)

    hub = await run(await submit(env, PERSONAL, "src-d", ["/hub"]))  # depth 1 follows the page link, not the pdf/fragment
    assert {p for p in by_path(await items(hub.id)) if p.startswith("/p")} == {"/p5"}
    shallow = await run(await submit(env, PERSONAL, "src-d", ["/hub"], request={"max_depth": 0}))
    assert not [p for p in by_path(await items(shallow.id)) if p.startswith("/p")]

    env.server.routes["/feed2.xml"] = env.server.routes["/feed.xml"]  # same entries, a URL not fetched before
    capped = await run(await submit(env, PERSONAL, "src-d", ["/feed2.xml"], request={"max_pages": 2}))
    assert capped.cursor["counts"]["skipped_limit"] >= 1 and capped.cursor["processed"] == 2 and capped.cursor["frontier"] == []
    assert_accounting(capped)


async def test_malformed_feed_is_held_and_the_job_continues(env):
    env.server.page("/feed.xml", "<?xml version='1.0'?><rss><channel><item><link>http://x", "application/rss+xml")
    env.server.page("/a", article_html("A", [LONG_EN]))
    await register(PERSONAL, "src-bf", spec(env))
    job = await run(await submit(env, PERSONAL, "src-bf", ["/feed.xml", "/a"]))
    got = by_path(await items(job.id))
    assert got["/feed.xml"].status == "held" and got["/a"].status == "accepted" and job.status == "succeeded"


# ---- cache, revisions, scope -------------------------------------------------------------------------------------

async def test_unchanged_content_is_not_a_new_revision_and_a_to_b_to_a_is(env):
    page = {"v": "A"}

    async def doc(request):
        body = article_html("Doc", [LONG_EN + f" revision {page['v']} marker text."])
        if request.headers.get("If-None-Match") == f'"{page["v"]}"':
            return web.Response(status=304, headers={"ETag": f'"{page["v"]}"'})
        return web.Response(text=body, content_type="text/html", headers={"ETag": f'"{page["v"]}"'})

    env.server.routes["/doc"] = doc
    await register(PERSONAL, "src-rev", spec(env))
    j1 = await run(await submit(env, PERSONAL, "src-rev", ["/doc"]))
    first = (await items(j1.id))[0]
    assert first.status == "accepted"

    j2 = await run(await submit(env, PERSONAL, "src-rev", ["/doc"]))  # ETag matches: 304
    second = (await items(j2.id))[0]
    assert second.status == "not_modified" and second.reason_codes == ["not_modified_etag"] and second.signals["not_modified_of"] == str(first.id)
    assert second.envelope is None and not [o for o in await outbox_rows(j2.id) if o.sink == "export"]

    page["v"] = "B"
    j3 = await run(await submit(env, PERSONAL, "src-rev", ["/doc"]))
    third = (await items(j3.id))[0]
    assert third.status == "accepted" and third.envelope["lineage"][-1] == f"revision_of:{first.id}"  # new revision, earlier review untouched

    page["v"] = "A"
    j4 = await run(await submit(env, PERSONAL, "src-rev", ["/doc"]))
    fourth = (await items(j4.id))[0]
    assert fourth.status == "accepted" and fourth.exact_of is None  # back to A is a new revision, not a duplicate of its own history
    assert fourth.envelope["lineage"][-1] == f"revision_of:{third.id}"
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(CollectorItem).where(CollectorItem.status == "accepted")) == 3


async def test_same_bytes_without_validators_are_detected_by_content_hash(env):
    env.server.page("/same", article_html("Same", [LONG_EN + " same bytes forever."]))
    await register(PERSONAL, "src-h", spec(env))
    first = (await items((await run(await submit(env, PERSONAL, "src-h", ["/same"]))).id))[0]
    j2 = await run(await submit(env, PERSONAL, "src-h", ["/same"]))
    second = (await items(j2.id))[0]
    assert second.status == "not_modified" and second.reason_codes == ["not_modified_content_hash"]
    assert second.signals["not_modified_of"] == str(first.id) and env.server.count("/same") == 2


async def test_dedupe_and_cache_never_cross_scope(env):
    env.server.page("/a", article_html("A", [LONG_KO]))
    await register(PERSONAL, "src-s", spec(env))
    await register(WORK, "src-s", spec(env))
    env.server.page("/a-mirror", article_html("A", [LONG_KO], extra_head='<meta name="copy" content="mirror"/>'))
    personal = await run(await submit(env, PERSONAL, "src-s", ["/a"]))
    work = await run(await submit(env, WORK, "src-s", ["/a"]))
    p_item, w_item = (await items(personal.id))[0], (await items(work.id))[0]
    assert p_item.status == "accepted" and w_item.status == "accepted"  # same text in another scope is not a duplicate
    assert w_item.exact_of is None and w_item.scope == "work" and w_item.envelope["access"]["scope"] == "work"
    assert env.server.count("/a") == 2  # the work scope did not reuse the personal fetch cache either
    # a different URL with the same text is an exact duplicate only inside the scope that already holds the text
    mirror_work = (await items((await run(await submit(env, WORK, "src-s", ["/a-mirror"]))).id))[0]
    assert mirror_work.status == "held" and mirror_work.exact_of == w_item.id



# ---- retry, cooldown, resume, atomicity, cancel ------------------------------------------------------------------

async def release_job_and_host(job_id: uuid.UUID) -> None:
    async with sessionmaker()() as db:
        await db.execute(update(CollectorJob).where(CollectorJob.id == job_id).values(not_before=None))
        await db.execute(update(CollectorHostState).values(cooldown_until=None))
        await db.commit()


async def test_transient_failure_defers_with_durable_cooldown_and_resumes_without_redoing_work(env):
    env.server.page("/ok", article_html("Ok", [LONG_EN + " ok page marker."]))
    state = {"flaky": 0}

    async def flaky(request):
        state["flaky"] += 1
        if state["flaky"] == 1:
            return web.Response(status=503, headers={"Retry-After": "120"})
        return web.Response(text=article_html("Flaky", [LONG_EN + " flaky page marker."]), content_type="text/html")

    env.server.routes["/flaky"] = flaky
    await register(PERSONAL, "src-t", spec(env))
    job_id = await submit(env, PERSONAL, "src-t", ["/ok", "/flaky"])
    first = await run(job_id)
    assert first.status == "retry_wait" and first.not_before is not None and first.last_error["code"] == "fetch_deferred"
    assert first.cursor["processed"] == 1 and [e["u"].endswith("/flaky") and e["t"] for e in first.cursor["frontier"]] == [1]
    assert_accounting(first)
    async with sessionmaker()() as db:
        host = (await db.scalars(select(CollectorHostState))).first()
    assert host.cooldown_until >= NOW + timedelta(seconds=120) and host.cooldown_reason == "server_error"

    again = await run(job_id)  # not_before is in the future: nothing is claimed, nothing is fetched
    assert again.status == "retry_wait" and env.server.count("/flaky") == 1

    await release_job_and_host(job_id)
    done = await run(job_id)
    assert done.status == "succeeded" and done.attempts == 2
    assert env.server.count("/ok") == 1, "the committed page was not redone"
    assert {r.status for r in await items(job_id)} == {"accepted"} and len(await items(job_id)) == 2
    assert_accounting(done)
    record("cooldown", {"case": "503 Retry-After then resume", "expected": "page 1 kept, flaky retried after release", "observed": "ok", "passed": True})


async def test_url_is_failed_after_three_attempts_not_retried_forever(env):
    async def always(request):
        return web.Response(status=500)

    env.server.routes["/down"] = always
    await register(PERSONAL, "src-x", spec(env))
    job_id = await submit(env, PERSONAL, "src-x", ["/down"])
    for _ in range(3):
        job = await run(job_id)
        if job.status != "retry_wait":
            break
        await release_job_and_host(job_id)
    job = await job_row(job_id)
    (row,) = await items(job_id)
    assert job.status == "partial" and row.status == "failed" and "retries_exhausted" in row.reason_codes
    assert env.server.count("/down") == 3 and job.attempts == 3


async def test_a_page_commits_atomically_and_a_crash_resumes_at_the_failed_page(env, monkeypatch):
    monkeypatch.setattr(pipeline, "PAGE_SIZE", 2)
    for i in range(4):
        env.server.page(f"/p{i}", article_html(f"P{i}", [LONG_EN + f" distinct page {i} marker words."]))
    await register(PERSONAL, "src-c", spec(env))
    job_id = await submit(env, PERSONAL, "src-c", [f"/p{i}" for i in range(4)])

    real = store.put_fetch_cache
    calls = {"n": 0}

    async def boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:  # first item of the second page
            raise RuntimeError("simulated crash inside the page transaction")
        return await real(*a, **kw)

    monkeypatch.setattr(store, "put_fetch_cache", boom)
    crashed = await run(job_id)
    assert crashed.status == "retry_wait" and crashed.last_error["code"] == "handler_error"
    assert len(await items(job_id)) == 2 and crashed.cursor["processed"] == 2 and len(crashed.cursor["frontier"]) == 2
    async with sessionmaker()() as db:  # nothing of the failed page leaked into any table
        assert await db.scalar(select(func.count()).select_from(CollectorOutbox).where(CollectorOutbox.job_id == job_id, CollectorOutbox.sink == "export")) == 2

    monkeypatch.setattr(store, "put_fetch_cache", real)
    await release_job_and_host(job_id)
    done = await run(job_id)
    rows = await items(job_id)
    assert done.status == "succeeded" and len(rows) == 4 and len({r.url_key for r in rows}) == 4
    assert env.server.count("/p0") == 1 and env.server.count("/p1") == 1  # page 1 was never refetched
    assert_accounting(done)


async def test_cancel_is_honoured_at_the_page_boundary_and_committed_pages_stay(env, monkeypatch):
    monkeypatch.setattr(pipeline, "PAGE_SIZE", 1)
    for i in range(4):
        env.server.page(f"/p{i}", article_html(f"P{i}", [LONG_EN + f" distinct page {i} marker words."]))
    await register(PERSONAL, "src-k", spec(env))
    job_id = await submit(env, PERSONAL, "src-k", [f"/p{i}" for i in range(4)])
    real_fetch = env.deps.polite.fetch
    seen = {"n": 0}

    async def fetch_then_cancel(*a, **kw):
        seen["n"] += 1
        if seen["n"] == 2:
            async with sessionmaker()() as db:
                assert await jobs.cancel(db, "fixture-tenant", "personal", job_id) == "cancelling"
                await db.commit()
        return await real_fetch(*a, **kw)

    monkeypatch.setattr(env.deps.polite, "fetch", fetch_then_cancel)
    job = await run(job_id)
    assert job.status == "cancelled" and job.last_error["code"] == "cancelled"
    assert len(await items(job_id)) == 2 and env.server.count("/p2") == 0  # stopped at the boundary, kept what was committed


async def test_storage_cap_pauses_the_job_and_does_not_refetch_while_full(env, monkeypatch):
    # Contract (stage-handler.md, cap-full replay): when the root is full the job stops in retry_wait/storage_cap_reached
    # with the rejected URL and tail kept in the frontier. A fresh URL may be fetched once, because its bytes could dedupe
    # to a durable orphan; after a quota refusal it is not fetched again until `used + quota_bytes <= cap`.
    page_a, page_b = article_html("A", [LONG_EN]), article_html("B", [LONG_EN + " second page marker."])
    env.server.page("/a", page_a)
    env.server.page("/b", page_b)
    size_a, size_b = len(page_a.encode()), len(page_b.encode())
    monkeypatch.setattr(pipeline, "PAGE_SIZE", 1)
    monkeypatch.setattr(get_settings(), "collector_blob_cap_bytes", size_a)  # /a fits exactly, nothing is left for /b
    await register(PERSONAL, "src-cap", spec(env))
    job_id = await submit(env, PERSONAL, "src-cap", ["/a", "/b"])

    job = await run(job_id)
    assert job.status == "retry_wait" and job.last_error["code"] == "storage_cap_reached"
    assert job.last_error["requested_bytes"] == size_b and job.last_error["cap_bytes"] == size_a
    assert job.cursor["processed"] == 1 and len(await items(job_id)) == 1
    assert [(e["t"], e["quota_bytes"]) for e in job.cursor["frontier"]] == [(1, size_b)]  # kept, not dropped
    assert env.server.count("/a") == 1 and env.server.count("/b") == 1
    assert env.blobs.usage_bytes() == size_a and [f.name for f in stored_raw_files(env.blobs.root) if f.name.startswith(".stage")] == []
    assert_accounting(job)

    await release_job_and_host(job_id)
    still_full = await run(job_id)  # no room appeared: the job stays paused and /b is not fetched a second time
    assert still_full.status == "retry_wait" and still_full.last_error["code"] == "storage_cap_reached"
    assert env.server.count("/b") == 1 and len(await items(job_id)) == 1

    monkeypatch.setattr(get_settings(), "collector_blob_cap_bytes", size_a + size_b)
    await release_job_and_host(job_id)
    resumed = await run(job_id)  # room appeared: the kept URL is retried and the job finishes
    assert resumed.status == "succeeded" and len(await items(job_id)) == 2 and env.server.count("/b") == 2
    assert resumed.cursor["frontier"] == [] and env.blobs.usage_bytes() == size_a + size_b


# ---- L1 port ---------------------------------------------------------------------------------------------------

class FakeScorer:
    info = quality.ScorerInfo("fake", "fake-edu-model", "rev-1", "pre-1")

    def __init__(self, value: float) -> None:
        self.value = value

    async def score(self, text: str, language: str):
        return {"education": quality.AxisScore(self.value, "raw_logit"), "spam": quality.AxisScore(0.1, "raw_logit")}


async def test_l1_hook_shadow_gate_and_unavailable_scorer(env):
    env.server.page("/a", article_html("A", [LONG_EN]))
    await register(PERSONAL, "src-l1", spec(env))
    quality.register_scorer(FakeScorer(0.2))
    try:
        gate = await run(await submit(env, PERSONAL, "src-l1", ["/a"], request={"l1": {"mode": "gate", "scorer": "fake", "hold_below": {"education": 0.5}}}))
        (g,) = await items(gate.id)
        assert g.status == "held" and g.reason_codes == ["l1_education_below_threshold"]
        axis = g.envelope["quality"]["education"]
        assert axis["status"] == "measured" and axis["scale"] == "raw_logit" and axis["model_id"] == "fake-edu-model" and axis["model_revision"] == "rev-1"
        assert g.envelope["quality"]["factuality"]["status"] == "unmeasured"

        shadow = await run(await submit(env, PERSONAL, "src-l1", ["/a"], request={"l1": {"mode": "shadow", "scorer": "fake"}}))
        (s,) = await items(shadow.id)
        assert s.status in ("accepted", "not_modified")  # unchanged content may short-circuit; use a fresh page below
    finally:
        quality.unregister_scorer("fake")
    env.server.page("/b", article_html("B", [LONG_EN + " shadow page marker words."]))
    quality.register_scorer(FakeScorer(0.2))
    try:
        shadow2 = await run(await submit(env, PERSONAL, "src-l1", ["/b"], request={"l1": {"mode": "shadow", "scorer": "fake", "hold_below": {}}}))
        (s2,) = await items(shadow2.id)
        assert s2.status == "accepted" and s2.envelope["quality"]["education"]["status"] == "measured"  # recorded, never gating
    finally:
        quality.unregister_scorer("fake")
    env.server.page("/c", article_html("C", [LONG_EN + " gate without scorer marker."]))
    missing = await run(await submit(env, PERSONAL, "src-l1", ["/c"], request={"l1": {"mode": "gate", "scorer": "absent"}}))
    (m,) = await items(missing.id)
    assert m.status == "held" and "l1_unavailable" in m.reason_codes
    assert m.envelope["quality"]["education"]["status"] == "unmeasured"  # a missing model is never reported as a score of 0
    off = await run(await submit(env, PERSONAL, "src-l1", ["/c"], request={"l1": {"mode": "off"}}))
    assert off.status == "succeeded"


# ---- structural guarantees ---------------------------------------------------------------------------------------

def test_collector_package_has_no_llm_or_knowledge_dependency():
    root = Path(__file__).resolve().parents[1] / "src/ttakkari/collector"
    forbidden = ("ttakkari.knowledge", "ttakkari.kb", "ttakkari.runs", "ttakkari.orchestrator", "anthropic", "openai", "ttakkari.context")
    seen: set[str] = set()
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                seen.add(n)
                if path.name == "runner.py":  # S2-02 runner imports models for generation liveness only
                    continue
                assert not n.startswith(forbidden), f"{path.name} imports {n}"
    assert "ttakkari.collector_models" in seen


async def test_host_state_is_public_data_only_and_policy_rows_are_tenant_scoped(env):
    await register(PERSONAL, "src-iso", spec(env))
    async with sessionmaker()() as db:
        assert await store.active_policy(db, "fixture-tenant", "work", "src-iso") is None
        assert await store.active_policy(db, "other-tenant", "personal", "src-iso") is None
        cols = {c.name for c in CollectorHostState.__table__.columns}
    assert not cols & {"tenant_id", "scope", "job_id"}


async def test_work_is_committed_with_the_fence_so_a_stale_attempt_leaves_nothing(env):
    env.server.page("/a", article_html("A", [LONG_EN]))
    await register(PERSONAL, "src-fence", spec(env))
    job_id = await submit(env, PERSONAL, "src-fence", ["/a"])
    async with sessionmaker()() as db:
        (claimed,) = await jobs.claim_batch(db, owner_generation="gen-a", limit=1)
        await db.commit()
    async with sessionmaker()() as db:  # the job was recovered and re-claimed by someone else: the old token is dead
        await db.execute(update(CollectorJob).where(CollectorJob.id == job_id).values(attempt_token=uuid.uuid4()))
        await db.commit()
    ctx = runner.JobContext(claimed)
    stale_item = {"added": False}

    async def work(db):
        db.add(CollectorItem(id=uuid.uuid4(), job_id=job_id, tenant_id="fixture-tenant", scope="personal", source_key="src-fence",
                             kind="document", status="held", stage="fetched", url="http://x/", url_key="k" * 64, depth=0,
                             reason_codes=[], errors=[], signals={}, created_at=NOW))
        await db.flush()
        stale_item["added"] = True
        return {"v": 1, "frontier": [], "seen": [], "processed": 1, "discovered": 1, "counts": {}}, []

    from ttakkari.collector.contracts import AttemptFencedError
    with pytest.raises(AttemptFencedError):
        await ctx.commit_work(work)
    assert stale_item["added"] and await items(job_id) == []
    assert (await job_row(job_id)).cursor is None
