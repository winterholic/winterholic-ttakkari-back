# ruff: noqa
"""실제 bge-m3 임베딩(문서 문단 1,813개)으로 이진 양자화 후보(해밍 top-C) + 원 벡터 재채점의 top-10 일치율."""
import json, glob, os, urllib.request
import numpy as np

paras = []
for p in glob.glob(os.path.expanduser("~/development/projects/winterholic-ttakkari/back/docs/**/*.md"), recursive=True):
    paras += [b.strip()[:700] for b in open(p, encoding="utf-8", errors="ignore").read().split("\n\n") if len(b.strip()) >= 200]
paras = paras[:2048]
def emb(xs):
    out = []
    for i in range(0, len(xs), 64):
        r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:19411/v1/embeddings",
              json.dumps({"input": xs[i:i + 64]}).encode(), {"Content-Type": "application/json"}), timeout=600)
        out += [d["embedding"] for d in json.load(r)["data"]]
    return np.array(out, dtype=np.float32)
X = emb(paras); X /= np.linalg.norm(X, axis=1, keepdims=True)
rng = np.random.default_rng(1); qi = rng.choice(len(X), 200, replace=False)
B = X > 0
res = {}
for C in (40, 100, 200):
    hits = []
    for i in qi:
        q = X[i]; exact = np.argsort(-(X @ q))[1:11]
        ham = (B != B[i]).sum(1); cand = np.argsort(ham)[:C + 1]
        cand = cand[cand != i]; resc = cand[np.argsort(-(X[cand] @ q))][:10]
        hits.append(len(set(exact) & set(resc)) / 10)
    res[f"overlap@10_C{C}"] = round(float(np.mean(hits)), 3)
print(json.dumps({"n": len(X), **res}))
