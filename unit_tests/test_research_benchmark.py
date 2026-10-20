import pytest

from ttakkari.research.benchmark import evaluate


def test_duplicate_ids_and_missing_qrels_never_score_as_success():
    for config in [
        {
            "corpus": [{"id": "a", "text": "a"}, {"id": "a", "text": "b"}],
            "queries": [{"query": "a", "relevant": ["a"]}],
        },
        {
            "corpus": [{"id": "a", "text": "a"}],
            "queries": [{"query": "a", "relevant": ["missing"]}],
        },
    ]:
        with pytest.raises(ValueError):
            evaluate(config)


def test_raw_ranks_and_denominators_are_reproducible():
    result = evaluate(
        {
            "corpus": [
                {"id": "a", "text": "metal inference"},
                {"id": "b", "text": "speech"},
            ],
            "queries": [{"query": "metal", "relevant": ["a"]}],
            "k": 1,
        }
    )
    assert result["means"]["bm25"]["recall"] == 1
    assert result["rows"][0]["bm25"]["ids"] == ["a"]
    assert len(result["dataset_sha256"]) == 64
