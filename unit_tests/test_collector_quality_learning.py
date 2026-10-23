import pytest


def samples():
    return [{"text": ("useful technical explanation transaction tutorial " if i % 2 else "spam buy cheap advertisement now ") + str(i),
             "label": i % 2, "group": "source-" + str(i // 2), "id": str(i)} for i in range(40)]


def test_learning_requires_group_separation_and_both_classes():
    from ttakkari.collector.learning import fit
    with pytest.raises(ValueError, match="groups"):
        fit([{**s, "group": "same"} for s in samples()])
    with pytest.raises(ValueError, match="classes"):
        fit([{**s, "label": 1} for s in samples()])


def test_model_roundtrip_replays_predictions_and_has_no_factuality_claim():
    from ttakkari.collector.learning import fit, predict
    artifact = fit(samples())
    assert artifact["status"] == "shadow"
    assert artifact["quality_gate"] == "unverified"
    assert artifact["rubric"] == "owner_document_utility_v1"
    assert artifact["metrics"]["train_groups"] + artifact["metrics"]["validation_groups"] == 20
    positive = predict(artifact, "useful technical explanation transaction tutorial")
    negative = predict(artifact, "spam buy cheap advertisement now")
    assert positive > negative
    assert "factuality" not in artifact


def test_duplicate_content_cannot_cross_the_validation_boundary():
    from ttakkari.collector.learning import fit
    rows = samples()
    rows[1]["text"] = rows[0]["text"]
    with pytest.raises(ValueError, match="duplicate"):
        fit(rows)
