"""Small, deterministic TF-IDF/logistic utility baseline. Shadow only; no factuality inference.

Training consumes explicitly reviewed, training-permitted data through the control API. Sources
are held out before fitting IDF. The development validation split is not a frozen human holdout.
"""
import hashlib
import json
import math
import re
from collections import Counter

import numpy as np

DIM = 1024
VERSION = "hashed-char-tfidf-logistic-v1"


def features(text):
    value = re.sub(r"\s+", " ", text.casefold())[:20000]
    counts = Counter()
    for n in (2, 3, 4):
        for i in range(max(0, len(value) - n + 1)):
            digest = hashlib.blake2b(value[i:i + n].encode(), digest_size=4).digest()
            counts[int.from_bytes(digest, "big") % DIM] += 1
    x = np.zeros(DIM, dtype=np.float64)
    for i, count in counts.items():
        x[i] = 1 + math.log(count)
    return x


def matrix(texts, idf):
    rows = np.stack([features(t) for t in texts]) * idf
    lengths = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(lengths, 1e-12)


def predict(artifact, text):
    x = matrix([text], np.asarray(artifact["idf"]))[0]
    return float(x @ np.asarray(artifact["weights"]) + artifact["bias"])


def confusion(y, predictions):
    result = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for expected, got in zip(y, predictions, strict=True):
        key = "tp" if expected and got else "fn" if expected else "fp" if got else "tn"
        result[key] += 1
    result["precision"] = result["tp"] / max(1, result["tp"] + result["fp"])
    result["recall"] = result["tp"] / max(1, result["tp"] + result["fn"])
    return result


def fit(samples):
    if len(samples) < 20 or len(samples) > 2000:
        raise ValueError("need 20..2000 reviewed, training-permitted examples")
    if {row["label"] for row in samples} != {0, 1}:
        raise ValueError("both utility classes are required")
    groups = sorted({row["group"] for row in samples}, key=lambda s: hashlib.sha256(s.encode()).hexdigest())
    if len(groups) < 5:
        raise ValueError("at least five independent source groups are required")
    seen = set()
    for row in samples:
        digest = hashlib.sha256(re.sub(r"\s+", " ", row["text"].casefold()).strip().encode()).hexdigest()
        if digest in seen:
            raise ValueError("duplicate content must be resolved before splitting")
        seen.add(digest)
    validation_groups = set(groups[:max(1, len(groups) // 4)])
    train = [r for r in samples if r["group"] not in validation_groups]
    validation = [r for r in samples if r["group"] in validation_groups]
    if any({row["label"] for row in split} != {0, 1} for split in (train, validation)):
        raise ValueError("both classes must occur in training and source-heldout validation")
    raw = np.stack([features(r["text"]) for r in train])
    idf = np.log((1 + len(train)) / (1 + np.count_nonzero(raw, axis=0))) + 1
    x = matrix([r["text"] for r in train], idf)
    y = np.asarray([r["label"] for r in train], dtype=np.float64)
    weights, bias = np.zeros(DIM), 0.0
    for _ in range(200):
        logits = np.clip(x @ weights + bias, -30, 30)
        error = 1 / (1 + np.exp(-logits)) - y
        weights -= 0.5 * (x.T @ error / len(y) + 0.01 * weights)
        bias -= 0.5 * float(np.mean(error))
    vx = matrix([r["text"] for r in validation], idf)
    vy = [r["label"] for r in validation]
    majority = int(float(np.mean(y)) >= 0.5)
    artifact = {"version": VERSION, "rubric": "owner_document_utility_v1", "status": "shadow",
                "preprocessing": {"max_chars": 20000, "coverage": "prefix", "ngrams": [2, 3, 4], "dimensions": DIM},
                "quality_gate": "unverified", "idf": idf.tolist(), "weights": weights.tolist(), "bias": bias,
                "training_manifest": [{"id": r["id"], "group": r["group"], "label": r["label"],
                                       "text_sha256": hashlib.sha256(r["text"].encode()).hexdigest(),
                                       "split": "validation" if r["group"] in validation_groups else "train"}
                                      for r in samples],
                "metrics": {"train_count": len(train), "validation_count": len(validation),
                            "train_groups": len(groups) - len(validation_groups), "validation_groups": len(validation_groups),
                            "model": confusion(vy, vx @ weights + bias >= 0),
                            "majority_baseline": confusion(vy, [majority] * len(vy))}}
    artifact["digest"] = hashlib.sha256(json.dumps(artifact, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return artifact
