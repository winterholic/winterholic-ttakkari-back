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
MIN_CALIBRATION_EXAMPLES = 20
MIN_CALIBRATION_PER_CLASS = 5
MIN_CALIBRATION_GROUPS = 5
PROVENANCE_FIELDS = ("review_id", "review_actor", "reviewed_at", "source_key",
                     "source_policy_hash", "source_policy_version", "license_id", "policy_url",
                     "train_permission")


def _manifest_row(row, split):
    result = {"id": row["id"], "group": row["group"], "label": row["label"],
              "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(), "split": split}
    result.update({key: row[key] for key in PROVENANCE_FIELDS if row.get(key) is not None})
    return result


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


def calibrated_probability(calibration, raw_score):
    if calibration is None:
        return None
    z = calibration["slope"] * ((float(raw_score) - calibration["raw_mean"]) / calibration["raw_scale"])
    z += calibration["intercept"]
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _fit_platt(raw_scores, labels, *, gold_hash):
    """Fit a binary utility calibrator only on a dedicated, source-heldout split."""
    y = np.asarray(labels, dtype=np.float64)
    raw = np.asarray(raw_scores, dtype=np.float64)
    if len(y) < MIN_CALIBRATION_EXAMPLES or min(np.count_nonzero(y), np.count_nonzero(1 - y)) < MIN_CALIBRATION_PER_CLASS:
        return None
    mean = float(np.mean(raw))
    scale = max(float(np.std(raw)), 1e-6)
    x = (raw - mean) / scale
    slope, intercept = 1.0, math.log(float(np.mean(y)) / float(1 - np.mean(y)))
    for _ in range(100):
        z = np.clip(slope * x + intercept, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        w = np.maximum(p * (1 - p), 1e-8)
        grad = np.asarray([np.sum((p - y) * x) + 1e-3 * slope, np.sum(p - y)])
        hessian = np.asarray([
            [np.sum(w * x * x) + 1e-3, np.sum(w * x)],
            [np.sum(w * x), np.sum(w)],
        ])
        step = np.linalg.solve(hessian, grad)
        slope -= float(step[0])
        intercept -= float(step[1])
        if float(np.linalg.norm(step)) < 1e-8:
            break
    result = {"method": "platt_logistic", "slope": float(slope), "intercept": float(intercept),
              "raw_mean": mean, "raw_scale": scale, "calibration_support": len(y),
              "positive_support": int(np.count_nonzero(y)), "negative_support": int(np.count_nonzero(1 - y)),
              "gold_hash": gold_hash}
    probs = np.asarray([calibrated_probability(result, score) for score in raw])
    result["brier"] = float(np.mean((probs - y) ** 2))
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
    group_n = len(groups)
    validation_n = max(1, round(group_n * 0.2))
    calibration_n = max(1, round(group_n * 0.2)) if group_n >= 10 else 0
    validation_groups = set(groups[:validation_n])
    calibration_groups = set(groups[validation_n:validation_n + calibration_n])
    train_groups = set(groups[validation_n + calibration_n:])
    if not train_groups:
        raise ValueError("not enough independent groups for train, validation and calibration")
    train = [r for r in samples if r["group"] in train_groups]
    validation = [r for r in samples if r["group"] in validation_groups]
    calibration_rows = [r for r in samples if r["group"] in calibration_groups]
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
    calibration = None
    if len(calibration_rows) >= MIN_CALIBRATION_EXAMPLES and len(calibration_groups) >= MIN_CALIBRATION_GROUPS:
        cx = matrix([r["text"] for r in calibration_rows], idf)
        cy = [r["label"] for r in calibration_rows]
        raw_scores = cx @ weights + bias
        gold_hash = hashlib.sha256(json.dumps(
            [_manifest_row(r, "calibration") for r in calibration_rows],
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        calibration = _fit_platt(raw_scores, cy, gold_hash=gold_hash)
        if calibration is not None:
            calibration["calibration_groups"] = sorted(calibration_groups)
    artifact = {"version": VERSION, "rubric": "owner_document_utility_v1", "status": "shadow",
                "preprocessing": {"max_chars": 20000, "coverage": "prefix", "ngrams": [2, 3, 4], "dimensions": DIM},
                "quality_gate": "unverified", "idf": idf.tolist(), "weights": weights.tolist(), "bias": bias,
                "calibration": calibration,
                "training_manifest": [_manifest_row(
                    r, "validation" if r["group"] in validation_groups else
                    "calibration" if r["group"] in calibration_groups else "train")
                                      for r in samples],
                "metrics": {"train_count": len(train), "validation_count": len(validation),
                            "calibration_count": len(calibration_rows), "train_groups": len(train_groups),
                            "validation_groups": len(validation_groups), "calibration_groups": len(calibration_groups),
                            "split_groups": {"train": sorted(train_groups), "validation": sorted(validation_groups),
                                             "calibration": sorted(calibration_groups)},
                            "model": confusion(vy, vx @ weights + bias >= 0),
                            "majority_baseline": confusion(vy, [majority] * len(vy))}}
    artifact["digest"] = hashlib.sha256(json.dumps(artifact, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return artifact
