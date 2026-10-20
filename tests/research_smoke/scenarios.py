"""Local isolated-DB acceptance scenarios A-D and durable resume.
Run with the disposable tests/research_smoke/live.py server on port 18791.
RESEARCH_SMOKE_OUTBOX contains prior host_gpu bench receipts gpu-f16.json and gpu-q8.json.
The password below belongs only to that disposable test fixture, never to production.
"""

import hashlib
import json
import os
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:18791"
OUT = Path(os.environ["RESEARCH_SMOKE_OUTBOX"]).resolve()
ROOT = Path(__file__).resolve().parents[2]
c = httpx.Client(base_url=BASE, timeout=30)
r = c.post("/api/auth/login", json={"password": "research-lab-test-only-2026"})
r.raise_for_status()
c.headers["Authorization"] = "Bearer " + r.json()["access_token"]


def call(path, body=None, method=None):
    r = c.request(
        method or ("GET" if body is None else "POST"), "/api/research" + path, json=body
    )
    r.raise_for_status()
    return r.json()


def study(title, goal, env=None):
    return call(
        "/studies",
        {
            "title": title,
            "goal": goal,
            "environment": env or {},
            "criteria": ["provenance", "reproducibility"],
        },
    )["id"]


def source(sid, key, title, quote, url):
    return call(
        f"/studies/{sid}/sources",
        {"source_key": key, "title": title, "body": quote, "locator": url},
    )["units"][0]["id"]


def node(sid, kind, title, summary="", conditions=None, details=None, **kw):
    return call(
        f"/studies/{sid}/nodes",
        {
            "kind": kind,
            "title": title,
            "summary": summary,
            "conditions": conditions or {},
            "details": details or {},
            **kw,
        },
    )["id"]


ids = {}
# A: real host measurements imported with their honest external provenance.
a = study(
    "A · Qwen 추론 효율 · 짧은 입력 파일럿",
    "f16 KV와 q8 KV를 비교하되 통계적 개선이나 품질 동등성을 가정하지 않는다.",
    {
        "backend": "metal",
        "model": "Qwen3.5-4B-Q4_K_M",
        "hardware": "Mac Studio M5 Max",
        "ram_gb": 64,
    },
)
ids["A"] = a
m = node(
    a,
    "method",
    "KV 저장 정밀도 비교",
    "실험 후보. 4K 문맥에서의 짧은 입력 파일럿; 다른 모델로 일반화하지 않는다.",
    {"backend": ["metal"]},
    {
        "cost_seconds": 180,
        "mechanism": "Cache storage format; quality not established by throughput",
    },
)
for filename in ["gpu-f16.json", "gpu-q8.json"]:
    raw = json.loads((OUT / filename).read_text())
    e = call(
        f"/studies/{a}/experiments",
        {
            "title": filename,
            "hypothesis": "캐시 저장 형식이 처리량에 영향을 줄 수 있다.",
            "rationale": "시간 상한과 단일 호스트 시험 인스턴스를 유지하며 비교",
            "adapter": "external",
            "config": raw["request"],
            "environment": {
                **raw["request"],
                "dataset": "fixed-22-token-prompt",
                "dataset_revision": "v1",
                "metric": "generation_tokens_per_second",
                "protocol": "host_gpu_bench-96-tokens-3-runs",
            },
            "budget": {"seconds": 180, "memory_mb": 4096},
            "method_ids": [m],
        },
    )
    call(
        "/experiments/" + e["id"] + "/report",
        {
            "result": raw,
            "interpretation": "실제 호스트 GPU 경로에서 측정된 도구 응답의 가져오기. Lab 내 실행 원점 검증은 별도. 출력 품질·정확한 장치 계층 배치는 미검증.",
            "next_actions": ["더 긴 입력·출력과 품질 평가, 독립 반복 실험"],
        },
    )
call(f"/studies/{a}/select", {"budget_seconds": 180})
# B: actual current Ttakkari documentation, no planted ranking gains.
corpus = []
for f in sorted((ROOT / "docs/adr").glob("*.md")):
    corpus.append({"id": f.name, "text": f.read_text()[:1800]})
queries = []
for q, prefix in [
    ("로컬 Whisper STT 전사", "0025"),
    ("host GPU Metal 시험", "0037"),
    ("지식 원장 evidence knowledge", "0034"),
    ("PMS WBS 프로젝트", "0015"),
]:
    relevant = [d["id"] for d in corpus if d["id"].startswith(prefix)]
    if relevant:
        queries.append({"query": q, "relevant": relevant})
assert len(queries) >= 3
config = {"corpus": corpus, "queries": queries, "k": 5}
b = study(
    "B · 따까리 문서 검색 품질",
    "현행 ADR 문서에서 단어 일치와 BM25를 같은 평가 질문으로 비교한다. 에이전트가 만든 qrels의 타당성은 추가 검토한다.",
    {
        "backend": "cpu",
        "hardware": "Mac Studio M5 Max",
        "model": "lexical/bm25",
        "dataset": "ttakkari-adr",
        "dataset_revision": hashlib.sha256(
            json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "metric": "recall@5",
        "protocol": "retrieval-eval-v1",
    },
)
ids["B"] = b
node(
    b,
    "method",
    "BM25",
    "TF/문서 빈도/길이 정규화. 데이터에 따라 효과가 달라진다.",
    {"backend": ["cpu"]},
    {"cost_seconds": 10},
)
e = call(
    f"/studies/{b}/experiments",
    {
        "title": "실제 ADR 문서 · lexical vs BM25",
        "hypothesis": "문서 길이와 단어 빈도를 반영하면 순위가 달라질 수 있다.",
        "rationale": "임베딩 모델을 추가하기 전 저비용 기준선 측정",
        "adapter": "retrieval_eval",
        "config": config,
        "environment": {
            "model": "lexical/bm25",
            "model_revision": "rules-v1",
            "hardware": "Mac Studio M5 Max",
            "software_revision": "research-v1",
            "dataset": "ttakkari-adr",
            "dataset_revision": hashlib.sha256(
                json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "metric": "recall@5",
            "protocol": "retrieval-eval-v1",
        },
        "budget": {"seconds": 10, "memory_mb": 64},
    },
)
measured = call("/experiments/" + e["id"] + "/execute", {})
assert measured["state"] == "completed", measured
# C: short source-exact excerpt; cause remains a separate unverified hypothesis.
cid = study(
    "C · 경험적 발견과 설명의 분리",
    "Zero-shot CoT의 보고된 효과와 작동 원리에 대한 추정을 분리하여 보존한다.",
)
ids["C"] = cid
quote = 'we show that LLMs are decent zero-shot reasoners by simply adding "Let\'s think step by step" before each answer.'
u = source(
    cid,
    "arxiv:2205.11916v4",
    "Large Language Models are Zero-Shot Reasoners",
    quote,
    "https://arxiv.org/abs/2205.11916v4",
)
f = node(
    cid,
    "finding",
    "간단한 프롬프트의 경험적 효과",
    "저자 보고. 따까리 직접 재현 아님. 원인을 확정하지 않는다.",
    {"model": ["text-davinci-002", "PaLM"], "tasks": ["MultiArith", "GSM8K"]},
    unit_id=u,
    start=0,
    quote=quote,
)
h = node(
    cid,
    "hypothesis",
    "작동 원리 미확정",
    "이 데이터는 원인이나 보편적 효과를 입증하지 않는다.",
    details={"mechanism_status": "not_established"},
)
call(
    f"/studies/{cid}/relations",
    {
        "from_id": f,
        "to_id": h,
        "predicate": "does_not_establish_mechanism",
        "rationale": "관찰된 효과와 해석은 별개다.",
    },
)
# D: papers that disagree at a high level without inventing matched experimental conditions.
d = study(
    "D · RAG와 긴 문맥의 상충하는 결론",
    "양쪽 주장을 보존하고 자원·모델·검색 방식 차이를 확인하기 전 승자를 정하지 않는다.",
)
ids["D"] = d
claims = []
for key, title, q, url, summary in [
    (
        "arxiv:2407.16833v2",
        "RAG or Long-Context?",
        "when resourced sufficiently, LC consistently outperforms RAG in terms of average performance.",
        "https://arxiv.org/abs/2407.16833v2",
        "충분한 자원이라는 조건 아래 저자 평가 평균에서 LC 우세. 실험 전체 조건은 본문 추가 확인 필요.",
    ),
    (
        "arxiv:2409.01666v1",
        "In Defense of RAG",
        "With OP-RAG, as the number of retrieved chunks increases, the answer quality initially rises, and then declines, forming an inverted U-shaped curve.",
        "https://arxiv.org/abs/2409.01666v1",
        "OP-RAG의 검색량과 품질 관계. 특정 조건에서 LC보다 효율적이라는 저자 보고와 연결; 보편 우열 아님.",
    ),
]:
    # Keep the source quote <=25 words.
    q = (
        q
        if len(q.split()) <= 25
        else "the answer quality initially rises, and then declines, forming an inverted U-shaped curve."
    )
    u = source(d, key, title, q, url)
    claims.append(
        node(
            d,
            "claim",
            title,
            summary,
            details={"full_conditions": "not_yet_extracted"},
            unit_id=u,
            start=0,
            quote=q,
        )
    )
call(
    f"/studies/{d}/relations",
    {
        "from_id": claims[0],
        "to_id": claims[1],
        "predicate": "contrasting_conclusions_requires_conditions",
        "rationale": "자원 충분성, 모델, 입력 길이, 검색 순서, 데이터셋 차이를 확인해야 한다. 어느 한쪽도 보편 사실로 승격하지 않는다.",
        "conditions": {
            "comparability": "unknown",
            "review_needed": ["model", "dataset", "retrieval_order", "resource_budget"],
        },
    },
)
# E: independent session token, new HTTP client, same persisted research.
call(
    f"/studies/{b}",
    {
        "expected_version": 1,
        "phase": "paused",
        "next_actions": [
            "qrels 독립 검토",
            "한글 토큰화·임베딩·리랭킹 대안 비교",
            "실패한 조건과 데이터 버전을 먼저 복원",
        ],
    },
    "PATCH",
)
snapshots = {k: call(f"/studies/{sid}/resume") for k, sid in ids.items()}
assert len(snapshots["D"]["nodes"]) == 2 and all(
    n["epistemic_status"] == "source_reported" for n in snapshots["D"]["nodes"]
)
assert (
    snapshots["B"]["study"]["next_actions"]
    and snapshots["B"]["experiments"][0]["receipt"]["input_sha256"]
)
(OUT / "research-scenarios.json").write_text(
    json.dumps(
        {
            "environment": "isolated_live_test_database",
            "study_ids": ids,
            "snapshots": snapshots,
        },
        ensure_ascii=False,
        indent=2,
    )
)
print(
    json.dumps(
        {
            "study_ids": ids,
            "B_metrics": measured["receipt"]["result"]["means"],
            "corpus": len(corpus),
            "queries": len(queries),
            "C_D_status": "source_reported; causes and fair comparison unresolved",
            "E": "persisted resume verified; separate token session also covered by API test",
        },
        ensure_ascii=False,
    )
)
