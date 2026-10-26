"""KB v3 5경로 하이브리드 검색 및 RRF 재순위 단위/통합 테스트 (unit_tests/test_kb_v3_retrieval.py)."""

from __future__ import annotations

import datetime

from ttakkari.kb_ontology.retrieval import _restrict_canonical_provenance
from ttakkari.kb_retrieval import (
    BETA_CANONICAL,
    BETA_FRESH,
    BETA_TITLE,
    DIVERSITY_PENALTY,
    RRF_K,
    WEIGHT_LEXICAL,
    WEIGHT_VECTOR,
    _metadata_filter_predicates,
    compute_coverage,
    fuse_and_rerank,
)


def test_rrf_scoring_basic():
    # 2개 경로(lexical rank 1, vector rank 2)를 거친 패시지의 RRF 기본 점수 검증
    candidate_ranks = {
        "p1": {"lexical": 1, "vector": 2},
    }
    passage_metadata = {
        "p1": {
            "passage_id": "p1",
            "document_id": "doc1",
            "ord": 0,
            "is_canonical_evidence": False,
            "is_current_revision": False,
        }
    }
    ranked = fuse_and_rerank(candidate_ranks, passage_metadata, query_terms=["test"])
    assert len(ranked) == 1
    pid, score = ranked[0]
    assert pid == "p1"

    expected_score = (WEIGHT_LEXICAL / (RRF_K + 1)) + (WEIGHT_VECTOR / (RRF_K + 2))
    assert abs(score - expected_score) < 1e-6


def test_rrf_quality_signals():
    # 정본 가산점, 최신성 가산점, 타이틀 가산점 검증
    now = datetime.datetime.now(datetime.UTC)
    recent_date = now - datetime.timedelta(days=10)

    candidate_ranks = {
        "p_plain": {"lexical": 1},
        "p_boosted": {"lexical": 1},
    }
    passage_metadata = {
        "p_plain": {
            "passage_id": "p_plain",
            "document_id": "doc1",
            "ord": 0,
            "is_canonical_evidence": False,
            "is_current_revision": False,
        },
        "p_boosted": {
            "passage_id": "p_boosted",
            "document_id": "doc2",
            "ord": 0,
            "heading": "Kubernetes Deployment Guide",
            "title": "K8s Intro",
            "collected_at": recent_date,
            "is_current_revision": True,
            "is_canonical_evidence": True,
        },
    }
    ranked = fuse_and_rerank(candidate_ranks, passage_metadata, query_terms=["Kubernetes"])
    ranked_dict = dict(ranked)

    base_score = WEIGHT_LEXICAL / (RRF_K + 1)
    boosted_expected = base_score + BETA_CANONICAL + BETA_FRESH + BETA_TITLE

    assert abs(ranked_dict["p_plain"] - base_score) < 1e-6
    assert abs(ranked_dict["p_boosted"] - boosted_expected) < 1e-6
    # p_boosted 가 p_plain 보다 확실히 높은 순위여야 함
    assert ranked[0][0] == "p_boosted"


def test_document_diversity_cap():
    # 동일 문서에서 상위 2개를 초과하는 3번째 패시지는 0.5배 감점
    candidate_ranks = {
        "p1": {"lexical": 1},
        "p2": {"lexical": 2},
        "p3": {"lexical": 3},
        "p4_other_doc": {"lexical": 4},
    }
    passage_metadata = {
        "p1": {"passage_id": "p1", "document_id": "doc1", "ord": 0},
        "p2": {"passage_id": "p2", "document_id": "doc1", "ord": 1},
        "p3": {"passage_id": "p3", "document_id": "doc1", "ord": 2},
        "p4_other_doc": {"passage_id": "p4_other_doc", "document_id": "doc2", "ord": 0},
    }
    ranked = fuse_and_rerank(candidate_ranks, passage_metadata, query_terms=[])
    ranked_dict = dict(ranked)

    # p1, p2 는 감점 없음
    assert abs(ranked_dict["p1"] - (1.0 / (RRF_K + 1))) < 1e-6
    assert abs(ranked_dict["p2"] - (1.0 / (RRF_K + 2))) < 1e-6

    # p3 는 doc1의 3번째 패시지이므로 원점수(1/(60+3))의 0.5배가 되어야 함
    raw_p3 = 1.0 / (RRF_K + 3)
    assert abs(ranked_dict["p3"] - (raw_p3 * DIVERSITY_PENALTY)) < 1e-6

    # p4_other_doc 은 doc2의 첫번째 패시지이므로 감점 없음 (1/(60+4) = 1/64 = 0.015625)
    # p3 감점 후 점수: (1/63) * 0.5 = 0.007936
    # 따라서 p4_other_doc 이 p3 보다 앞서야 함! (다양성 보장)
    assert ranked_dict["p4_other_doc"] > ranked_dict["p3"]
    order = [pid for pid, _ in ranked]
    assert order == ["p1", "p2", "p4_other_doc", "p3"]


def test_deterministic_ties():
    # 점수가 완전히 같은 경우 (-score, doc_id, passage_id, ord) 순으로 일관성 보장
    candidate_ranks = {
        "p_b": {"lexical": 1},
        "p_a": {"lexical": 1},
    }
    passage_metadata = {
        "p_b": {"passage_id": "p_b", "document_id": "doc_z", "ord": 0},
        "p_a": {"passage_id": "p_a", "document_id": "doc_a", "ord": 0},
    }
    ranked = fuse_and_rerank(candidate_ranks, passage_metadata, query_terms=[])
    # doc_a 가 doc_z 보다 앞서므로 p_a 가 먼저 나와야 함
    assert ranked[0][0] == "p_a"
    assert ranked[1][0] == "p_b"


def test_coverage_computation():
    ranks_multi = {"p1": {"lexical": 1, "vector": 1}}
    passages_strong = [{"passage_id": "p1", "score": 0.035}]
    assert compute_coverage(passages_strong, [], ranks_multi) == "strong"

    passages_weak = [{"passage_id": "p1", "score": 0.012}]
    ranks_single = {"p1": {"lexical": 1}}
    assert compute_coverage(passages_weak, [], ranks_single) == "weak"

    assert compute_coverage([], [{"entity_id": "e1"}], {}) == "weak"
    assert compute_coverage([], [], {}) == "none"


def test_canonical_card_provenance_is_restricted_to_accessible_sources():
    card = {
        "source_document_ids": ["visible-doc", "hidden-doc"],
        "supporting_evidence_ids": ["visible-evidence", "hidden-evidence"],
        "has_open_conflict": True,
    }

    safe = _restrict_canonical_provenance(
        card,
        allowed_document_ids={"visible-doc"},
        allowed_evidence_ids={"visible-evidence"},
    )

    assert safe["source_document_ids"] == ["visible-doc"]
    assert safe["supporting_evidence_ids"] == ["visible-evidence"]


def test_metadata_filters_target_source_and_entity_lineage_columns():
    from ttakkari.kb_access import create_owner_context

    predicates = _metadata_filter_predicates(
        {
            "source_kind": "file",
            "domain": "ai",
            "collected_after": "2026-01-01T00:00:00+00:00",
            "concept": "Nimbus",
        },
        create_owner_context(),
    )
    sql = " ".join(
        str(predicate.compile(compile_kwargs={"literal_binds": True})).lower()
        for predicate in predicates
    )
    assert "kb_sources.kind = 'file'" in sql
    assert "source_meta ->> 'domain'" in sql and "= 'ai'" in sql
    assert "kb_sources.collected_at >= '2026-01-01 00:00:00+00:00'" in sql
    assert "lower(kb_entities_1.canonical_name) like lower('%nimbus%')" in sql
