# ruff: noqa
"""Tantivy BM25(임베디드) 10만 패시지 색인·질의 + Kiwi 분석 처리량."""
import csv, time, json, random, os, shutil, glob, statistics, sys
csv.field_size_limit(10**9)
import tantivy

out = {}
# Kiwi 처리량(실제 한국어 문서)
from kiwipiepy import Kiwi
t = time.perf_counter(); kiwi = Kiwi(); out["kiwi_load_s"] = round(time.perf_counter() - t, 2)
paras = []
for p in glob.glob(os.path.expanduser("~/development/projects/winterholic-ttakkari/back/docs/**/*.md"), recursive=True):
    paras += [b.strip()[:700] for b in open(p, encoding="utf-8", errors="ignore").read().split("\n\n") if len(b.strip()) >= 200]
paras = paras[:2000]
keep = {"NNG", "NNP", "NNB", "NR", "SL", "SH", "SN", "XR", "VV", "VA"}
t = time.perf_counter(); chars = 0
for p in paras:
    chars += len(p)
    toks = [tk.form for tk in kiwi.tokenize(p) if tk.tag in keep]
el = time.perf_counter() - t
out["kiwi_single_thread"] = {"paras": len(paras), "chars_per_s": round(chars / el), "paras_per_s": round(len(paras) / el)}
sample = "AIP가 앱별 백엔드에서 줄이려는 반복 작업은 무엇이며 어떤 원칙으로 처리하나?"
out["kiwi_sample"] = [f"{tk.form}/{tk.tag}" for tk in kiwi.tokenize(sample) if tk.tag in keep]

# Tantivy 색인
path = "/tmp/kbv2/tvidx"; shutil.rmtree(path, ignore_errors=True); os.makedirs(path)
sb = tantivy.SchemaBuilder()
sb.add_text_field("pid", stored=True, tokenizer_name="raw")
sb.add_text_field("body", stored=False, tokenizer_name="whitespace")
schema = sb.build()
idx = tantivy.Index(schema, path=path)
w = idx.writer(heap_size=512_000_000, num_threads=4)
rows = []
t = time.perf_counter()
with open("/tmp/kbv2/pas.csv", newline="") as f:
    for r in csv.reader(f):
        w.add_document(tantivy.Document(pid=r[0], body=r[7]))
        if len(rows) < 400 and random.random() < 0.005: rows.append(r[7].split())
w.commit(); w.wait_merging_threads()
out["tantivy_index_100k_s"] = round(time.perf_counter() - t, 1)
out["tantivy_size_mb"] = round(sum(os.path.getsize(os.path.join(path, x)) for x in os.listdir(path)) / 1e6, 1)
idx.reload(); s = idx.searcher()
random.seed(3)
lat = []
for toks in rows[:200]:
    q = idx.parse_query(" OR ".join(random.sample(toks, 8)), ["body"])
    t = time.perf_counter(); s.search(q, 200); lat.append((time.perf_counter() - t) * 1000)
lat.sort()
out["tantivy_or8_top200"] = {"n": len(lat), "p50_ms": round(lat[len(lat) // 2], 2), "p95_ms": round(lat[int(len(lat) * .95)], 2)}
print(json.dumps(out, ensure_ascii=False))
