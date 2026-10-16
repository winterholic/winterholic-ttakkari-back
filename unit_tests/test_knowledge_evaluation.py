"""Independent relevance fixtures catch denominator, grouping and missing-run mistakes."""

import importlib
import importlib.util

import pytest


def evaluator():
    assert importlib.util.find_spec("ttakkari.knowledge.evaluation") is not None, (
        "Independent evaluation runner is missing"
    )
    return importlib.import_module("ttakkari.knowledge.evaluation")


def test_macro_recall_is_not_any_hit_rate():
    cases = [
        {"id": "one", "group": "g1", "relevance": {"a": 3, "b": 1}},
        {"id": "two", "group": "g2", "relevance": {"c": 2}},
    ]
    r = evaluator().score(cases, {"one": ["a", "x"], "two": []}, k=5)
    assert r["recall"] == 0.25
    assert r["hit_rate"] == 0.5
    assert r["mrr"] == 0.5
    assert r["queries"] == 2
    assert r["relevant_targets"] == 3


def test_missing_predictions_and_unlabelled_gold_are_errors():
    cases = [{"id": "one", "group": "g1", "relevance": {"a": 3}}]
    with pytest.raises(ValueError, match="missing_prediction"):
        evaluator().score(cases, {}, k=5)
    with pytest.raises(ValueError, match="empty_gold"):
        evaluator().score(
            [{"id": "bad", "group": "g", "relevance": {}}], {"bad": []}, k=5
        )


def test_duplicate_hits_do_not_inflate_recall_or_ndcg():
    case = [{"id": "one", "group": "g", "relevance": {"a": 3, "b": 2}}]
    r = evaluator().score(case, {"one": ["a", "a", "a"]}, k=5)
    assert r["recall"] == 0.5 and 0 < r["ndcg"] < 1


def test_holdout_cannot_overlap_conversation_family():
    dev = [{"id": "one", "group": "conversation1"}]
    holdout = [{"id": "two", "group": "conversation1"}]
    with pytest.raises(ValueError, match="group_leakage"):
        evaluator().validate_split(dev, holdout)


def test_paired_bootstrap_reports_new_minus_baseline_with_query_denominator():
    r = evaluator().paired_interval([0, 0.5, 0, 0.5], [1, 1, 1, 1], seed=20261016)
    assert r["delta"] == 0.75
    assert 0 < r["low"] <= r["high"] <= 1
    assert r["pairs"] == 4


def test_bm25_counts_repeated_terms_in_short_documents():
    documents = {"z": "단어 단어 단어 기타", "a": "단어 기타 기타 기타"}
    assert evaluator().bm25("단어", documents)[0] == "z"
