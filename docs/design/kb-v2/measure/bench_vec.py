# ruff: noqa
import asyncio, time, json, random
import asyncpg

async def main():
    c = await asyncpg.connect(host="/tmp/kbv2", port=55432, user="kb", database="kbm")
    qs = [r["v"] for r in await c.fetch("select v::text v from vec tablesample system(1) limit 100")]
    out = {}
    for ef in (40, 100, 200):
        await c.execute(f"set hnsw.ef_search={ef}")
        lat = []
        for q in qs:
            t = time.perf_counter()
            await c.fetch("select passage_id from vec order by v <=> $1::halfvec limit 100", q)
            lat.append((time.perf_counter() - t) * 1000)
        lat.sort(); out[f"ef{ef}_top100"] = {"p50_ms": round(lat[50], 1), "p95_ms": round(lat[95], 1)}
    # 증분 삽입: HNSW 가 있는 상태에서 1,000행
    rows = [(f"00000000-0000-0000-0000-{i:012d}", "[" + ",".join(f"{random.random()-0.5:.4f}" for _ in range(1024)) + "]") for i in range(1000)]
    t = time.perf_counter()
    async with c.transaction():
        await c.executemany("insert into vec values ($1::uuid, $2::halfvec)", rows)
    out["insert_1000_with_hnsw_s"] = round(time.perf_counter() - t, 2)
    print(json.dumps(out))
    await c.close()

asyncio.run(main())
