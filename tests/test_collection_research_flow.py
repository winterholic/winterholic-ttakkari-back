"""Scenario E on a disposable PostgreSQL and a local fixture web server: a topic with no URL becomes bilingual queries,
independent sources, follow-up searches from what was collected, coverage from evidence, and KB documents.

Search and paper providers are replaced by deterministic fakes; page fetching, jobs, fences, triage, outbox and KB
delivery are the real ones. No public network.
"""
import json

import pytest
from collector_support import article_html, prose
from sqlalchemy import select
from test_collector_control import BASE, source_body
from test_collector_pipeline import env  # noqa: F401

from ttakkari import kb_delivery
from ttakkari.collector import adapters, paper, render, runner
from ttakkari.collector.fetcher import FetchError
from ttakkari.collector_models import CollectorItem
from ttakkari.db import sessionmaker

pytestmark = pytest.mark.security
GOAL = "검색 증강 생성의 비용과 정확도 비교"
CONTEXT = "가상 문서 임베딩은 검색 증강 생성의 정확도를 높인다. 이 문장은 다른 자료와 겹치지 않는 완전히 별개의 사안이다."


async def open_source(client, auth, env, hosts=("docs.test", "other.test"), key="research-fixture"):  # noqa: F811
    body = source_body(source_key=key, seeds=[env.server.url("/seed", host=hosts[0])], max_pages=30)
    body["policy"].update(allowed_hosts=list(hosts), schemes=["http"], ports=[env.server.port], min_delay_seconds=0,
                          max_pages_per_job=30)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201


def candidate(url, query):
    return {"url": url, "adapter": "website", "provenance": "web_search", "query": query, "title": url}


async def drain(monkeypatch, env, *, rounds=60):  # noqa: F811
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    for _ in range(rounds):
        ran = await runner.consume_once(limit=5, managed_only=True)
        delivered = await kb_delivery.deliver_once()
        if not ran and not delivered:
            return
    raise AssertionError("jobs never settled")


async def mission(client, auth, mid):
    return (await client.get(BASE + "/missions/" + mid, headers=auth)).json()


def site(env):  # noqa: F811
    pages = {
        "/rag-intro": ("docs.test", article_html("RAG intro", [
            ("검색 증강 생성(Retrieval-Augmented Generation, RAG)은 외부 지식을 검색해 답변을 만드는 방식이다. HyDE reranking improves recall "
             "of the retriever. See arXiv:2005.11401 and doi:10.1000/closed.2 for the original description."), prose("en", 201, 14)])),
        "/rag-survey": ("other.test", article_html("RAG survey", [
            "This survey of retrieval augmented generation analyses HyDE reranking and cites https://arxiv.org/abs/2005.11401 as the starting point.", prose("en", 202, 14)])),
        "/rag-cost": ("other.test", article_html("RAG cost", ["Serving cost per query depends on retrieval depth and context length.", prose("en", 203, 14)])),
        "/rag-eval": ("docs.test", article_html("RAG evaluation", ["Empirical evaluation of answer accuracy on open benchmarks.", prose("en", 204, 14)])),
        "/rag-hyde": ("docs.test", article_html("HyDE reranking", [
            "가상 문서 임베딩(HyDE)은 검색 증강 생성의 정확도를 높인다. HyDE reranking improves the accuracy of retrieval augmented generation pipelines.",
            prose("en", 205, 14)])),
        "/paper-2005.pdf": ("docs.test", article_html("Retrieval-Augmented Generation for Knowledge-Intensive NLP", [
            "The paper introduces retrieval augmented generation for knowledge intensive tasks.", prose("en", 206, 14)])),
    }
    for path, (_, html) in pages.items():
        env.server.page(path, html)
    return {path: env.server.url(path, host=host) for path, (host, _) in pages.items()}


def fake_search(urls, calls):
    async def search(query, **kwargs):
        calls.append(query)
        q = query.lower()
        if "hyde" in q:
            return [candidate(urls["/rag-hyde"], query)]
        if "평가 지표" in q or "empirical" in q:
            return [candidate(urls["/rag-eval"], query)]
        if "개념" in q or "theory" in q:
            return [candidate(urls["/rag-intro"], query), candidate(urls["/rag-survey"], query)]
        if "비용" in q or "cost" in q:
            return [candidate(urls["/rag-cost"], query)]
        if "논문" in q or "paper" in q:
            return [candidate(urls["/rag-survey"], query)]
        return []
    return search


def fake_papers(urls):
    async def resolve(ref, **kwargs):
        if ref["identifier"] == "2005.11401":
            rights = {"id": "CC-BY-4.0", "url": "https://creativecommons.org/licenses/by/4.0/", "source": "arxiv_oai",
                      "permission": "allowed", "error": None, "metadata_sha": "a" * 64}
            record = {"kind": "arxiv", "identifier": ref["identifier"], "canonical_url": ref["canonical_url"], "title": "Retrieval-Augmented Generation",
                      "authors": ["A. Author"], "year": 2020, "venue": "arXiv", "abstract": "Retrieval augmented generation for knowledge tasks.",
                      "doi": None, "pdf_url": urls["/paper-2005.pdf"]}
            return paper._finish(record, rights, "arxiv_api+oai")
        rights = {"id": None, "url": "https://publisher.test/terms", "source": "crossref", "permission": "unknown", "error": None, "metadata_sha": "b" * 64}
        record = {"kind": "doi", "identifier": ref["identifier"], "canonical_url": ref["canonical_url"], "title": "Closed paper", "authors": [],
                  "year": 2019, "venue": "Journal", "abstract": None, "doi": ref["identifier"], "pdf_url": None}
        return paper._finish(record, rights, "crossref")
    return resolve


async def test_topic_without_urls_becomes_followup_research_with_real_coverage_papers_and_context_relations(client, auth, env, monkeypatch):  # noqa: F811
    await open_source(client, auth, env)
    urls = site(env)
    calls = []
    monkeypatch.setattr(adapters, "search_web", fake_search(urls, calls))
    monkeypatch.setattr(paper, "resolve", fake_papers(urls))
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": GOAL, "context": CONTEXT})).json()
    assert calls == []  # the API only enqueues
    assert made["plan"]["mode"] == "research" and made["plan"]["budget"]["model_calls"] == 0
    assert {c["state"] for c in made["plan"]["coverage"]} == {"unexplored"}
    await drain(monkeypatch, env)
    detail = await mission(client, auth, made["id"])
    plan, research = detail["plan"], detail["research"]

    # bilingual queries: Korean and English per topic, and the user's own claim becomes a search topic
    assert any("검색 증강 생성" in q for q in calls) and any("retrieval augmented generation" in q.lower() for q in calls)
    assert any(t["key"] == "context" for t in plan["topics"])
    # follow-up rounds ran from collected evidence: deferred topics, a keyword learned from the documents, rewordings
    rounds = research["rounds"]
    assert rounds and rounds[0]["queries"]
    followed = [h for h in plan["search_history"] if h["round"] >= 1]
    assert followed and any("hyde" in h["query"].lower() for h in followed)
    assert {r for rd in rounds for r in rd["reasons"]} >= {"deferred_topic", "new_keyword"}
    assert "/rag-hyde" in " ".join(s["url"] for s in plan["sources"])  # found only through the learned keyword
    # coverage is recomputed from items, not from the plan, and the remainder is listed
    states = {c["key"]: c["state"] for c in research["coverage"]}
    assert states[plan["topics"][0]["key"]] == "covered"  # two accepted documents from two different hosts
    assert research["remaining"] and all(r["state"] != "covered" for r in research["remaining"])
    assert any(g["reason"] == "research_coverage_incomplete" for g in detail["gaps"]) or research["stop"] == "coverage_satisfied"
    # explicit stop reason and bounded budget
    assert research["stop"] in {"no_novel_queries", "diminishing_returns", "search_budget", "round_limit", "coverage_satisfied"}
    assert research["searches_used"] <= plan["budget"]["searches"] and research["round"] <= plan["budget"]["rounds"] + 1
    # papers: the cited arXiv paper is open-licensed so its text was collected; the closed DOI stays bibliographic
    by_id = {p["identifier"]: p for p in plan["papers"]}
    assert by_id["2005.11401"]["fulltext"] == "allowed" and by_id["10.1000/closed.2"]["fulltext"] == "metadata_only"
    assert not any(urls["/paper-2005.pdf"] == s["url"] and s["adapter"] != "paper_pdf" for s in plan["sources"])
    gap = next(g for g in detail["gaps"] if g["reason"] == "paper_rights_unregistered")
    assert gap["identifier"] == "10.1000/closed.2" and "서지" in gap["message"]
    # context claims relate to collected material as unverified leads, with unlinked claims listed
    links = plan["context_links"]
    first, second = links["claims"]
    related = [r for r in links["relations"] if r["claim_id"] == first["id"]]
    assert related and all(r["verification"] == "unverified" and r["type"] in {"lexical_overlap", "user_cited"} for r in related)
    assert any(r["target"].get("document_id") for r in related)  # a KB document id, not just a URL
    assert second["id"] in links["unlinked"] and plan["context"]["fact_check_status"] == "unverified"
    # real KB delivery of what was collected, searchable
    assert detail["counts"]["imported"] >= 5, detail["counts"]
    assert (await client.get("/api/assistant/kb/search?q=HyDE", headers=auth)).json()["items"]
    assert detail["status"] != "succeeded"  # gaps remain and say so
    prepares = [j for j in detail["jobs"] if j["stage"] == "mission_prepare"]
    assert len(prepares) == research["round"] + 1  # one initial job plus exactly one job per round, never a duplicate


async def test_provider_failure_is_a_recorded_failure_retried_and_never_an_empty_success(client, auth, env, monkeypatch):  # noqa: F811
    from datetime import timedelta

    from ttakkari.collector import jobs

    monkeypatch.setattr(jobs, "retry_delay", lambda attempts, retry_after=None: timedelta(0))

    async def blocked(query, **kwargs):
        raise FetchError("search_provider_challenge", "anti-bot challenge")
    monkeypatch.setattr(adapters, "search_web", blocked)
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": GOAL})).json()
    await drain(monkeypatch, env)
    detail = await mission(client, auth, made["id"])
    reasons = [g["reason"] for g in detail["gaps"]]
    assert "search_provider_failed" in reasons and "no_search_candidates" not in reasons
    assert detail["status"] == "failed"
    assert detail["counts"]["acquired"] == 0 and detail["counts"]["imported"] == 0
    history = detail["plan"]["search_history"]
    assert history and {h["status"] for h in history} == {"failed"} and {h["error"] for h in history} == {"search_provider_challenge"}
    assert detail["plan"]["search_stop"] != "diminishing_returns"  # failures are not "no new results"
    assert detail["research"]["stop"] == "provider_failed"
    assert any(j["stage"] == "mission_prepare" and j["status"] == "partial" for j in detail["jobs"])
    # an empty answer, by contrast, is a different outcome
    other = (await client.post(BASE + "/missions", headers=auth, json={"goal": "자료가 없는 주제 조사"})).json()

    async def empty(query, **kwargs):
        return []
    monkeypatch.setattr(adapters, "search_web", empty)
    await drain(monkeypatch, env)
    empty_detail = await mission(client, auth, other["id"])
    gaps = [g["reason"] for g in empty_detail["gaps"]]
    assert "no_search_candidates" in gaps and "search_provider_failed" not in gaps
    assert empty_detail["status"] == "failed"
    assert empty_detail["counts"]["acquired"] == 0 and empty_detail["counts"]["imported"] == 0


async def test_provider_that_recovers_clears_the_failure_and_finishes_the_search(client, auth, env, monkeypatch):  # noqa: F811
    from datetime import timedelta

    from ttakkari.collector import jobs

    monkeypatch.setattr(jobs, "retry_delay", lambda attempts, retry_after=None: timedelta(0))
    await open_source(client, auth, env)
    urls = site(env)
    calls, failures = [], []
    inner = fake_search(urls, calls)

    async def flaky(query, **kwargs):
        if len(failures) < 3:  # the provider is down for the first three calls, then recovers
            failures.append(query)
            raise FetchError("server_error", "503", retryable=True)
        return await inner(query, **kwargs)
    monkeypatch.setattr(adapters, "search_web", flaky)
    monkeypatch.setattr(paper, "resolve", fake_papers(urls))
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": GOAL})).json()
    await drain(monkeypatch, env)
    detail = await mission(client, auth, made["id"])
    statuses = [h["status"] for h in detail["plan"]["search_history"]]
    assert statuses[:3] == ["failed"] * 3 and "ok" in statuses
    assert "search_provider_failed" not in [g["reason"] for g in detail["gaps"]]  # the retried queries succeeded
    assert detail["counts"]["imported"] >= 2


SPA = ('<html><head><title>Docs app</title><script src="/app.js"></script></head><body><div id="root"></div>'
       '<noscript>You need to enable JavaScript to run this app.</noscript></body></html>')


def render_runtime():
    from pathlib import Path
    root = Path(__file__).parents[2] / "back" / ".browser-runtime"
    browsers = Path.home() / "Library/Caches/ms-playwright"
    executable = render._find_chromium(browsers)
    node = render.shutil.which("node")
    if (root / "node_modules" / "playwright-core").is_dir() and executable and node:
        return {"node": node, "runtime": str(root), "executable": str(executable), "browsers": str(browsers)}
    return None


async def test_empty_script_shell_is_unrenderable_with_an_explicit_reason_when_no_browser_runtime(client, auth, env, monkeypatch):  # noqa: F811
    await open_source(client, auth, env, hosts=("docs.test",), key="spa-source")
    env.server.page("/spa", SPA)

    def missing():
        raise render.RendererUnavailable("chromium_missing")
    monkeypatch.setattr(render, "runtime", missing)
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "앱 문서 수집", "urls": [env.server.url("/spa")]})).json()
    await drain(monkeypatch, env)
    detail = await mission(client, auth, made["id"])
    assert detail["counts"]["imported"] == 0 and detail["counts"]["failed"] == 1
    async with sessionmaker()() as db:
        item = (await db.scalars(select(CollectorItem).where(CollectorItem.tenant_id == "owner", CollectorItem.url.like("%/spa")))).one()
        assert item.reason_codes == ["empty_extraction", "spa_render_unavailable"] and item.signals["render"]["error"] == "chromium_missing"


@pytest.mark.skipif(render_runtime() is None, reason="playwright-core and Chromium are not installed")
async def test_script_shell_page_is_rendered_collected_and_searchable_through_the_real_pipeline(client, auth, env, monkeypatch):  # noqa: F811
    await open_source(client, auth, env, hosts=("docs.test",), key="spa-source")
    body = "The durable job runner fences attempts and records heartbeats for crawlers. " + prose("en", 301, 14)
    env.server.page("/spa", SPA)
    env.server.page("/app.js", "fetch('/api/page').then(r => r.json()).then(d => { document.getElementById('root').innerHTML = "
                    "'<article><h1>' + d.title + '</h1><p>' + d.body + '</p></article>'; });", "application/javascript")
    env.server.page("/api/page", json.dumps({"title": "Runner internals", "body": body}), "application/json")
    monkeypatch.setattr(render, "runtime", render_runtime)

    # Use the pipeline's actual policy-aware fetcher, which already owns the
    # fixture resolver. Replacing default_fetch hid its policy/polite contract.
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "러너 내부 구조", "urls": [env.server.url("/spa")]})).json()
    await drain(monkeypatch, env)
    detail = await mission(client, auth, made["id"])
    assert detail["counts"]["imported"] == 1, detail
    async with sessionmaker()() as db:
        item = (await db.scalars(select(CollectorItem).where(CollectorItem.url.like("%/spa")))).one()
        info = item.envelope["extensions"]["collector:render"]
        assert info["used"] and info["static_chars"] == 0 and info["rendered_chars"] > 200 and info["requests"] >= 2
        assert "durable job runner" in item.envelope["normalized"]["text"]
        raw = env.blobs.get(item.raw_handle)  # the preserved original is the static response, not the rendered DOM
        assert b'id="root"' in raw and b"durable job runner" not in raw
    assert (await client.get("/api/assistant/kb/search?q=heartbeats", headers=auth)).json()["items"]


async def test_user_paper_link_and_context_in_one_mission_resolve_through_a_retry_and_relate_to_the_paper(client, auth, env, monkeypatch):  # noqa: F811
    from datetime import timedelta

    from ttakkari.collector import jobs

    monkeypatch.setattr(jobs, "retry_delay", lambda attempts, retry_after=None: timedelta(0))
    attempts = []

    async def inner(ref, **kwargs):  # an arXiv paper whose license is the default non-redistribution grant
        rights = {"id": None, "url": "http://arxiv.org/licenses/nonexclusive-distrib/1.0/", "source": "arxiv_oai", "permission": "unknown",
                  "error": None, "metadata_sha": "c" * 64}
        record = {"kind": "arxiv", "identifier": ref["identifier"], "canonical_url": ref["canonical_url"], "title": "Retrieval-Augmented Generation",
                  "authors": ["A. Author"], "year": 2020, "venue": "arXiv", "abstract": "Retrieval augmented generation for knowledge tasks.",
                  "doi": None, "pdf_url": "https://arxiv.org/pdf/" + ref["identifier"]}
        return paper._finish(record, rights, "arxiv_api+oai")

    async def flaky(ref, **kwargs):
        attempts.append(ref["identifier"])
        if len(attempts) == 1:
            raise FetchError("server_error", "503", retryable=True)
        return await inner(ref, **kwargs)
    monkeypatch.setattr(paper, "resolve", flaky)
    made = (await client.post(BASE + "/missions", headers=auth, json={
        "goal": "RAG 논문 맥락 정리", "urls": ["https://arxiv.org/abs/2005.11401v4"],
        "context": "Retrieval augmented generation for knowledge tasks improves factual answers. 전혀 관련 없는 요리 레시피 메모."})).json()
    assert made["plan"]["sources"][0]["adapter"] == "paper" and made["plan"]["mode"] == "mixed"
    await drain(monkeypatch, env)
    detail = await mission(client, auth, made["id"])
    assert attempts == ["2005.11401v4", "2005.11401v4"]  # the first provider failure was retried, not recorded as a result
    assert [p["identifier"] for p in detail["plan"]["papers"]] == ["2005.11401v4"]
    assert "paper_metadata_failed" not in [g["reason"] for g in detail["gaps"]]
    gap = next(g for g in detail["gaps"] if g["reason"] == "paper_rights_unregistered")
    assert gap["identifier"] == "2005.11401v4"
    links = detail["plan"]["context_links"]
    first, second = links["claims"]
    related = [r for r in links["relations"] if r["claim_id"] == first["id"]]
    assert related and related[0]["target"]["kind"] == "paper" and related[0]["target"]["identifier"] == "2005.11401v4"
    assert second["id"] in links["unlinked"]
    assert detail["research"]["stop"] == "no_novel_queries" and detail["counts"]["imported"] == 0


async def test_rerun_starts_a_fresh_research_round_state_and_the_view_carries_research_and_precise_blocks(client, auth, env, monkeypatch):  # noqa: F811
    await open_source(client, auth, env)
    urls = site(env)
    calls = []
    monkeypatch.setattr(adapters, "search_web", fake_search(urls, calls))
    monkeypatch.setattr(paper, "resolve", fake_papers(urls))
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": GOAL, "context": CONTEXT})).json()
    await drain(monkeypatch, env)
    first = await mission(client, auth, made["id"])
    assert first["run_seq"] == 1 and first["research"]["round"] >= 1
    first_prepares = [j for j in first["jobs"] if j["stage"] == "mission_prepare"]
    assert len(first_prepares) == first["research"]["round"] + 1 and {j["run_seq"] for j in first_prepares} == {1}

    assert (await client.post(BASE + "/missions/" + made["id"] + "/rerun", headers=auth)).status_code == 200
    await drain(monkeypatch, env)
    second = await mission(client, auth, made["id"])
    assert second["run_seq"] == 2
    research = second["research"]
    prepares = [j for j in second["jobs"] if j["stage"] == "mission_prepare" and j["run_seq"] == 2]
    # round state belongs to the new run: one preparation job plus one per round of this run, not carried over from run 1
    assert len(prepares) == research["round"] + 1
    assert len([j for j in second["jobs"] if j["stage"] == "mission_prepare" and j["run_seq"] == 1]) == len(first_prepares)
    assert research["searches_used"] <= second["plan"]["budget"]["searches"]
    assert research["stop"] is not None
    # the same view also carries the precise block and the waiting counts next to the research block
    precise = second["triage"]["precise_eval"]
    assert {"pending", "processing", "unresolved", "engine"} <= set(precise)
    assert {"precise_waiting", "precise_unresolved"} <= set(second["counts"])
    runs = {r["run_seq"] for r in second["runs"]}
    assert runs == {1, 2}
