#!/usr/bin/env python3
"""Independent Evaluation Script for Collection Triage (C4).

Usage:
    PYTHONPATH=src python scripts/eval_collection_triage.py
"""

import sys
from pathlib import Path

# Add src and unit_tests to sys.path
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "src"))
sys.path.insert(0, str(root / "unit_tests"))

from test_collection_triage_eval import load_eval_dataset, run_triage_evaluation


def main():
    dataset = load_eval_dataset()
    metrics, results = run_triage_evaluation(dataset)

    print(
        "================================================================================"
    )
    print(
        "        따까리 수집 플랫폼 경량 ML 선별 독립 평가 (C4 Independent Evaluation)    "
    )
    print(
        "================================================================================"
    )
    print(
        f"평가 데이터셋: {metrics['total_samples']}건 (좋음: {metrics['good_count']}, 애매: {metrics['ambiguous_count']}, 나쁨: {metrics['bad_count']})"
    )
    print(
        "--------------------------------------------------------------------------------"
    )
    print(
        f" 1. 좋은 문서 오판 보류율 (False Negative Rate) : {metrics['false_negative_good_held_rate'] * 100:6.2f}% (목표 0%)"
    )
    print(
        f" 2. 좋은 문서 적재 재현율 (Good Document Recall): {metrics['good_recall_rate'] * 100:6.2f}%"
    )
    print(
        f" 3. 나쁜 문서 자동 보류율 (Bad Document Hold)  : {metrics['bad_hold_rate'] * 100:6.2f}%"
    )
    print(
        f" 4. 수집 적재 정밀도     (Ingest Precision)     : {metrics['ingest_precision'] * 100:6.2f}%"
    )
    print(
        f" 5. 애매 문서 검토필요율 (Review Coverage)       : {metrics['ambiguous_review_rate'] * 100:6.2f}%"
    )
    print(
        f" 6. 로컬 평균 처리 시간  (Average CPU Latency)   : {metrics['avg_latency_ms']:6.3f} ms / doc"
    )
    print(
        "--------------------------------------------------------------------------------"
    )
    print("상세 평가 샘플 목록 (일부):")
    for r in results:
        status_mark = (
            "OK"
            if (r["gold_label"] == "hold" and r["predicted_action"] == "hold")
            or (r["gold_label"] != "hold" and r["predicted_action"] == "accept")
            else "MISMATCH"
        )
        print(
            f" [{status_mark:8s}] {r['id']:6s} ({r['category']:18s}) -> gold:{r['gold_label']:6s} | pred:{r['predicted_action']:6s} ({r['triage_status']:13s}) | rel:{r['relevance_score']:.2f} qual:{r['quality_score']:.2f} nov:{r['novelty_score']:.2f} comp:{r['composite_score']:.2f}"
        )
    print(
        "================================================================================"
    )


if __name__ == "__main__":
    main()
