"""Stage-2 ML triage: candidate experiment, head training and external-label evaluation (C4).

Labels are never produced by the rules under test:
- education / problematic: FineWeb-C `kor_Hang` (community HUMAN annotators, ODC-By 1.0), one annotator per document.
- relevance: Mr. TyDi Korean train split (human TyDi QA answer judgements; negatives are BM25 hard negatives, so a
  small share may be unannotated positives). Used for evaluation only, never for fitting.
The in-repo fixture (unit_tests/fixtures/triage_eval_dataset.json) is a separate, hand-written regression set.

Usage (needs the e5/MiniLM ONNX files in --models-dir and network for the first fetch):
  PYTHONPATH=src ../back/.venv/bin/python scripts/eval_triage_ml.py --cache-dir /tmp/triage-ml-eval \
      --models-dir /tmp/c4/models --write-artifact
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from scipy.stats import rankdata, spearmanr

from ttakkari.collector import learning, triage, triage_ml

FWC_URL = (
    "https://datasets-server.huggingface.co/rows?dataset=data-is-better-together/fineweb-c"
    "&config=kor_Hang&split=train&offset={offset}&length=100"
)
MRT_URL = "https://huggingface.co/datasets/castorini/mr-tydi/resolve/main/mrtydi-v1.1-korean/train.jsonl.gz"
EDU_ORDER = {"None": 0, "Minimal": 1, "Basic": 2, "Good": 3, "Excellent": 4}
PROBLEMATIC = "❗ Problematic Content ❗"
MODELS = {
    "e5-large": ("intfloat/multilingual-e5-large", "query: ", "passage: "),
    "minilm-l12": ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "", ""),
}
SEED = 20261025
DOC_CHARS = triage_ml.DOC_CHARS


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def fetch(cache: Path) -> tuple[list[dict], list[dict]]:
    cache.mkdir(parents=True, exist_ok=True)
    fwc_path, mrt_path = cache / "fineweb_c_kor.json", cache / "mrtydi_ko_train.jsonl.gz"
    if not fwc_path.exists():
        rows = []
        for offset in range(0, 700, 100):
            rows += [r["row"] for r in json.loads(_get(FWC_URL.format(offset=offset)))["rows"]]
        fwc_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    if not mrt_path.exists():
        mrt_path.write_bytes(_get(MRT_URL))
    fwc = json.loads(fwc_path.read_text(encoding="utf-8"))
    with gzip.open(mrt_path, "rt", encoding="utf-8") as fh:
        mrt = [json.loads(line) for line in fh]
    return fwc, mrt


def embed(cache: Path, models_dir: str, key: str, texts: list[str], prefix: str) -> np.ndarray:
    name = MODELS[key][0]
    digest = hashlib.sha256(json.dumps([name, prefix, texts], ensure_ascii=False).encode()).hexdigest()[:16]
    path = cache / f"emb_{key}_{digest}.npy"
    if path.exists():
        return np.load(path)
    from fastembed import TextEmbedding

    model = TextEmbedding(name, cache_dir=models_dir, threads=4, local_files_only=True)
    out = np.asarray(list(model.embed([prefix + t for t in texts], batch_size=16)), dtype=np.float64)
    out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)
    np.save(path, out)
    return out


def auc(y: np.ndarray, score: np.ndarray) -> float:
    y = np.asarray(y).astype(bool)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = rankdata(score)
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def auc_ci(y: np.ndarray, score: np.ndarray, *, n: int = 2000) -> tuple[float, float]:
    rng = np.random.default_rng(SEED)
    y = np.asarray(y)
    values = []
    for _ in range(n):
        idx = rng.integers(0, len(y), len(y))
        if y[idx].min() == y[idx].max():
            continue
        values.append(auc(y[idx], score[idx]))
    return float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))


def ridge(x: np.ndarray, y: np.ndarray, lam: float) -> tuple[np.ndarray, float]:
    ym = float(y.mean())
    w = np.linalg.solve(x.T @ x + lam * np.eye(x.shape[1]), x.T @ (y - ym))
    return w, ym


def logistic(x: np.ndarray, y: np.ndarray, lam: float) -> tuple[np.ndarray, float]:
    n, d = x.shape
    xa = np.hstack([x, np.ones((n, 1))])
    w = np.zeros(d + 1)
    reg = lam * np.eye(d + 1)
    reg[d, d] = 1e-6
    for _ in range(40):
        p = 1 / (1 + np.exp(-np.clip(xa @ w, -30, 30)))
        grad = xa.T @ (p - y) + reg @ w
        hess = (xa * (p * (1 - p))[:, None]).T @ xa + reg
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.linalg.norm(step) < 1e-7:
            break
    return w[:d], float(w[d])


def cv_lambda(x: np.ndarray, y: np.ndarray, kind: str, grid=(1, 10, 100, 1000, 10000)) -> float:
    rng = np.random.default_rng(SEED)
    folds = rng.permutation(len(y)) % 5
    best, best_score = grid[0], -1.0
    for lam in grid:
        scores = np.zeros(len(y))
        for f in range(5):
            tr, va = folds != f, folds == f
            if kind == "ridge":
                w, b = ridge(x[tr], y[tr], lam)
                scores[va] = x[va] @ w + b
            else:
                w, b = logistic(x[tr], y[tr], lam)
                scores[va] = x[va] @ w + b
        value = auc(y >= 2, scores) if kind == "ridge" else auc(y, scores)
        if value > best_score:
            best, best_score = lam, value
    return best


def stratified_split(strata: list[str]) -> np.ndarray:
    """True = held-out test (30%), seeded and stratified by label class."""
    rng = random.Random(SEED)
    test = np.zeros(len(strata), dtype=bool)
    for cls in sorted(set(strata)):
        idx = [i for i, s in enumerate(strata) if s == cls]
        rng.shuffle(idx)
        for i in idx[: round(len(idx) * 0.3)]:
            test[i] = True
    return test


def prf(y: np.ndarray, pred: np.ndarray) -> dict:
    y, pred = np.asarray(y).astype(bool), np.asarray(pred).astype(bool)
    tp, fp, fn = int((y & pred).sum()), int((~y & pred).sum()), int((y & ~pred).sum())
    return {"tp": tp, "fp": fp, "fn": fn, "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn)}


def doc_text(row: dict) -> str:
    return row["text"][:DOC_CHARS]


def evaluate_heads(cache: Path, models_dir: str, fwc: list[dict]) -> dict:
    labels = [r["educational_value_labels"][0] for r in fwc]
    problematic = np.array([lab == PROBLEMATIC for lab in labels])
    edu = np.array([EDU_ORDER.get(lab, -1) for lab in labels])
    strata = ["problematic" if p else ("edu" if e >= 2 else f"edu{e}") for p, e in zip(problematic, edu, strict=True)]
    test = stratified_split(strata)
    texts = [doc_text(r) for r in fwc]
    report: dict = {
        "label_source": "FineWeb-C kor_Hang community human annotators (1 annotator/doc), ODC-By 1.0",
        "n_total": len(fwc), "n_train": int((~test).sum()), "n_test": int(test.sum()),
        "label_counts": {k: labels.count(k) for k in [*EDU_ORDER, PROBLEMATIC]},
        "split": f"seeded stratified 70/30 (seed {SEED}); test never used for lambda/threshold/fit",
        "models": {},
    }
    ok = ~problematic  # education is only defined for documents annotators did not flag as problematic
    artifacts: dict = {}
    for key in MODELS:
        emb = embed(cache, models_dir, key, texts, MODELS[key][2])
        mean, std = emb[~test].mean(0), emb[~test].std(0) + 1e-6
        z = (emb - mean) / std
        tr_edu, te_edu = (~test) & ok, test & ok
        lam_e = cv_lambda(z[tr_edu], edu[tr_edu].astype(float), "ridge")
        w_e, b_e = ridge(z[tr_edu], edu[tr_edu].astype(float), lam_e)
        s_e = z @ w_e + b_e
        lam_p = cv_lambda(z[~test], problematic[~test].astype(float), "logistic")
        w_p, b_p = logistic(z[~test], problematic[~test].astype(float), lam_p)
        s_p = z @ w_p + b_p
        # fixed operating points chosen on TRAIN only
        thr_e = float(np.percentile(s_e[tr_edu][edu[tr_edu] >= 2], 25))  # keeps ~75% of train Basic+ docs
        thr_p = float(np.percentile(s_p[(~test) & problematic], 25))
        y_edu = edu[te_edu] >= 2
        lo, hi = auc_ci(y_edu, s_e[te_edu])
        plo, phi = auc_ci(problematic[test], s_p[test])
        rho = float(spearmanr(s_e[te_edu], edu[te_edu]).statistic)
        report["models"][key] = {
            "edu": {"n_test": int(te_edu.sum()), "n_pos": int(y_edu.sum()), "lambda": lam_e, "auc": auc(y_edu, s_e[te_edu]),
                    "auc_ci95": [lo, hi], "spearman": rho, "threshold_train_p25": thr_e,
                    "at_threshold": prf(y_edu, s_e[te_edu] >= thr_e)},
            "problematic": {"n_test": int(test.sum()), "n_pos": int(problematic[test].sum()), "lambda": lam_p,
                            "auc": auc(problematic[test], s_p[test]), "auc_ci95": [plo, phi],
                            "threshold_train_p25": thr_p, "at_threshold": prf(problematic[test], s_p[test] >= thr_p)},
        }
        artifacts[key] = {
            "mean": mean, "std": std, "edu": (w_e, b_e, lam_e, thr_e), "problematic": (w_p, b_p, lam_p, thr_p),
            "test_scores": (s_e[te_edu], s_p[test]),
        }
    # baselines on the same split
    base: dict = {}
    # existing Ttakkari family: hashed char TF-IDF + logistic (learning.features/matrix), same fit/test rows
    raw = np.stack([learning.features(t) for t, m in zip(texts, test, strict=True) if not m])
    idf = np.log((1 + len(raw)) / (1 + np.count_nonzero(raw, axis=0))) + 1
    x_all = learning.matrix(texts, idf)
    tr, te_e = ~test, test & ok
    y_bin = (edu >= 2).astype(float)
    w, b = logistic(x_all[tr & ok], y_bin[tr & ok], 1.0)
    s = x_all @ w + b
    base["char_tfidf_logistic"] = {"edu_auc": auc(y_bin[te_e], s[te_e]),
                                   "edu_auc_ci95": list(auc_ci(y_bin[te_e], s[te_e]))}
    w, b = logistic(x_all[tr], problematic[tr].astype(float), 1.0)
    s = x_all @ w + b
    base["char_tfidf_logistic"]["problematic_auc"] = auc(problematic[test], s[test])
    base["char_tfidf_logistic"]["problematic_auc_ci95"] = list(auc_ci(problematic[test], s[test]))
    rule_q = np.array([triage.calculate_quality(t)[0] for t in texts])
    base["rule_quality_axis"] = {"edu_auc": auc(y_bin[te_e], rule_q[te_e]),
                                 "problematic_auc_inverse_quality": auc(problematic[test], -rule_q[test]),
                                 "note": "rule axis is a length/structure heuristic, not an educational-value model"}
    base["length_only"] = {"edu_auc": auc(y_bin[te_e], np.array([len(t) for t in texts])[te_e]),
                           "problematic_auc": auc(problematic[test], -np.array([len(t) for t in texts])[test])}
    report["baselines"] = base
    manifest = [{"id": r["id"], "label": lab, "split": "test" if t else "train",
                 "text_sha256": hashlib.sha256(r["text"].encode()).hexdigest()}
                for r, lab, t in zip(fwc, labels, test, strict=True)]
    report["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return {"report": report, "artifacts": artifacts, "manifest": manifest}


def evaluate_relevance(cache: Path, models_dir: str, mrt: list[dict]) -> dict:
    rng = random.Random(SEED)
    picked = rng.sample(mrt, 150)
    pairs = []  # (qi, text, label)
    for qi, row in enumerate(picked):
        pairs.append((qi, row["positive_passages"][0], 1))
        for neg in rng.sample(row["negative_passages"], 2):
            pairs.append((qi, neg, 0))
    queries = [r["query"] for r in picked]
    docs = [f"{p['title']}\n{p['text']}"[:DOC_CHARS] for _, p, _ in pairs]
    qid = np.array([q for q, _, _ in pairs])
    lab = np.array([y for _, _, y in pairs])
    calib_q = qid < 50  # thresholds come from 50 queries; every number below is on the other 100
    out: dict = {
        "label_source": "Mr. TyDi ko train: TyDi QA human answer judgements; negatives = BM25 hard negatives",
        "caveat": "queries are questions and passages are short Wikipedia paragraphs, not mission goals vs web pages",
        "n_queries_total": 150, "n_calibration_queries": 50, "n_test_queries": 100,
        "n_test_pairs": int((~calib_q).sum()), "scores": {},
    }

    def summarize(name: str, score: np.ndarray) -> dict:
        te = ~calib_q
        wins = [
            float(score[(qid == q) & (lab == 1)][0] > score[(qid == q) & (lab == 0)].max())
            for q in sorted(set(qid[te]))
        ]
        lo, hi = auc_ci(lab[te], score[te])
        return {"auc": auc(lab[te], score[te]), "auc_ci95": [lo, hi], "positive_beats_all_negatives": float(np.mean(wins))}

    lex = np.array([triage.calculate_relevance(queries[q], p["title"], p["text"])[0] for q, p, _ in pairs])
    out["scores"]["lexical_current"] = summarize("lexical", lex)
    out["scores"]["lexical_current"]["zero_overlap_share_of_positives"] = float(np.mean(lex[(lab == 1)] == 0))
    thresholds = {}
    for key in MODELS:
        q_emb = embed(cache, models_dir, key, queries, MODELS[key][1])
        d_emb = embed(cache, models_dir, key, docs, MODELS[key][2])
        cos = np.sum(q_emb[qid] * d_emb, axis=1)
        out["scores"][key] = summarize(key, cos)
        c_pos, c_neg = cos[calib_q & (lab == 1)], cos[calib_q & (lab == 0)]
        # band edges from calibration queries: below `low` is rarely relevant, above `high` is rarely a hard negative
        thresholds[key] = {"low": float(np.percentile(c_pos, 5)), "high": float(np.percentile(c_neg, 95))}
        te = ~calib_q
        out["scores"][key]["bands_on_test"] = {
            "positives_below_low": float(np.mean(cos[te & (lab == 1)] < thresholds[key]["low"])),
            "negatives_below_low": float(np.mean(cos[te & (lab == 0)] < thresholds[key]["low"])),
            "positives_above_high": float(np.mean(cos[te & (lab == 1)] > thresholds[key]["high"])),
            "negatives_above_high": float(np.mean(cos[te & (lab == 0)] > thresholds[key]["high"])),
        }
    out["thresholds_from_calibration_queries"] = thresholds
    return out


def measure_cost(models_dir: str) -> dict:
    """Cold child process (load + 1 goal + 1 document) and warm batch cost, through the real worker module."""
    import asyncio

    from ttakkari.model_process import run_json_worker

    doc = ("파이썬 비동기 프로그래밍은 asyncio 이벤트 루프를 기반으로 동작한다. " * 40)[:DOC_CHARS]

    async def one(n_docs: int) -> dict:
        res = await run_json_worker(
            module="ttakkari.context.embedder_worker",
            request={"texts": ["파이썬 비동기 입문", *[doc] * n_docs], "prefixes": ["query: ", *["passage: "] * n_docs],
                     "query": False, "model": MODELS["e5-large"][0], "cache_dir": models_dir, "threads": 2},
            timeout=300, error_prefix="embedding_worker",
        )
        return {"docs": n_docs, **res["metrics"], "snapshot": res.get("snapshot")}

    cold = [asyncio.run(one(n)) for n in (1, 1, 5, 5)]
    from fastembed import TextEmbedding

    model = TextEmbedding(MODELS["e5-large"][0], cache_dir=models_dir, threads=2, local_files_only=True)
    list(model.embed(["passage: warmup"]))
    started = time.perf_counter()
    list(model.embed(["passage: " + doc] * 8, batch_size=8))
    warm = (time.perf_counter() - started) / 8
    return {"cold_child_runs": cold, "warm_seconds_per_doc_threads2": warm, "doc_chars": len(doc),
            "machine": "Mac Studio (local CPU, 2 ONNX threads)"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default="/tmp/triage-ml-eval")
    ap.add_argument("--models-dir", required=True)
    ap.add_argument("--out", default="unit_tests/fixtures/triage_ml/eval_report.json")
    ap.add_argument("--write-artifact", action="store_true")
    ap.add_argument("--measure-cost", action="store_true")
    args = ap.parse_args()
    cache = Path(args.cache_dir)
    fwc, mrt = fetch(cache)
    heads = evaluate_heads(cache, args.models_dir, fwc)
    relevance = evaluate_relevance(cache, args.models_dir, mrt)
    report = {"heads": heads["report"], "relevance": relevance}
    if args.measure_cost:
        report["cost"] = measure_cost(args.models_dir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.write_artifact:
        key = "e5-large"
        a = heads["artifacts"][key]
        artifact = triage_ml.build_artifact(
            embedding_model=MODELS[key][0], snapshot=triage_ml.EXPECTED_SNAPSHOT,
            mean=a["mean"], std=a["std"],
            edu=a["edu"], problematic=a["problematic"],
            relevance_bands=relevance["thresholds_from_calibration_queries"][key],
            evaluation=report,
        )
        triage_ml.ARTIFACT_PATH.write_text(json.dumps(artifact, ensure_ascii=False, indent=1), encoding="utf-8")
        (out.parent / "fineweb_c_kor_manifest.json").write_text(
            json.dumps(heads["manifest"], ensure_ascii=False, indent=0), encoding="utf-8")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
