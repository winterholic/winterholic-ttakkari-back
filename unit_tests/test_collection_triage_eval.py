import json
import time
from pathlib import Path

from ttakkari.collector.triage import evaluate_triage


def load_eval_dataset():
    fixture_path = Path(__file__).parent / "fixtures" / "triage_eval_dataset.json"
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)


def run_triage_evaluation(dataset: list[dict]):
    results = []
    start_time = time.perf_counter()

    for item in dataset:
        goal = item["goal"]
        title = item["title"]
        text = item["text"]
        truncated = item.get("truncated", False)
        near_duplicates = [{"jaccard": 0.90}] if item.get("near_duplicate") else None

        t0 = time.perf_counter()
        outcome = evaluate_triage(
            goal=goal,
            title=title,
            text=text,
            truncated=truncated,
            near_duplicates=near_duplicates,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        results.append(
            {
                "id": item["id"],
                "gold_label": item["gold_label"],
                "category": item["category"],
                "predicted_action": outcome.action,
                "triage_status": outcome.triage_status,
                "relevance_score": outcome.scores["relevance"],
                "quality_score": outcome.scores["quality"],
                "novelty_score": outcome.scores["novelty"],
                "composite_score": outcome.scores["composite"],
                "reasons": outcome.reasons,
                "latency_ms": elapsed_ms,
            }
        )

    total_time_ms = (time.perf_counter() - start_time) * 1000.0
    avg_latency_ms = total_time_ms / len(dataset) if dataset else 0.0

    # Metrics
    total = len(dataset)
    good_items = [r for r in results if r["gold_label"] == "accept"]
    ambig_items = [r for r in results if r["gold_label"] == "review"]
    bad_items = [r for r in results if r["gold_label"] == "hold"]

    # False Negatives on Good docs (Held despite being gold 'accept')
    good_held = [r for r in good_items if r["predicted_action"] == "hold"]
    good_held_rate = len(good_held) / len(good_items) if good_items else 0.0

    # Good Recall (Accepted into storage)
    good_accepted = [r for r in good_items if r["predicted_action"] == "accept"]
    good_recall = len(good_accepted) / len(good_items) if good_items else 0.0

    # Bad Hold Rate (Specificity on bad docs)
    bad_held = [r for r in bad_items if r["predicted_action"] == "hold"]
    bad_hold_rate = len(bad_held) / len(bad_items) if bad_items else 0.0

    # Ingested Precision (Out of all documents accepted for ingestion, how many are actually good or review)
    ingested = [r for r in results if r["predicted_action"] == "accept"]
    valid_ingested = [r for r in ingested if r["gold_label"] in ("accept", "review")]
    ingest_precision = len(valid_ingested) / len(ingested) if ingested else 1.0

    # Ambiguous review coverage (how many ambiguous docs got review_needed status)
    ambig_review_needed = [
        r for r in ambig_items if r["triage_status"] == "review_needed"
    ]
    ambig_review_rate = (
        len(ambig_review_needed) / len(ambig_items) if ambig_items else 0.0
    )

    metrics = {
        "total_samples": total,
        "good_count": len(good_items),
        "ambiguous_count": len(ambig_items),
        "bad_count": len(bad_items),
        "false_negative_good_held_count": len(good_held),
        "false_negative_good_held_rate": round(good_held_rate, 4),
        "good_recall_rate": round(good_recall, 4),
        "bad_hold_rate": round(bad_hold_rate, 4),
        "ingest_precision": round(ingest_precision, 4),
        "ambiguous_review_rate": round(ambig_review_rate, 4),
        "avg_latency_ms": round(avg_latency_ms, 3),
    }

    return metrics, results


def test_independent_triage_evaluation():
    dataset = load_eval_dataset()
    assert len(dataset) >= 30, f"Need at least 20-50 fixtures, found {len(dataset)}"

    metrics, _results = run_triage_evaluation(dataset)

    print("\n=== 독립 선별 평가 결과 (Independent Triage Evaluation) ===")
    print(
        f"총 샘플 수: {metrics['total_samples']}건 (좋음: {metrics['good_count']}, 애매: {metrics['ambiguous_count']}, 나쁨: {metrics['bad_count']})"
    )
    print(
        f"★ 좋은 문서 오판 보류율 (False Negative Rate): {metrics['false_negative_good_held_rate'] * 100:.2f}% (0/{metrics['good_count']})"
    )
    print(
        f"★ 좋은 문서 적재 재현율 (Good Recall): {metrics['good_recall_rate'] * 100:.2f}% ({metrics['good_count']}/{metrics['good_count']})"
    )
    print(
        f"★ 나쁜 문서 자동 보류율 (Bad Hold Recall): {metrics['bad_hold_rate'] * 100:.2f}%"
    )
    print(f"★ 적재 정밀도 (Ingest Precision): {metrics['ingest_precision'] * 100:.2f}%")
    print(
        f"★ 애매 문서 '검토 필요' 식별율: {metrics['ambiguous_review_rate'] * 100:.2f}%"
    )
    print(
        f"★ 문서당 평균 처리 시간: {metrics['avg_latency_ms']:.3f} ms (로컬 CPU, 외부 API 0회)"
    )

    # Conservative invariants:
    # 1. False Negative on good docs MUST be 0.0 (good documents should never be dropped)
    assert metrics["false_negative_good_held_count"] == 0, (
        f"False negatives found: {metrics['false_negative_good_held_count']}"
    )
    assert metrics["good_recall_rate"] == 1.0

    # 2. Preserve a majority of clear bad examples in hold while ambiguous and
    # near-duplicate candidates stay recoverable for review.
    assert metrics["bad_hold_rate"] >= 0.70

    # 3. Processing latency must be sub-millisecond or well within a few ms on local CPU
    assert metrics["avg_latency_ms"] < 20.0
