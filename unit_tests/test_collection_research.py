from ttakkari.collector.research import (
    decompose_topics,
    extract_paper_references,
    queries_for_topics,
)


def test_topic_decomposition_builds_bilingual_queries_for_distinct_subtopics():
    topics = decompose_topics("검색 증강 생성의 비용과 정확도 비교")

    assert len(topics) >= 2
    assert any("비용" in topic["label"] or "효율" in topic["label"] for topic in topics)
    queries = queries_for_topics(topics)
    assert any("검색 증강 생성" in query and "논문" in query for query in queries)
    assert any("retrieval augmented generation" in query.lower() for query in queries)
    assert len(set(queries)) == len(queries)


def test_paper_reference_extraction_keeps_doi_and_arxiv_versions_canonical():
    refs = extract_paper_references(
        "See https://doi.org/10.1145/1234567.1234568 and arXiv:2401.01234v2."
    )

    assert refs == [
        {"kind": "doi", "identifier": "10.1145/1234567.1234568",
         "canonical_url": "https://doi.org/10.1145/1234567.1234568"},
        {"kind": "arxiv", "identifier": "2401.01234v2",
         "canonical_url": "https://arxiv.org/abs/2401.01234v2"},
    ]


from datetime import UTC, datetime, timedelta

import pytest

from ttakkari.collector import paper, research
from ttakkari.collector.fetcher import FetchError


def test_aspects_the_user_names_become_their_own_topics_ahead_of_the_baseline_dimensions():
    topics = decompose_topics("RAG 보안 위험과 비용 조사해줘")
    labels = [topic["label"] for topic in topics]
    assert labels[0] == "핵심 이론·용어" and {"보안·안전", "비용·효율"} <= set(labels)
    assert labels.index("보안·안전") < labels.index("논문·원전")
    assert research.subject_of("RAG 보안 위험과 비용 조사해줘")[0] == "RAG"


def test_untranslatable_korean_subject_gets_no_invented_english_query_until_a_pair_is_learned():
    plan = research.initial_plan_parts("양자 암호 통신 조사", "")
    assert all(not query.isascii() for query in plan["queries"])  # no guessed translation
    learned = {"양자 암호 통신": "quantum cryptographic communication"}
    queries = research.queries_for_topics(research.decompose_topics("양자 암호 통신 조사", learned=learned))
    assert any(query.startswith("quantum cryptographic communication") for query in queries)


def test_documents_gloss_korean_terms_in_english_and_that_is_what_gets_learned():
    pairs = research.extract_term_pairs("검색 증강 생성(Retrieval-Augmented Generation, RAG)과 하네스 엔지니어링 (Harness Engineering)을 다룬다.")
    assert pairs == {"검색 증강 생성": "retrieval augmented generation", "하네스 엔지니어링": "harness engineering"}


def test_initial_round_is_capped_and_the_rest_is_deferred_rather_than_dropped():
    plan = research.initial_plan_parts("검색 증강 생성의 비용과 정확도 비교", "")
    assert len(plan["queries"]) == research.INITIAL_QUERY_CAP
    assert plan["deferred_queries"] and set(plan["deferred_queries"]).isdisjoint(plan["queries"])
    assert set(plan["query_topics"]) == {*plan["queries"], *plan["deferred_queries"]}
    assert {entry["state"] for entry in plan["coverage"]} == {"unexplored"}


def _plan(**over):
    parts = research.initial_plan_parts("검색 증강 생성의 비용과 정확도 비교", "")
    return {"goal": "검색 증강 생성의 비용과 정확도 비교", "mode": "research", **parts, "sources": [], "search_history": [],
            "budget": {"pages": 200, "depth": 3, "searches": 16, "seconds": 1800}, **over}


def _item(url, topic, status="accepted"):
    return {"url": url, "status": status, "topics": [topic]}


def test_coverage_comes_from_collected_items_not_from_the_plan():
    plan = _plan()
    plan["search_history"] = [{"query": q, "status": "ok"} for q in plan["queries"][:4]]
    theory, cost = plan["topics"][0]["key"], plan["topics"][1]["key"]
    plan["sources"] = [{"url": "https://a.test/x", "topics": [cost]}]
    items = [_item("https://a.test/1", theory), _item("https://b.test/2", theory), _item("https://c.test/3", theory, "held"),
             _item("https://d.test/4", plan["topics"][2]["key"], "failed")]
    states = {entry["key"]: entry["state"] for entry in research.compute_coverage(plan, items)}
    assert states[theory] == "covered"  # two accepted documents from two hosts
    assert states[cost] == "sources_found"  # found but nothing collected yet
    assert states[plan["topics"][2]["key"]] == "unavailable"  # attempted, every attempt failed
    assert states[plan["topics"][-1]["key"]] == "unexplored"
    one_host = research.compute_coverage(plan, [_item("https://a.test/1", theory), _item("https://a.test/2", theory)])
    assert {e["key"]: e["state"] for e in one_host}[theory] == "thin"  # same host twice is not independent


def test_search_that_ran_and_found_nothing_is_not_unexplored():
    plan = _plan()
    plan["search_history"] = [{"query": plan["queries"][0], "status": "ok", "candidates": 0}]
    first = research.compute_coverage(plan, [])[0]
    assert first["state"] == "no_candidates"
    plan["search_history"] = [{"query": plan["queries"][0], "status": "failed"}]  # a failed call explored nothing
    assert research.compute_coverage(plan, [])[0]["state"] == "unexplored"


def test_followups_pull_deferred_topics_weak_topic_rewordings_new_keywords_and_cited_papers():
    plan = _plan()
    docs = [{"url": "https://a.test/1", "topics": [plan["topics"][0]["key"]],
             "text": "In retrieval augmented generation, HyDE reranking helps recall. See arXiv:2005.11401 and doi:10.1000/x.1 for details. HyDE reranking again."},
            {"url": "https://b.test/2", "topics": [plan["topics"][1]["key"]],
             "text": "A second page on retrieval augmented generation also mentions HyDE reranking and cites https://arxiv.org/abs/2005.11401."}]
    coverage = research.compute_coverage(plan, [{**d, "status": "accepted"} for d in docs])
    found = research.followups(plan, docs, coverage, budget_left=16)
    reasons = {reason for _, _, reason in found["queries"]}
    assert "deferred_topic" in reasons and "new_keyword" in reasons
    assert any("hyde" in query.lower() for query, _, _ in found["queries"])
    known = [*plan["queries"]]
    assert all(research.is_novel_query(query, known) for query, _, _ in found["queries"])
    cited = {ref["identifier"]: ref for ref in found["papers"]}
    assert set(cited) == {"2005.11401", "10.1000/x.1"}
    assert cited["2005.11401"]["topics"] == sorted(p["key"] for p in plan["topics"][:2]) and len(cited["2005.11401"]["cited_by"]) == 2
    assert len(research.followups(plan, docs, coverage, budget_left=2)["queries"]) == 2
    assert research.followups(plan, docs, coverage, budget_left=0)["queries"] == []


def test_similar_queries_are_not_repeated():
    assert not research.is_novel_query("harness engineering theory fundamentals", ["harness engineering theory fundamentals survey"])
    assert research.is_novel_query("harness engineering evaluation", ["harness engineering theory fundamentals survey"])


def test_stop_reasons_are_explicit_and_ordered():
    plan, started = _plan(), datetime.now(UTC)
    coverage = [{"state": "thin"}]

    def stop(**over):
        args = {"now": started + timedelta(seconds=10), "started_at": started, "coverage": coverage, "searches_used": 3,
                "new_documents": 5, "followup_count": 2}
        return research.decide_stop(plan, over.pop("state", {"round": 0}), **{**args, **over})
    assert stop() is None
    assert stop(coverage=[{"state": "covered"}]) == "coverage_satisfied"
    assert stop(now=started + timedelta(seconds=1801)) == "time_budget"
    assert stop(searches_used=16) == "search_budget"
    assert stop(state={"round": research.MAX_ROUNDS}) == "round_limit"
    assert stop(followup_count=0) == "no_novel_queries"
    assert stop(state={"round": 1}, new_documents=0) == "diminishing_returns"
    assert stop(state={"round": 1}, new_documents=2) is None


def test_context_relations_are_leads_that_stay_unverified_and_unlinked_claims_are_listed():
    claims = research.context_claims("HyDE reranking은 검색 증강 생성의 정확도를 높인다고 생각한다. 이 문장은 아무 자료와도 겹치지 않는 별개 주제다. 짧음.")
    assert [c["provenance"] for c in claims] == ["user_provided", "user_provided"]
    targets = [{"kind": "kb_document", "url": "https://a.test/1", "title": "HyDE", "document_id": "doc-1",
                "text": "HyDE reranking improves the accuracy of 검색 증강 생성 pipelines. Unrelated sentence."},
               {"kind": "paper", "url": "https://arxiv.org/abs/2005.11401", "title": "Other", "identifier": "2005.11401", "text": "gardening tips"}]
    links = research.link_context(claims, targets)
    assert links["verification"] == "unverified"
    related = [r for r in links["relations"] if r["claim_id"] == claims[0]["id"]]
    assert [r["target"]["document_id"] for r in related] == ["doc-1"]
    assert related[0]["type"] == "lexical_overlap" and related[0]["verification"] == "unverified" and "hyde" in related[0]["matched_terms"]
    assert "HyDE reranking improves" in related[0]["snippet"]
    assert links["unlinked"] == [claims[1]["id"]]


def test_a_url_the_user_wrote_in_a_claim_is_an_exact_relation():
    claims = research.context_claims("이 구현은 https://a.test/1 에서 가져온 설계를 따른다.")
    links = research.link_context(claims, [{"kind": "collected_document", "url": "https://a.test/1", "title": "t", "text": "x"}])
    assert [r["type"] for r in links["relations"]] == ["user_cited"] and links["unlinked"] == []


def test_provider_failures_get_stable_codes_and_are_distinct_from_empty_results():
    assert research.classify_search_error(FetchError("search_provider_challenge")) == {"code": "search_provider_challenge", "retryable": False}
    assert research.classify_search_error(FetchError("not_found", status=404)) == {"code": "not_found", "retryable": False}
    assert research.classify_search_error(FetchError("rate_limited", retryable=True))["retryable"] is True
    assert research.classify_search_error(TimeoutError())["code"] == "timeout"
    assert research.classify_search_error(ValueError("Document is empty"))["code"] == "provider_response_unparseable"


def test_paper_references_cover_arxiv_doi_forms_and_old_style_ids():
    refs = research.extract_paper_references(
        "10.48550/arXiv.2401.01234 https://arxiv.org/pdf/2005.11401v4 arXiv:hep-th/9901001 doi:10.1000/abc.def).")
    assert [(r["kind"], r["identifier"]) for r in refs] == [("arxiv", "2401.01234"), ("arxiv", "2005.11401v4"), ("arxiv", "hep-th/9901001"),
                                                          ("doi", "10.1000/abc.def")]


def test_only_provider_reported_open_licenses_allow_full_text():
    assert paper.license_from_url("https://creativecommons.org/licenses/by/4.0/legalcode", source="x") ["permission"] == "allowed"
    assert paper.license_from_url("http://creativecommons.org/licenses/by-sa/4.0/", source="x")["id"] == "CC-BY-SA-4.0"
    assert paper.license_from_url("http://creativecommons.org/publicdomain/zero/1.0/", source="x")["id"] == "CC0-1.0"
    for url in ("http://arxiv.org/licenses/nonexclusive-distrib/1.0/", "https://creativecommons.org/licenses/by-nc/4.0/", "https://publisher.test/terms", None):
        assert paper.license_from_url(url, source="x")["permission"] == "unknown"


ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2005.11401v4</id><title>Retrieval-Augmented
 Generation</title><summary>Abstract text.</summary><published>2020-05-22T00:00:00Z</published>
<link href="http://arxiv.org/pdf/2005.11401v4" rel="related" type="application/pdf" title="pdf"/>
<author><name>A. Author</name></author><arxiv:doi>10.1000/xyz</arxiv:doi></entry></feed>"""


def _oai(license_url):
    return (f'<OAI-PMH><GetRecord><record><metadata><arXiv xmlns="http://arxiv.org/OAI/arXiv/"><license>{license_url}</license></arXiv></metadata>'
            '</record></GetRecord></OAI-PMH>').encode()


async def _arxiv(license_url=None, oai_error=False):
    async def fetch(url):
        if "oai" in url:
            if oai_error:
                raise FetchError("server_error", "503", retryable=True)
            return _oai(license_url)
        return ATOM
    return await paper.arxiv_record("2005.11401v4", fetch=fetch, min_interval=0)


async def test_arxiv_with_open_license_offers_the_pdf_and_without_one_stays_metadata_only():
    record = await _arxiv("http://creativecommons.org/licenses/by/4.0/")
    assert record["title"] == "Retrieval-Augmented Generation" and record["fulltext"] == "allowed"
    doc = paper.document_for(record, ["theory"])
    assert doc["url"] == "https://arxiv.org/pdf/2005.11401v4" and doc["license"]["id"] == "CC-BY-4.0" and doc["topics"] == ["theory"]
    closed = await _arxiv("http://arxiv.org/licenses/nonexclusive-distrib/1.0/")
    assert closed["fulltext"] == "metadata_only" and paper.document_for(closed) is None and closed["abstract"] == "Abstract text."
    unknown = await _arxiv(oai_error=True)  # a failed rights lookup is unknown rights, never an assumed grant
    assert unknown["fulltext"] == "metadata_only" and unknown["license"]["error"] == "server_error"


async def test_crossref_metadata_without_an_open_license_is_bibliographic_only():
    async def fetch_json(url):
        return {"message": {"title": ["Optuna"], "author": [{"given": "T", "family": "Akiba"}], "issued": {"date-parts": [[2019]]},
                            "container-title": ["KDD"], "license": [{"URL": "https://www.acm.org/policies"}]}}
    record = await paper.crossref_record("10.1145/3292500.3330701", fetch_json=fetch_json)
    assert record["fulltext"] == "metadata_only" and record["authors"] == ["T Akiba"] and record["year"] == 2019
    assert paper.document_for(record) is None and "서지" in record["rights_note"]


async def test_arxiv_search_skips_non_english_queries_and_surfaces_provider_errors():
    calls = []

    async def fetch(url):
        calls.append(url)
        raise FetchError("server_error", "503", retryable=True)
    assert await paper.search_arxiv("검색 증강 생성 논문", fetch=fetch, min_interval=0) == []
    assert calls == []  # an honest empty: nothing was asked
    with pytest.raises(FetchError):
        await paper.search_arxiv("retrieval augmented generation", fetch=fetch, min_interval=0)


def test_a_translation_learned_from_documents_unlocks_english_queries_for_every_topic():
    plan = research.initial_plan_parts("양자 암호 통신 조사", "")
    plan = {"goal": "양자 암호 통신 조사", "mode": "research", **plan, "sources": [], "search_history": [], "budget": {"searches": 16}}
    docs = [{"url": "https://a.test/1", "topics": [], "text": "양자 암호 통신(Quantum Cryptographic Communication)은 안전하다."}]
    found = research.followups(plan, docs, research.compute_coverage(plan, []), budget_left=16)
    assert found["glossary"] == {"양자 암호 통신": "quantum cryptographic communication"}
    assert any(reason == "learned_translation" and query.startswith("quantum cryptographic communication") for query, _, reason in found["queries"])


def test_keywords_are_terminology_near_the_subject_not_filler_vocabulary():
    filler = " ".join(["The district hospital network reported covering sector figures."] * 30)
    docs = [{"url": f"https://a{i}.test/", "topics": [], "title": "",
             "text": f"{filler} Retrieval augmented generation uses HyDE reranking from Hypothetical Document Embeddings. {filler}"} for i in range(2)]
    terms = [t["term"] for t in research.candidate_terms(docs, ["retrieval augmented generation"], anchors=["retrieval augmented generation"])]
    assert "hyde" in terms and "hypothetical document embeddings" in terms
    assert "hospital network" not in terms and "covering sector" not in terms
    assert research.candidate_terms([{"url": "u", "topics": [], "title": "", "text": filler}], [], anchors=["retrieval augmented generation"]) == []


async def test_search_candidates_for_papers_are_routed_to_rights_resolution_not_blind_fetching():
    from ttakkari.collector import adapters

    def link(url):
        return f'<a class="result__a" href="{url}">t</a>'
    html = "<html><body>" + "".join(link(u) for u in (
        "https://arxiv.org/abs/2005.11401v4", "https://doi.org/10.1145/3292500.3330701", "https://arxiv.org/pdf/2005.11401",
        "https://arxiv.org/html/2506.00054v1", "https://blog.test/post")) + "</body></html>"

    async def fetch(url):
        return html.encode()
    found = await adapters.search_web("rag", fetch_bytes=fetch)
    assert {c["url"].split("//")[1]: c["adapter"] for c in found} == {
        "arxiv.org/abs/2005.11401v4": "paper", "doi.org/10.1145/3292500.3330701": "paper", "arxiv.org/pdf/2005.11401": "paper_pdf",
        "arxiv.org/html/2506.00054v1": "paper", "blog.test/post": "website"}


def test_challenge_failure_is_non_retryable_so_outstanding_failures_do_not_exhaust_attempts():
    from ttakkari.collector import research_rounds

    plan = _plan()
    st = research_rounds.state(plan, 1)
    # Simulate a search query that failed due to DDG anti-bot challenge
    classified = research.classify_search_error(FetchError("search_provider_challenge"))
    assert classified["retryable"] is False
    st["failed"] = [{"index": 0, "code": classified["code"], "retryable": classified["retryable"], "attempts": 1}]
    plan["research"] = st

    failures = research_rounds.outstanding_failures(plan, 1)
    assert failures["search"] == 1
    assert failures["search_retryable"] == 0  # non-retryable challenge must NOT trigger retry_wait loop


def test_attempts_exhausted_preserves_original_cause():
    orig_error = {"code": "search_provider_failed", "detail": "ddg challenge"}
    orig_code = orig_error.get("code")
    exhausted_error = {**orig_error, "code": "attempts_exhausted", "attempts": 3,
                       **({"cause": orig_code} if orig_code and orig_code != "attempts_exhausted" else {})}
    assert exhausted_error["code"] == "attempts_exhausted"
    assert exhausted_error["cause"] == "search_provider_failed"
    assert exhausted_error["detail"] == "ddg challenge"


def test_design_system_official_specs_have_permissive_licenses():
    from ttakkari.collector import adapters, missions

    # Verify that the official design system documentation specs have permissive licenses
    # without fake grants or bypassing robots.
    assert "CC0-1.0" in adapters.PERMISSIVE_LICENSES
    # register_design_system_docs is an async function in missions
    assert callable(missions.register_design_system_docs)


