# ruff: noqa
import asyncio, random, time, statistics, json, sys
import asyncpg

async def main():
    c = await asyncpg.connect(host="/tmp/kbv2", port=55432, user="kb", database="kbm")
    rows = await c.fetch("select analyzed from pas tablesample system(1) limit 400")
    random.seed(3)
    qs = []
    for r in rows:
        toks = r["analyzed"].split()
        qs.append(random.sample(toks, min(8, len(toks))))
    out = {}
    for label, sql in {
        "or_rank_top200": "select id, ts_rank_cd(tsv, q) r from pas, to_tsquery('simple', $1) q where tsv @@ q order by r desc limit 200",
        "or_count_only": "select count(*) from pas where tsv @@ to_tsquery('simple', $1)",
    }.items():
        lat = []; hits = []
        for q in qs[:200]:
            tq = " | ".join(q)
            t = time.perf_counter()
            res = await c.fetch(sql, tq)
            lat.append((time.perf_counter() - t) * 1000)
            hits.append(len(res) if label.startswith("or_rank") else res[0][0])
        lat.sort()
        out[label] = {"n": len(lat), "p50_ms": round(lat[len(lat)//2], 1), "p95_ms": round(lat[int(len(lat)*0.95)], 1),
                      "median_hits": statistics.median(hits)}
    print(json.dumps(out, ensure_ascii=False))
    await c.close()

asyncio.run(main())
