"""Source-first evaluation: strict denominators, paired comparisons and provenance."""

from __future__ import annotations

import math
import random
from collections import Counter
from statistics import mean


def validate_split(dev: list[dict], holdout: list[dict]) -> None:
    groups = {c["group"] for c in dev}
    if groups & {c["group"] for c in holdout}:
        raise ValueError("group_leakage")
    if {c["id"] for c in dev} & {c["id"] for c in holdout}:
        raise ValueError("query_id_leakage")


def score(cases: list[dict], predictions: dict[str, list[str]], *, k: int = 5) -> dict:
    if not cases or k < 1 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("invalid_evaluation_cases")
    rows = []
    for case in cases:
        if case["id"] not in predictions:
            raise ValueError("missing_prediction")
        relevance = {
            key: grade for key, grade in case["relevance"].items() if grade > 0
        }
        if not relevance:
            raise ValueError("empty_gold")
        if any(
            not isinstance(g, int) or not 0 <= g <= 3
            for g in case["relevance"].values()
        ):
            raise ValueError("invalid_relevance_grade")
        ranked = list(dict.fromkeys(predictions[case["id"]]))[:k]
        hits = [i + 1 for i, key in enumerate(ranked) if key in relevance]
        dcg = sum(
            (2 ** relevance.get(key, 0) - 1) / math.log2(i + 2)
            for i, key in enumerate(ranked)
        )
        ideal = sum(
            (2**g - 1) / math.log2(i + 2)
            for i, g in enumerate(sorted(relevance.values(), reverse=True)[:k])
        )
        rows.append(
            {
                "id": case["id"],
                "cohort": case.get("cohort", "unknown"),
                "relevant": len(relevance),
                "recall": len(hits) / len(relevance),
                "hit_rate": float(bool(hits)),
                "mrr": 1 / hits[0] if hits else 0,
                "ndcg": dcg / ideal,
            }
        )
    return {
        "queries": len(rows),
        "relevant_targets": sum(r["relevant"] for r in rows),
        "k": k,
        **{
            metric: mean(r[metric] for r in rows)
            for metric in ("recall", "hit_rate", "mrr", "ndcg")
        },
        "per_query": rows,
    }


def paired_interval(
    baseline: list[float],
    candidate: list[float],
    *,
    seed: int = 20261016,
    replicates: int = 2000,
) -> dict:
    if len(baseline) != len(candidate) or not baseline:
        raise ValueError("unpaired_results")
    delta = [b - a for a, b in zip(baseline, candidate, strict=True)]
    rng = random.Random(seed)
    samples = sorted(mean(rng.choices(delta, k=len(delta))) for _ in range(replicates))
    return {
        "pairs": len(delta),
        "delta": mean(delta),
        "low": samples[int(replicates * 0.025)],
        "high": samples[min(int(replicates * 0.975), replicates - 1)],
        "seed": seed,
        "replicates": replicates,
    }


def bm25(query: str, documents: dict[str, str], *, k: int = 10) -> list[str]:
    from ttakkari.context.search import terms

    words = terms(query)
    tokens = {key: _full_terms(text) for key, text in documents.items()}
    average = mean(len(t) for t in tokens.values()) if tokens else 1
    # Document frequency is corpus-level: compute it once per query, not once per document.
    counts = {key: Counter(doc) for key, doc in tokens.items()}
    dfs = {word: sum(word in freq for freq in counts.values()) for word in words}
    ranked = []
    for key, doc in tokens.items():
        frequencies = counts[key]
        value = 0.0
        for word in words:
            df = dfs[word]
            idf = math.log(1 + (len(tokens) - df + 0.5) / (df + 0.5))
            tf = frequencies[word]
            value += (
                idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(doc) / max(average, 1)))
            )
        if value:
            ranked.append((value, key))
    return [key for _, key in sorted(ranked, key=lambda row: (-row[0], row[1]))[:k]]


def _full_terms(text: str) -> list[str]:
    import re
    import unicodedata

    return re.findall(
        r"[\w]+(?:[-.][\w]+)*", unicodedata.normalize("NFKC", text).casefold()
    )


async def compare(
    db, cases: list[dict], *, scope: str = "all", k: int = 5, limit: int = 20000
) -> dict:
    """Compare within the current raw-unit corpus; no external export coverage claim."""
    from sqlalchemy import select

    from ttakkari.context import store as legacy
    from ttakkari.knowledge import store
    from ttakkari.knowledge_models import KnowledgeRevision as R
    from ttakkari.knowledge_models import KnowledgeUnit as U
    from ttakkari.models import CtxSource

    rows = (
        await db.execute(
            select(U, R)
            .join(R, R.id == U.revision_id)
            .outerjoin(CtxSource, CtxSource.id == R.source_id)
            .where(*store.visible(scope), U.active_branch.is_not(False))
            .order_by(U.id)
            .limit(limit + 1)
        )
    ).all()
    if len(rows) > limit:
        raise ValueError("corpus_cap_exceeded")
    documents: dict[str, str] = {}
    revisions = set()
    for u, r in rows:
        key = r.locator or r.source_key
        documents[key] = documents.get(key, "") + "\n" + u.body
        revisions.add((r.source_key, r.sha256))
    predictions = {
        mode: {}
        for mode in (
            "raw_literal",
            "bm25",
            "legacy",
            "knowledge",
            "graph_2hop",
            "hybrid_rrf",
        )
    }
    for case in cases:
        query = case["query"]
        literal = [
            key
            for key, body in documents.items()
            if query.casefold() in body.casefold()
        ]
        predictions["raw_literal"][case["id"]] = literal[:k]
        predictions["bm25"][case["id"]] = bm25(query, documents, k=k)
        old = await legacy.recall(db, query, k=k, scope=scope)
        predictions["legacy"][case["id"]] = [
            h.meta.get("path") or h.meta.get("source_ref") or h.id for h in old
        ]
        for mode, hops in [("knowledge", 1), ("graph_2hop", 2)]:
            result = await store.recall(db, query, scope=scope, k=k, hops=hops)
            paths = [e["locator"] for a in result["assertions"] for e in a["evidence"]]
            paths += [e["locator"] for e in result["evidence"]]
            predictions[mode][case["id"]] = list(dict.fromkeys(paths))[:k]
        fused = {}
        for method in ("legacy", "bm25", "graph_2hop"):
            for rank, path in enumerate(
                dict.fromkeys(predictions[method][case["id"]]), 1
            ):
                fused[path] = fused.get(path, 0) + 1 / (60 + rank)
        predictions["hybrid_rrf"][case["id"]] = sorted(
            fused, key=lambda path: (-fused[path], path)
        )[:k]
    metrics = {mode: score(cases, runs, k=k) for mode, runs in predictions.items()}
    base = metrics["legacy"]["per_query"]
    comparisons = {
        mode: paired_interval(
            [r["recall"] for r in base], [r["recall"] for r in value["per_query"]]
        )
        for mode, value in metrics.items()
        if mode != "legacy"
    }
    gold_paths = {p for c in cases for p, grade in c["relevance"].items() if grade > 0}
    return {
        "corpus_fingerprint": store.digest(repr(sorted(revisions))),
        "scope": scope,
        "corpus_documents": len(documents),
        "corpus_units": len(rows),
        "indexed_gold": len(gold_paths & documents.keys()),
        "gold_targets": len(gold_paths),
        "metrics": metrics,
        "paired_recall_vs_legacy": comparisons,
        "limitations": [
            "raw_literal covers current ingested source units, not an unimported export",
            "No automatic human-confirmed labels; dev and holdout must be declared",
            "BM25 uses Unicode tokens, not a Korean morphological analyzer",
        ],
        "predictions": predictions,
    }
