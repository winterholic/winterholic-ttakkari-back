# ruff: noqa
"""llama-server /v1/embeddings 처리량. 실제 한국어·영어 문서 문단(back/docs)을 700자 이내로 잘라 쓴다."""
import json, time, glob, os, sys, urllib.request, concurrent.futures as cf

paras = []
for p in glob.glob(os.path.expanduser("~/development/projects/winterholic-ttakkari/back/docs/**/*.md"), recursive=True):
    for b in open(p, encoding="utf-8", errors="ignore").read().split("\n\n"):
        b = b.strip()
        if len(b) >= 200:
            paras.append(b[:700])
paras = paras[:2048]
url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:19411/v1/embeddings"

def call(batch):
    req = urllib.request.Request(url, data=json.dumps({"input": batch, "model": "x"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    return len(d["data"]), len(d["data"][0]["embedding"]), d.get("usage", {}).get("prompt_tokens")

# 단건 지연(질의 임베딩)
lat = []
for q in ["PostgreSQL 기본 격리 수준은?", "default isolation level in PostgreSQL", "광합성 명반응은 어디서 일어나나"] * 10:
    t = time.perf_counter(); call([q]); lat.append((time.perf_counter() - t) * 1000)
lat.sort()
# 처리량
B = 32
t = time.perf_counter(); toks = 0; n = 0
with cf.ThreadPoolExecutor(4) as ex:
    for cnt, dim, pt in ex.map(call, [paras[i:i + B] for i in range(0, len(paras), B)]):
        n += cnt; toks += pt or 0
el = time.perf_counter() - t
print(json.dumps({"passages": n, "dim": dim, "avg_chars": round(sum(map(len, paras)) / len(paras)),
                  "prompt_tokens": toks, "seconds": round(el, 1), "passages_per_s": round(n / el, 1),
                  "tokens_per_s": round(toks / el) if toks else None,
                  "query_p50_ms": round(lat[len(lat) // 2], 1), "query_p95_ms": round(lat[int(len(lat) * .95)], 1)}))
