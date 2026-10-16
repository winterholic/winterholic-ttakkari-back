"""운영 이식본(ttakkari.runs.desk_qwen)이 하네스 v1 과 같게 동작하는지, lead_id 선택은 쓸 만한지 실제 모델로 확인한다."""
import asyncio, json, os, re, sys, time
sys.path.insert(0, os.environ["DESK_SRC"])
from ttakkari.config import get_settings
from ttakkari.runs import desk_qwen
s = get_settings(); s.desk_qwen_base_url = os.environ["QWEN_ENDPOINT"]; s.desk_qwen_enabled = True

async def main():
    out = {}
    for name in ("dev3", "dev2", "dev"):
        rows = [json.loads(l) for l in open(f"{name}.jsonl")]
        res = []
        for c in rows:
            ctx = c["context"]
            board = [] if ctx["board"] == "(맡긴 일 없음)" else ctx["board"].split("\n")
            hist = [] if ctx["history"] in ("(최근 대화 없음)", "(없음)") else ctx["history"].split("\n")
            ids = re.findall(r"lead_id=(\S+)", ctx["board"])
            t = time.monotonic()
            d = await desk_qwen.decide(board, hist, c["message"], ids)
            pa = "answer" if d.action == "answer" else ("abstain" if d.abstained else "delegate")
            res.append(dict(case_id=c["case_id"], gold=c["gold_status"], got=pa, ms=int((time.monotonic()-t)*1000), calls=d.calls, lead=d.lead_id))
        hard = [r for r in res if r["gold"] != "hold"]
        ok = sum(r["gold"] == r["got"] for r in hard)
        out[name] = (ok, len(hard))
        json.dump(res, open(f"port_{name}.json", "w"), ensure_ascii=False)
        print(name, ok, len(hard), flush=True)
    rows = [json.loads(l) for l in open("lead_dev.jsonl")]
    hit = 0; wrong_attach = 0; missed = 0; det = []
    for c in rows:
        board = c["board"].split("\n"); hist = [] if c["history"] == "(최근 대화 없음)" else c["history"].split("\n")
        ids = re.findall(r"lead_id=(\S+)", c["board"])
        d = await desk_qwen.decide(board, hist, c["message"], ids)
        got = d.lead_id if d.action == "delegate" else "ANSWER"
        det.append((c["message"], c["expected"], got, d.kind))
        if got == c["expected"]: hit += 1
        elif c["expected"] is None: wrong_attach += 1
        else: missed += 1
    print("lead", hit, len(rows), "wrong_attach", wrong_attach, "missed_attach", missed, flush=True)
    for m, e, g, k in det:
        if e != g: print("  MISS", m, "| exp", (e or "None")[:4], "| got", (g or "None")[:6], k)

asyncio.run(main())
