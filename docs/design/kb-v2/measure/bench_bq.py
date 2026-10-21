# ruff: noqa
import asyncio, time, json
import asyncpg

SQL = """
with c as (
  select passage_id, v from vec
  order by binary_quantize(v)::bit(1024) <~> binary_quantize($1::halfvec)::bit(1024) limit 200)
select passage_id from c order by v <=> $1::halfvec limit 50
"""

async def main():
    c = await asyncpg.connect(host="/tmp/kbv2", port=55432, user="kb", database="kbm")
    await c.execute("set hnsw.ef_search=200")
    qs = [r["v"] for r in await c.fetch("select v::text v from vec tablesample system(1) limit 100")]
    plan = await c.fetch("explain " + SQL, qs[0])
    lat = []
    for q in qs:
        t = time.perf_counter(); await c.fetch(SQL, q); lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    print(json.dumps({"bq_c200_rescore_top50": {"p50_ms": round(lat[50], 1), "p95_ms": round(lat[95], 1)},
                      "uses_index": any("vec_bq" in r[0] for r in plan)}))
    await c.close()

asyncio.run(main())
