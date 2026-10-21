# ruff: noqa
"""bge-reranker-v2-m3 /v1/rerank: 질의 1건 × 문서 N건 지연과 짝 스모크 정렬."""
import json, time, glob, os, urllib.request
from smoke_xling import pairs  # noqa: E402  (임베딩 서버도 함께 돈다)

url = "http://127.0.0.1:19412/v1/rerank"
def rerank(q, docs):
    r = urllib.request.urlopen(urllib.request.Request(url, json.dumps({"query": q, "documents": docs}).encode(),
                                                      {"Content-Type": "application/json"}), timeout=600)
    return json.load(r)["results"]

docs = [p for _, p in pairs]
top1 = 0; gap = []
for i, (q, _) in enumerate(pairs):
    res = sorted(rerank(q, docs), key=lambda x: -x["relevance_score"])
    top1 += int(res[0]["index"] == i)
    gap.append(res[0]["relevance_score"] - res[1]["relevance_score"])
fill = []
for p in glob.glob(os.path.expanduser("~/development/projects/winterholic-ttakkari/back/docs/**/*.md"), recursive=True):
    for b in open(p, encoding="utf-8", errors="ignore").read().split("\n\n"):
        if len(b.strip()) >= 300: fill.append(b.strip()[:700])
out = {"smoke_top1": f"{top1}/{len(pairs)}", "min_gap": round(min(gap), 3)}
for n in (20, 50, 100):
    lat = []
    for k in range(5):
        t = time.perf_counter(); rerank("PostgreSQL 기본 격리 수준", fill[k * n:(k + 1) * n]); lat.append((time.perf_counter() - t) * 1000)
    lat.sort(); out[f"n{n}_median_ms"] = round(lat[2], 0); out[f"n{n}_max_ms"] = round(lat[-1], 0)
print(json.dumps(out))
