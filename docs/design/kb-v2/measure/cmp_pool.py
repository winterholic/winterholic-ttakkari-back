# ruff: noqa
"""GPU 서버(--pooling cls 지정)와 CPU 서버(모델 기본 풀링)의 같은 입력 임베딩 코사인, CPU 처리량."""
import json, time, glob, os, urllib.request, math, concurrent.futures as cf
def emb(port, xs):
    r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/embeddings",
          json.dumps({"input": xs}).encode(), {"Content-Type": "application/json"}), timeout=900)
    return [d["embedding"] for d in json.load(r)["data"]]
xs = ["PostgreSQL 기본 격리 수준", "Photosynthesis converts light energy into chemical energy.", "로마 제국의 분열"]
a = emb(19411, xs); b = emb(19413, xs)
cos = [sum(x * y for x, y in zip(u, v)) / math.sqrt(sum(x * x for x in u) * sum(y * y for y in v)) for u, v in zip(a, b)]
paras = []
for p in glob.glob(os.path.expanduser("~/development/projects/winterholic-ttakkari/back/docs/**/*.md"), recursive=True):
    paras += [s.strip()[:700] for s in open(p, encoding="utf-8", errors="ignore").read().split("\n\n") if len(s.strip()) >= 200]
paras = paras[:256]
t = time.perf_counter()
with cf.ThreadPoolExecutor(4) as ex:
    list(ex.map(lambda bt: emb(19413, bt), [paras[i:i + 32] for i in range(0, len(paras), 32)]))
el = time.perf_counter() - t
print(json.dumps({"gpu_vs_cpu_cos": [round(c, 4) for c in cos], "cpu_passages_per_s": round(len(paras) / el, 1)}))
