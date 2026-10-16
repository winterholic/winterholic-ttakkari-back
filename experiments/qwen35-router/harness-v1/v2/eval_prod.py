"""운영 모듈 desk_qwen.decide 를 실모델로 평가한다. 사용: DESK_SRC=<src> eval_prod.py <dataset.jsonl> <out.json> [--show-b]
gold abstain 은 '직접 답하지 않고 근거 부족으로 기획에 넘김'(action=delegate, abstained=True), gold delegate 는 abstained=False 인 delegate."""
import asyncio, json, os, re, sys, time
from collections import Counter, defaultdict
sys.path.insert(0, os.environ["DESK_SRC"])
from ttakkari.config import get_settings
from ttakkari.runs import desk_qwen
s = get_settings(); s.desk_qwen_base_url = os.environ["QWEN_ENDPOINT"]; s.desk_qwen_enabled = True
s.desk_qwen_timeout_seconds = 60.0

async def main():
    path, out = sys.argv[1], sys.argv[2]; show_b = "--show-b" in sys.argv
    rows = [json.loads(l) for l in open(path)]
    res = []
    for c in rows:
        ctx = c["context"]
        board = [] if ctx["board"] == "(맡긴 일 없음)" else ctx["board"].split("\n")
        hist = [] if ctx["history"] in ("(최근 대화 없음)", "(없음)") else ctx["history"].split("\n")
        ids = re.findall(r"lead_id=(\S+)", ctx["board"])
        t = time.monotonic()
        d = await desk_qwen.decide(board, hist, c["message"], ids)
        got = "answer" if d.action == "answer" else ("abstain" if d.abstained else "delegate")
        res.append(dict(case_id=c["case_id"], category=c["category"], split=c.get("split"), message=c["message"], gold=c["gold_status"], allowed=c["allowed_actions"],
                        got=got, kind=d.kind, evidence=d.evidence, grounded=d.grounded, reply=d.reply, ms=int((time.monotonic()-t)*1000), calls=d.calls))
    json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
    def ok(r): return r["got"] in r["allowed"] if r["gold"] == "hold" else r["got"] == r["gold"]
    for sp in ("A", "B", None):
        sub = [r for r in res if sp is None or r["split"] == sp]
        print(sp or "ALL", sum(ok(r) for r in sub), len(sub))
    by = defaultdict(lambda: [0, 0])
    for r in res: by[r["category"]][0] += ok(r); by[r["category"]][1] += 1
    print({k: f"{a}/{b}" for k, (a, b) in sorted(by.items())})
    print("fact-answers-without-support:", sum(1 for r in res if r["gold"] != "answer" and r["got"] == "answer"), "unsafe-answers listed below")
    lat = sorted(r["ms"] for r in res); print("p50", lat[len(lat)//2], "p95", lat[int(len(lat)*.95)-1])
    for r in res:
        if not ok(r) and (r["split"] == "A" or show_b or r["split"] is None):
            print(" MISS", r["case_id"], r["category"], "|", r["message"][:40], "| gold", r["gold"], "got", r["got"], r["kind"], repr(r["evidence"])[:50], "|", r["reply"][:30])
asyncio.run(main())
