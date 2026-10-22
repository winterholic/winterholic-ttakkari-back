# ruff: noqa
"""한국어 질의 ↔ 영어 패시지 짝 8개 스모크. 정답 짝 코사인과 오답 짝 코사인 분포를 본다. 평가가 아니라 실행 가능성 확인용."""
import json, urllib.request, math, sys

pairs = [
    ("PostgreSQL 트랜잭션 기본 격리 수준", "Read Committed is the default isolation level in PostgreSQL. When a transaction uses this isolation level, a SELECT query sees only data committed before the query began."),
    ("식물이 빛 에너지를 화학 에너지로 바꾸는 과정", "Photosynthesis is the process by which plants, algae and some bacteria convert light energy into chemical energy stored in glucose."),
    ("중앙은행이 물가를 잡으려고 금리를 올리는 이유", "Central banks raise policy interest rates to reduce aggregate demand and bring inflation back toward their target."),
    ("로마 제국이 동서로 나뉜 시기", "The Roman Empire was permanently divided into the Western and Eastern Roman Empires after the death of Theodosius I in 395 AD."),
    ("파이썬에서 딕셔너리 순서가 보장되는 버전", "Changed in version 3.7: Dictionary order is guaranteed to be insertion order."),
    ("빛의 속도는 얼마인가", "The speed of light in vacuum is exactly 299,792,458 metres per second."),
    ("인구 고령화가 연금 재정에 미치는 영향", "Population ageing increases the old-age dependency ratio, putting pressure on pay-as-you-go pension systems."),
    ("다중 버전 동시성 제어 방식", "PostgreSQL maintains data consistency by using a multiversion model (Multiversion Concurrency Control, MVCC)."),
]
url = "http://127.0.0.1:19411/v1/embeddings"
def emb(xs):
    r = urllib.request.urlopen(urllib.request.Request(url, json.dumps({"input": xs}).encode(), {"Content-Type": "application/json"}))
    return [d["embedding"] for d in json.load(r)["data"]]
Q = emb([q for q, _ in pairs]); P = emb([p for _, p in pairs])
cos = lambda a, b: sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
pos = []; neg = []; top1 = 0
for i, q in enumerate(Q):
    s = [cos(q, p) for p in P]
    pos.append(s[i]); neg += [v for j, v in enumerate(s) if j != i]
    top1 += int(max(range(len(s)), key=s.__getitem__) == i)
print(json.dumps({"top1": f"{top1}/{len(pairs)}", "pos_min": round(min(pos), 3), "pos_mean": round(sum(pos) / len(pos), 3),
                  "neg_max": round(max(neg), 3), "neg_mean": round(sum(neg) / len(neg), 3)}))
