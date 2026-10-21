# ruff: noqa
"""df 필터 후보 생성 + 앱 BM25 재채점 지연. 질의 토큰 중 df/N 이 임계값 이하인 것만 후보 생성에 쓴다."""
import asyncio, random, time, json, math
import asyncpg

N = 1000000

async def main():
    c = await asyncpg.connect(host="/tmp/kbv2", port=55432, user="kb", database="kbm1")
    rows = await c.fetch("select analyzed from pas tablesample system(1) limit 400")
    df = {r["term"]: r["df"] for r in await c.fetch("select term, df from term_stats")}
    random.seed(3)
    qs = [random.sample(r["analyzed"].split(), 8) for r in rows][:200]
    out = {}
    for thr in (0.05, 0.01):
        lat = []; cand = []; bm = []
        for q in qs:
            keep = [t for t in set(q) if df.get(t, 0) / N <= thr] or sorted(set(q), key=lambda t: df.get(t, 0))[:1]
            tq = " | ".join(keep)
            t = time.perf_counter()
            res = await c.fetch(
                "select id, analyzed from pas where tsv @@ to_tsquery('simple', $1) limit 2000", tq)
            t1 = time.perf_counter()
            # 앱 BM25 재채점(k1=1.2,b=0.75, 평균 길이 근사 110)
            scored = []
            qset = set(q)
            for r in res:
                toks = r["analyzed"].split(); L = len(toks); s = 0.0
                tf = {}
                for w in toks:
                    if w in qset: tf[w] = tf.get(w, 0) + 1
                for w, f in tf.items():
                    d = df.get(w, 1); idf = math.log(1 + (N - d + 0.5) / (d + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * L / 110))
                scored.append((s, r["id"]))
            scored.sort(reverse=True)
            t2 = time.perf_counter()
            lat.append((t2 - t) * 1000); bm.append((t2 - t1) * 1000); cand.append(len(res))
        lat.sort(); bm.sort(); cand.sort()
        out[f"df<={thr}"] = {"p50_ms": round(lat[len(lat)//2], 1), "p95_ms": round(lat[int(len(lat)*.95)], 1),
                             "bm25_p50_ms": round(bm[len(bm)//2], 1), "cand_p50": cand[len(cand)//2], "cand_p95": cand[int(len(cand)*.95)]}
    print(json.dumps(out))
    await c.close()

asyncio.run(main())
