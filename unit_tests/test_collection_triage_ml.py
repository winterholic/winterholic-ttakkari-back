"""Stage-2 ML triage: head math, validation gating, decision folding, failure isolation and the committed artifact.

Uses a 3-dimensional synthetic artifact and a fake embedder: no model files, no network. The committed e5 head
artifact is only checked for integrity and for agreement with the recorded external-label evaluation report.
"""

import asyncio
import json
from pathlib import Path

import pytest

from ttakkari.collector import pipeline, triage, triage_ml
from ttakkari.collector.triage import evaluate_triage

REPORT = Path(__file__).parent / "fixtures" / "triage_ml" / "eval_report.json"


def synthetic_artifact(*, rel="validated", edu="validated", prob="validated") -> dict:
    def block(status: str) -> dict:
        return {"status": status, "reason": "test", "metrics": {}, "label_source": "test"}

    return {
        "version": triage_ml.VERSION, "digest": "d" * 64,
        "embedding": {"model": "m", "snapshot": "snap", "mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]},
        "heads": {
            "information_value": {"kind": "ridge", "scale": "ordinal", "weights": [2.0, 0.0, 0.0], "bias": 1.0,
                                  "review_floor": 1.0, "validation": block(edu)},
            "low_quality_likelihood": {"kind": "logistic", "scale": "raw_logit", "weights": [0.0, 3.0, 0.0],
                                       "bias": -1.0, "review_at": 0.5, "validation": block(prob)},
        },
        "relevance": {"low": 0.2, "high": 0.7, "validation": block(rel)},
    }


def vec(*values: float) -> list[float]:
    return list(values)


def test_axes_are_separate_and_carry_validation_state():
    axes = triage_ml.score_document(synthetic_artifact(), vec(1, 0, 0), vec(0.6, 0.8, 0), language="ko", snapshot_ok=True)
    assert set(axes) == set(triage_ml.AXIS_NAMES)
    assert axes["topic_relevance_semantic"]["value"] == pytest.approx(0.6, abs=1e-3)
    assert axes["information_value"]["value"] == pytest.approx(2.2, abs=1e-2)  # 2*0.6+1
    assert axes["low_quality_likelihood"]["value"] == pytest.approx(1.4, abs=1e-2)  # 3*0.8-1
    assert axes["low_quality_likelihood"]["probability_uncalibrated"] == pytest.approx(0.8021, abs=1e-3)
    assert all(axes[n]["validation"] == "validated" for n in triage_ml.AXIS_NAMES[:3])
    # explanation quality has no labelled data: it must stay unmeasured and unvalidated
    assert axes["explanation_quality"]["status"] == "unmeasured"
    assert axes["explanation_quality"]["value"] is None


@pytest.mark.parametrize(
    ("language", "snapshot_ok", "needle"),
    [("en", True, "language_not_validated:en"), ("ko", False, "embedding_snapshot_mismatch")],
)
def test_axes_become_unvalidated_outside_the_labelled_language_or_snapshot(language, snapshot_ok, needle):
    axes = triage_ml.score_document(synthetic_artifact(), vec(1, 0, 0), vec(1, 0, 0), language=language,
                                    snapshot_ok=snapshot_ok)
    for name in triage_ml.AXIS_NAMES[:3]:
        assert axes[name]["validation"] == "unvalidated"
        assert needle in axes[name]["reason"]


def test_unvalidated_artifact_axis_is_not_validated_even_for_korean():
    axes = triage_ml.score_document(synthetic_artifact(rel="unvalidated"), vec(1, 0, 0), vec(1, 0, 0),
                                    language="ko", snapshot_ok=True)
    assert axes["topic_relevance_semantic"]["validation"] == "unvalidated"
    assert "artifact_unvalidated" in axes["topic_relevance_semantic"]["reason"]
    assert axes["information_value"]["validation"] == "validated"


def test_validation_bars_use_the_ci_lower_bound_and_positive_support():
    good = {"auc": 0.82, "auc_ci95": [0.7, 0.9], "n_test": 100, "n_pos": 30}
    assert triage_ml.validation(good, min_auc=0.75, min_lower=0.6, min_pos=15, label_source="x")["status"] == "validated"
    wide = {**good, "auc_ci95": [0.5, 0.95]}
    v = triage_ml.validation(wide, min_auc=0.75, min_lower=0.6, min_pos=15, label_source="x")
    assert v["status"] == "unvalidated" and "auc_ci95_lower" in v["reason"]
    few = {**good, "n_pos": 5}
    assert "positives 5" in triage_ml.validation(few, min_auc=0.75, min_lower=0.6, min_pos=15, label_source="x")["reason"]


def ml_with(axes_artifact: dict, goal_vec, doc_vec, *, language="ko") -> dict:
    return {
        "version": triage_ml.VERSION, "status": "evaluated", "reason": "", "artifact_digest": "d" * 64,
        "embedding": {"model": "m", "snapshot": "snap", "snapshot_ok": True},
        "axes": triage_ml.score_document(axes_artifact, goal_vec, doc_vec, language=language, snapshot_ok=True),
        "cost": {"batch_docs": 1},
    }


GOOD_TEXT = (
    "# PostgreSQL MVCC\n\nPostgreSQL MVCC 는 다중 버전 동시성 제어로 트랜잭션 격리 수준에 따라 스냅샷을 만든다. "
    "읽기 작업은 쓰기 작업을 막지 않고, 오래된 튜플은 vacuum 이 정리한다. " * 6
)


def triage_for(goal: str, title: str, text: str, ml: dict | None):
    return evaluate_triage(goal=goal, title=title, text=text, ml=ml)


HOLD_GOAL = "프로그래밍 언어 컴파일러 최적화"  # same script as GOOD_TEXT, zero lexical overlap
REVIEW_GOAL = "데이터베이스 색인 병합 정렬 컴파일러 최적화 튜플"  # 1 of 7 terms overlap: lexical review band


def test_validated_semantic_relevance_rescues_a_lexical_hold_into_review():
    goal, title = HOLD_GOAL, "PostgreSQL MVCC"
    rules = triage_for(goal, title, GOOD_TEXT, None)
    assert rules.action == "hold" and "triage:irrelevant_to_goal" in rules.reasons
    ml = ml_with(synthetic_artifact(), vec(1, 0, 0), vec(0.9, 0.1, 0))  # cosine ~0.99 > low
    out = triage_for(goal, title, GOOD_TEXT, ml)
    assert out.action == "accept" and out.triage_status == "review_needed"
    assert "review:semantic_overrides_lexical_hold" in out.reasons
    assert out.signals["precise_eval"]["state"] == "pending"
    assert "lexical_semantic_disagreement" in out.signals["precise_eval"]["reasons"]
    assert out.signals["stage2"]["effects"] == ["rescued_from_lexical_hold"]


def test_unvalidated_semantic_axis_never_changes_the_rule_decision():
    goal, title = HOLD_GOAL, "PostgreSQL MVCC"
    ml = ml_with(synthetic_artifact(rel="unvalidated"), vec(1, 0, 0), vec(1, 0, 0))
    out = triage_for(goal, title, GOOD_TEXT, ml)
    assert out.action == "hold" and out.triage_status == "auto_held"
    assert out.signals["validation"]["overall"] == "partial"  # info/problem heads are validated, relevance is not
    assert "topic_relevance_semantic" not in out.signals["validation"]["validated_axes"]
    assert out.signals["axes"]["topic_relevance_semantic"]["validation"] == "unvalidated"


def test_english_document_is_recorded_but_cannot_be_decided_by_ml():
    ml = ml_with(synthetic_artifact(), vec(1, 0, 0), vec(0, 1, 0), language="en")  # orthogonal: would flag as irrelevant
    text = "PostgreSQL MVCC isolation snapshot transaction guide. " * 12
    out = triage_for("PostgreSQL MVCC transaction", "MVCC guide", text, ml)
    assert out.triage_status == "auto_accepted"
    assert out.signals["validation"]["validated_axes"] == []
    assert out.signals["validation"]["overall"] == "unvalidated"


def test_validated_low_semantic_relevance_flags_review_not_hold():
    goal, title = "PostgreSQL MVCC 트랜잭션", "PostgreSQL MVCC 가이드"
    assert triage_for(goal, title, GOOD_TEXT, None).triage_status == "auto_accepted"
    ml = ml_with(synthetic_artifact(), vec(1, 0, 0), vec(0.05, 1, 0))  # cosine ~0.05 < low
    out = triage_for(goal, title, GOOD_TEXT, ml)
    assert out.action == "accept" and out.triage_status == "review_needed"
    assert "review:semantic_low_relevance" in out.reasons
    assert out.signals["precise_eval"]["needed"] is True


def test_validated_problematic_head_adds_review_but_never_hold():
    goal, title = "PostgreSQL MVCC 트랜잭션", "PostgreSQL MVCC 가이드"
    ml = ml_with(synthetic_artifact(), vec(1, 0, 0), vec(0.8, 0.6, 0))  # problematic logit 3*0.6-1=0.8 >= 0.5
    out = triage_for(goal, title, GOOD_TEXT, ml)
    assert out.action == "accept" and out.triage_status == "review_needed"
    assert "review:ml_low_quality_likelihood" in out.reasons
    assert "ml_flags_low_quality_but_rules_pass" in out.signals["precise_eval"]["reasons"]


def test_stage2_can_never_produce_a_hold_on_its_own():
    goal, title = "PostgreSQL MVCC 트랜잭션", "PostgreSQL MVCC 가이드"
    for doc in (vec(0, 1, 0), vec(0.05, 1, 0), vec(-1, 1, 0), vec(1, 1, 1)):
        out = triage_for(goal, title, GOOD_TEXT, ml_with(synthetic_artifact(), vec(1, 0, 0), doc))
        assert out.action == "accept"


def test_semantic_high_clears_lexical_low_relevance_when_nothing_else_is_wrong():
    goal = REVIEW_GOAL
    title, text = "문서", GOOD_TEXT
    rules = triage_for(goal, title, text, None)
    assert rules.triage_status == "review_needed" and "review:low_relevance" in rules.reasons
    ml = ml_with(synthetic_artifact(), vec(1, 0, 0), vec(1, 0.1, 0))  # cosine ~0.99 >= high, problematic low
    out = triage_for(goal, title, text, ml)
    assert out.triage_status == "auto_accepted"
    assert "relevance_confirmed_by_semantic" in out.reasons
    assert out.signals["precise_eval"]["needed"] is False


def test_unavailable_stage2_escalates_rule_review_to_pending_precise_eval():
    out = triage_for(REVIEW_GOAL, "문서", GOOD_TEXT, {"status": "unavailable", "reason": "embedder_error:RuntimeError", "axes": {}})
    assert out.triage_status == "review_needed"
    assert out.signals["precise_eval"]["reasons"] == ["stage2_unavailable_or_unvalidated"]
    assert out.signals["stage2"]["reason"] == "embedder_error:RuntimeError"


def test_old_and_short_or_ai_style_documents_get_no_ml_penalty_path():
    # None of the stage-2 axes may turn age, brevity or provenance into a quality failure by themselves.
    ml = ml_with(synthetic_artifact(), vec(1, 0, 0), vec(0.9, 0.2, 0))
    short = "2009년 PostgreSQL 8.4 MVCC 설명. 스냅샷 격리와 vacuum 요약. " * 4
    out = triage_for("PostgreSQL MVCC 격리", "PostgreSQL 8.4 MVCC", short, ml)
    assert out.action == "accept"


async def fake_embed_ok(texts, prefixes):
    assert prefixes[0] == "query: " and set(prefixes[1:]) == {"passage: "}
    return [[1.0, 0.0, 0.0], *[[0.9, 0.1, 0.0] for _ in texts[1:]]], {"snapshot": "snap", "load_seconds": 1.5}


def test_evaluate_documents_runs_one_batch_and_records_cost(monkeypatch):
    monkeypatch.setattr(triage_ml, "load_artifact", lambda: synthetic_artifact())
    docs = [{"title": "a", "text": "x" * 40, "language": "ko"}, {"title": "b", "text": "y" * 40, "language": "en"}]
    results = asyncio.run(triage_ml.evaluate_documents("목표", docs, embed=fake_embed_ok))
    assert [r["status"] for r in results] == ["evaluated", "evaluated"]
    assert results[0]["axes"]["information_value"]["validation"] == "validated"
    assert results[1]["axes"]["information_value"]["validation"] == "unvalidated"
    assert results[0]["cost"]["batch_docs"] == 2 and results[0]["embedding"]["snapshot_ok"] is True


def test_evaluate_documents_failure_and_missing_goal_are_isolated(monkeypatch):
    monkeypatch.setattr(triage_ml, "load_artifact", lambda: synthetic_artifact())

    async def boom(texts, prefixes):
        raise RuntimeError("embedding_worker_failed")

    docs = [{"title": "a", "text": "x" * 40, "language": "ko"}]
    failed = asyncio.run(triage_ml.evaluate_documents("목표", docs, embed=boom))
    assert failed[0]["status"] == "unavailable" and failed[0]["reason"] == "embedder_error:RuntimeError"
    nogoal = asyncio.run(triage_ml.evaluate_documents("  ", docs, embed=fake_embed_ok))
    assert nogoal[0]["status"] == "not_applicable"


def test_evaluate_documents_is_off_without_the_e5_embedder(monkeypatch):
    # the default path must not spawn a model child unless the configured embedder is the e5 model the heads need
    from types import SimpleNamespace

    monkeypatch.setattr("ttakkari.config.get_settings",
                        lambda: SimpleNamespace(collector_triage_ml_enabled=True, context_embedder="hash"))
    docs = [{"title": "a", "text": "x" * 40, "language": "ko"}]
    out = asyncio.run(triage_ml.evaluate_documents("목표", docs))
    assert out[0]["status"] == "disabled" and "context_embedder" in out[0]["reason"]


def test_missing_or_tampered_artifact_means_unavailable(monkeypatch, tmp_path):
    bad = tmp_path / "a.json"
    bad.write_text(json.dumps({**synthetic_artifact(), "digest": "0" * 64}))
    monkeypatch.setattr(triage_ml, "ARTIFACT_PATH", bad)
    triage_ml.reset_artifact_cache()
    try:
        assert triage_ml.load_artifact() is None
        out = asyncio.run(triage_ml.evaluate_documents(
            "목표", [{"title": "a", "text": "x" * 40, "language": "ko"}], embed=fake_embed_ok))
        assert out[0]["reason"] == "artifact_missing_or_digest_mismatch"
    finally:
        triage_ml.reset_artifact_cache()


def test_precise_queue_cap_marks_overflow_and_keeps_the_item():
    p = type("P", (), {})()
    p.signals = {"triage": {"precise_eval": {"needed": True, "state": "pending", "reasons": ["x"]}}}
    pipeline._admit_precise(p, 0)
    pe = p.signals["triage"]["precise_eval"]
    assert pe["state"] == "pending" and pe["engine"]["status"] == "unavailable"
    pipeline._admit_precise(p, pe["cap"])
    assert pe["state"] == "skipped_cap" and "precise_queue_cap_reached" in pe["reasons"]


def test_committed_artifact_is_intact_and_matches_the_recorded_evaluation():
    artifact = triage_ml.load_artifact()
    assert artifact is not None, "committed artifact missing or digest mismatch"
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert artifact["training"]["manifest_sha256"] == report["heads"]["manifest_sha256"]
    e5 = report["heads"]["models"]["e5-large"]
    for head, key in (("information_value", "edu"), ("low_quality_likelihood", "problematic")):
        v = artifact["heads"][head]["validation"]
        assert v["metrics"]["auc"] == e5[key]["auc"]
        assert v["status"] == triage_ml.validation(
            e5[key], min_auc=triage_ml.HEAD_MIN_AUC, min_lower=triage_ml.HEAD_MIN_AUC_LOWER,
            min_pos=triage_ml.HEAD_MIN_POSITIVES, label_source="")["status"]
    assert report["relevance"]["n_test_queries"] == 100
    assert "HUMAN" in report["heads"]["label_source"].upper() or "human" in report["heads"]["label_source"]
    assert triage.TRIAGE_VERSION == "multiaxis-triage-v2"
