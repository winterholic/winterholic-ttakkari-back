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
    assert (artifact["metrics"]["train_groups"] + artifact["metrics"]["validation_groups"]
            + artifact["metrics"]["calibration_groups"]) == 20
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


def test_calibration_uses_source_heldout_groups_and_only_emits_probability_with_enough_support():
    from ttakkari.collector.learning import fit

    artifact = fit(samples())
    assert artifact["metrics"]["calibration_count"] == 8
    assert artifact["calibration"] is None

    rows = [{"text": ("useful technical reference " if i % 2 else "spam cheap purchase ad ") + str(i),
             "label": i % 2, "group": "source-" + str(i // 2), "id": str(i)}
            for i in range(120)]
    calibrated = fit(rows)
    split_groups = calibrated["metrics"]["split_groups"]
    assert set(split_groups["train"]).isdisjoint(split_groups["validation"])
    assert set(split_groups["train"]).isdisjoint(split_groups["calibration"])
    assert set(split_groups["validation"]).isdisjoint(split_groups["calibration"])
    assert calibrated["calibration"]["method"] == "platt_logistic"
    assert calibrated["calibration"]["calibration_support"] >= 20
    assert calibrated["calibration"]["gold_hash"]


def test_shadow_scorer_returns_calibrated_utility_probability_with_its_gold_hash():
    import asyncio
    from types import SimpleNamespace

    from ttakkari.collector import quality_service
    from ttakkari.collector.learning import fit
    from ttakkari.collector.quality import get_scorer

    rows = [{"text": ("useful technical reference " if i % 2 else "spam cheap purchase ad ") + str(i),
             "label": i % 2, "group": "source-" + str(i // 2), "id": str(i)}
            for i in range(120)]
    artifact = fit(rows)
    model = SimpleNamespace(tenant_id="owner", scope="personal", digest=artifact["digest"], artifact=artifact)
    quality_service.install(model)
    scorer = get_scorer(quality_service.scorer_name("owner", "personal", artifact["digest"]))
    scores = asyncio.run(scorer.score("useful technical reference", "en"))
    assert 0 <= scores["utility"].calibrated_probability <= 1
    assert scores["utility"].calibration_gold_hash == artifact["calibration"]["gold_hash"]
    assert scores["utility"].scale == "raw_logit"


def test_training_and_calibration_manifests_preserve_label_and_rights_provenance():
    from ttakkari.collector.learning import fit

    rows = [{**row, "review_id": f"review-{i}", "review_actor": "owner",
             "reviewed_at": "2026-10-23T00:00:00+00:00", "source_key": f"source-{i // 2}",
             "source_policy_hash": f"policy-{i // 2}", "license_id": "CC-BY-4.0"}
            for i, row in enumerate(samples())]
    artifact = fit(rows)
    entry = artifact["training_manifest"][0]
    assert entry["review_id"].startswith("review-")
    assert entry["review_actor"] == "owner"
    assert entry["source_policy_hash"].startswith("policy-")
    assert entry["license_id"] == "CC-BY-4.0"
    if artifact["calibration"]:
        assert artifact["calibration"]["gold_hash"]
