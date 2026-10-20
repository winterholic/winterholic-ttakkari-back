"""Bounded CPU retrieval evaluation. No model downloads or arbitrary code execution."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from pathlib import Path


def code_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def tokens(text: str) -> list[str]:
    return re.findall(r"[\w]+", text.casefold())


def evaluate(config: dict, seconds: int = 30) -> dict:
    corpus, queries = config.get("corpus", []), config.get("queries", [])
    k = config.get("k", 5)
    if (
        not isinstance(corpus, list)
        or not isinstance(queries, list)
        or any(not isinstance(d, dict) for d in corpus + queries)
    ):
        raise ValueError("invalid_evaluation_structure")
    if (
        not isinstance(k, int)
        or not 1 <= k <= 50
        or not 1 <= len(corpus) <= 500
        or not 1 <= len(queries) <= 50
    ):
        raise ValueError("evaluation_size_limit")
    if len(json.dumps(config, ensure_ascii=False)) > 200_000:
        raise ValueError("evaluation_payload_limit")
    if any(
        not isinstance(d.get("id"), str) or not isinstance(d.get("text"), str)
        for d in corpus
    ):
        raise ValueError("invalid_corpus")
    ids = [d["id"] for d in corpus]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_corpus_id")
    if any(
        not isinstance(q.get("query"), str)
        or not isinstance(q.get("relevant"), list)
        or not q["relevant"]
        or any(not isinstance(x, str) for x in q["relevant"])
        or not set(q["relevant"]) <= set(ids)
        for q in queries
    ):
        raise ValueError("invalid_qrels")
    started = time.monotonic()
    docs = [Counter(tokens(d["text"])) for d in corpus]
    lengths = [sum(d.values()) for d in docs]
    avg = sum(lengths) / len(docs) or 1
    df = Counter(t for doc in docs for t in doc)
    rows = []
    for q in queries:
        if time.monotonic() - started > seconds:
            raise TimeoutError("evaluation_deadline")
        terms = set(tokens(q["query"]))
        lexical = [sum(doc[t] > 0 for t in terms) for doc in docs]
        bm25 = [
            sum(
                math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                * doc[t]
                * 2.2
                / (doc[t] + 1.2 * (0.25 + 0.75 * length / avg))
                for t in terms
                if doc[t]
            )
            for doc, length in zip(docs, lengths, strict=True)
        ]
        row = {"query": q["query"], "relevant": q["relevant"]}
        for name, scores in [("lexical", lexical), ("bm25", bm25)]:
            ranked = sorted(range(len(ids)), key=lambda i: (-scores[i], ids[i]))
            hits = [ids[i] for i in ranked if scores[i] > 0][:k]
            row[name] = {
                "ids": hits,
                "recall": len(set(hits) & set(q["relevant"])) / len(set(q["relevant"])),
                "reciprocal_rank": next(
                    (1 / (i + 1) for i, doc in enumerate(hits) if doc in q["relevant"]),
                    0,
                ),
            }
        rows.append(row)
    return {
        "rows": rows,
        "k": k,
        "corpus_count": len(corpus),
        "query_count": len(queries),
        "means": {
            name: {
                metric: sum(r[name][metric] for r in rows) / len(rows)
                for metric in ("recall", "reciprocal_rank")
            }
            for name in ("lexical", "bm25")
        },
        "elapsed_seconds": time.monotonic() - started,
        "protocol": "lexical-overlap-vs-bm25-k1.2-b0.75-v1",
        "dataset_sha256": hashlib.sha256(
            json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "limitations": [
            "CPU retrieval only; generation quality not measured",
            "No statistical significance claim",
            "Caller supplied qrels; representativeness requires independent review",
        ],
    }
