"""Graded passage metrics. Unjudged hits never become negative gold labels."""

import math
from statistics import mean


def _rate(errors, denominator):
    return {
        "errors": errors,
        "denominator": denominator,
        "rate": errors / denominator if denominator else None,
    }


def metrics(cases, predictions, *, k=10):
    if not cases or k < 1 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("invalid_evaluation_cases")
    rows = []
    for c in cases:
        if any(type(g) is not int or not 0 <= g <= 3 for g in c["relevance"].values()):
            raise ValueError("invalid_relevance_grade")
        p = predictions.get(c["id"])
        failure = c.get("anchor_status", "valid")
        failure = None if failure == "valid" else failure
        if p is None:
            failure = failure or "missing_prediction"
        elif p.get("failure"):
            failure = failure or p["failure"]
        row = {
            "id": c["id"],
            "group": c["group"],
            "answerable": c["answerable"],
            "failure": failure,
            "recall": None,
            "ndcg": None,
            "mrr": None,
            "candidate_recall": None,
            "judged_at_k": None,
            "coverage": p.get("coverage", "unknown") if p else "unknown",
            "answer_correct": p.get("answer_correct") if p else None,
            "classification": classify_failure(c, p, k=k)
            if p
            else "missing_prediction",
        }
        if not failure:
            ranked = p["ranked"][:k]
            candidates = p.get("candidates", [])
            if len(set(ranked)) != len(ranked) or len(set(candidates)) != len(
                candidates
            ):
                raise ValueError("duplicate_prediction")
            relevant = {
                key: grade for key, grade in c["relevance"].items() if grade > 0
            }
            if c["answerable"] and not relevant:
                raise ValueError("empty_answerable_gold")
            if not c["answerable"] and relevant:
                raise ValueError("no_answer_positive_gold")
            row["judged_at_k"] = (
                sum(key in c["relevance"] for key in ranked) / len(ranked)
                if ranked
                else None
            )
            if relevant:
                hits = [i + 1 for i, key in enumerate(ranked) if key in relevant]
                dcg = sum(
                    (2 ** relevant.get(key, 0) - 1) / math.log2(i + 2)
                    for i, key in enumerate(ranked)
                )
                ideal = sum(
                    (2**g - 1) / math.log2(i + 2)
                    for i, g in enumerate(sorted(relevant.values(), reverse=True)[:k])
                )
                row.update(
                    recall=len(hits) / len(relevant),
                    mrr=1 / hits[0] if hits else 0.0,
                    ndcg=dcg / ideal,
                    candidate_recall=len(set(candidates) & relevant.keys())
                    / len(relevant),
                )
        rows.append(row)
    valid = [r for r in rows if not r["failure"]]
    answered = [r for r in valid if r["answerable"]]
    negatives = [
        r for r in valid if not r["answerable"] or r["answer_correct"] is False
    ]
    no_answer = [r for r in valid if not r["answerable"]]
    true_none = sum(r["coverage"] == "none" for r in no_answer)
    predicted_none = sum(r["coverage"] == "none" for r in valid)
    all_answerable = sum(r["answerable"] for r in rows)
    unknown_answerable = all_answerable - len(answered)
    recall_sum = sum(r["recall"] for r in answered)
    slices = {}
    for dimension in ("domain", "types", "language_pair"):
        bins = {}
        for c, row in zip(cases, rows, strict=True):
            tags = c.get(dimension, "unknown")
            for tag in tags if isinstance(tags, list) else [tags]:
                bins.setdefault(tag, []).append(row)
        slices[dimension] = {
            name: {
                "false_abstain": _rate(
                    sum(
                        r["coverage"] == "none"
                        for r in rs
                        if not r["failure"] and r["answerable"]
                    ),
                    sum(not r["failure"] and r["answerable"] for r in rs),
                ),
                "false_strong": _rate(
                    sum(
                        r["coverage"] == "strong"
                        for r in rs
                        if not r["failure"]
                        and (not r["answerable"] or r["answer_correct"] is False)
                    ),
                    sum(
                        not r["failure"]
                        and (not r["answerable"] or r["answer_correct"] is False)
                        for r in rs
                    ),
                ),
                "queries": len(rs),
                "invalid_or_failed": sum(bool(r["failure"]) for r in rs),
                **{
                    metric: mean(v)
                    if (v := [r[metric] for r in rs if r[metric] is not None])
                    else None
                    for metric in ("recall", "ndcg", "mrr")
                },
            }
            for name, rs in bins.items()
        }
    return {
        "metric_unit": "native_passage",
        "k": k,
        "total": len(rows),
        "invalid": sum(
            r["failure"] in {"gold_invalid", "anchor_ambiguous"} for r in rows
        ),
        "failed": sum(
            bool(r["failure"])
            and r["failure"] not in {"gold_invalid", "anchor_ambiguous"}
            for r in rows
        ),
        "valid_answerable": len(answered),
        "no_answer_denominator": len(no_answer),
        "false_strong_unknown_correctness": sum(
            r["answerable"] and r["answer_correct"] is None for r in valid
        ),
        **{
            m: mean(r[m] for r in answered) if answered else None
            for m in ("recall", "ndcg", "mrr", "candidate_recall")
        },
        "recall_all_answerable_bounds": [
            recall_sum / all_answerable,
            (recall_sum + unknown_answerable) / all_answerable,
        ]
        if all_answerable
        else None,
        "false_strong": _rate(
            sum(r["coverage"] == "strong" for r in negatives), len(negatives)
        ),
        "false_abstain": _rate(
            sum(r["coverage"] == "none" for r in answered), len(answered)
        ),
        "no_answer_none_recall": _rate(true_none, len(no_answer)),
        "abstain_f1": 2 * true_none / (len(no_answer) + predicted_none)
        if len(no_answer) + predicted_none
        else None,
        "abstain_intervals": _abstain_intervals(rows),
        "abstain_gate": "inconclusive",
        "per_query": rows,
        "slices": slices,
        "limitations": [
            "Unjudged hits are reported; numeric DCG credits only judged positive labels.",
            "No semantic evidence accuracy or answer absence is inferred.",
        ],
    }


def answer_recall(answer_sets, ranked, *, k):
    if (
        k < 1
        or not answer_sets
        or any(
            not options or any(not option for option in options)
            for options in answer_sets.values()
        )
    ):
        raise ValueError("invalid_answer_sets")
    top = set(ranked[:k])
    hits = sum(
        any(set(option) <= top for option in options)
        for options in answer_sets.values()
    )
    return {
        "metric_unit": "answer",
        "hits": hits,
        "denominator": len(answer_sets),
        "recall": hits / len(answer_sets),
    }


def classify_failure(case, prediction, *, k=10):
    """Stage labels require observed trace; lack of telemetry is never a miss."""
    if prediction.get("failure"):
        return prediction["failure"]
    relevant = {key for key, grade in case["relevance"].items() if grade > 0}
    trace = prediction.get("trace") or {}
    if trace.get("filtered"):
        return "filtered_out"
    if trace.get("stale"):
        return "index_stale"
    if trace.get("calibration_sufficient") is False:
        return "calibration_insufficient"
    if not relevant:
        return "false_strong" if prediction.get("coverage") == "strong" else None
    if prediction.get("coverage") == "none":
        return "false_abstain"
    if (
        prediction.get("coverage") == "strong"
        and prediction.get("answer_correct") is False
    ):
        return "false_strong"
    if trace.get("condition_correct") is False:
        return "wrong_condition"
    if set(prediction["ranked"][:k]) & relevant:
        return None
    if trace.get("indexed") is not None and not relevant & set(trace["indexed"]):
        return "not_in_corpus"
    if trace.get("routes") is not None and not any(
        relevant & set(route) for route in trace["routes"].values()
    ):
        return "no_candidate"
    if trace.get("fused") is not None:
        return "ranked_out" if relevant & set(trace["fused"]) else "cut_by_fusion"
    return "trace_unverified"


def _abstain_intervals(rows):
    from .statistics import binomial_interval, cluster_interval

    result = {
        "gate": "inconclusive",
        "reasons": ["power_not_established"],
        "binomial_assumption": "independent_questions_not_established_for_clustered_corpus",
    }
    for name in ("false_strong", "false_abstain"):
        eligible = [
            r
            for r in rows
            if not r["failure"]
            and (
                (not r["answerable"] or r["answer_correct"] is False)
                if name == "false_strong"
                else r["answerable"]
            )
        ]
        flags = [
            int(r["coverage"] == ("strong" if name == "false_strong" else "none"))
            for r in eligible
        ]
        groups = [r["group"] for r in eligible]
        result[name] = {
            "errors": sum(flags),
            "denominator": len(flags),
            "binomial_one_sided": binomial_interval(sum(flags), len(flags))
            if flags
            else None,
            "cluster": cluster_interval(flags, groups, seed=20261021)
            if len(set(groups)) >= 2
            else None,
        }
    return result
