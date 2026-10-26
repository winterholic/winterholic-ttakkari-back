"""kb-eval/fixtures/kb3_dev_benchmark.json 생성기: 코퍼스·질문·정답 anchor 와 정확한 SHA-256/오프셋을 만든다.

predictions 는 이 스크립트가 지어내지 않는다. 사람이 쓴 순위·coverage·점수는 측정이 아니라 가정이므로,
scripts/collect_kb3_benchmark_predictions.py 가 임시 PostgreSQL 에서 실제 검색 함수를 돌려 만든 JSON 만
--predictions 로 받는다. 지정하지 않으면 predictions 는 비어 있고(런너는 missing_prediction 으로 센다) 지표를 만들지 않는다.

사용: python scripts/generate_kb3_benchmark_fixture.py [--predictions PATH] [--code-sha SHA] [--build-id ID]
"""

import argparse
import hashlib
import json
from pathlib import Path

ARGS = argparse.ArgumentParser()
ARGS.add_argument("--predictions", type=Path, default=None)
ARGS.add_argument("--code-sha", default="ba71814167d281d1d1ff31ba405692eb12f87a23")
ARGS.add_argument("--build-id", default="kb3-search-dev-1")
ARGS.add_argument("--model-id", default="fixture-no-inference")
ARGS.add_argument("--quantization", default="fixture")
ARGS.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "kb-eval/fixtures/kb3_dev_benchmark.json")
OPTS = ARGS.parse_args()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 6 Documents
docs = [
    {
        "doc_key": "k8s-deploy",
        "rev_id": "r-k8s-deploy-v1",
        "title": "Kubernetes Deployment and Rollback Guide",
        "body": "Kubernetes Deployment Rollback: To undo a rollout and return to the previous revision, run kubectl rollout undo deployment. Deployments manage ReplicaSets for declarative pod updates.",
        "uri": "fixture://k8s-deploy",
        "passages": [
            {
                "id": "p-deploy-rollback",
                "start": 0,
                "end": 178,
                "quote": "kubectl rollout undo deployment",
                "quote_start": 78,
                "quote_end": 109,
            }
        ],
    },
    {
        "doc_key": "k8s-hpa",
        "rev_id": "r-k8s-hpa-v1",
        "title": "Horizontal Pod Autoscaler (HPA) Overview",
        "body": "Horizontal Pod Autoscaler (HPA) automatically updates a workload resource such as a Deployment or StatefulSet with the aim of automatically scaling the workload to match demand.",
        "uri": "fixture://k8s-hpa",
        "passages": [
            {
                "id": "p-hpa-scale",
                "start": 0,
                "end": 179,
                "quote": "automatically scaling the workload to match demand",
                "quote_start": 128,
                "quote_end": 178,
            }
        ],
    },
    {
        "doc_key": "k8s-rs",
        "rev_id": "r-k8s-rs-v1",
        "title": "ReplicaSet Object Specification",
        "body": "A ReplicaSet controller ensures that a specified number of pod replicas are running at any given time. While Deployments manage ReplicaSets, ReplicaSets directly manage Pods.",
        "uri": "fixture://k8s-rs",
        "passages": [
            {
                "id": "p-rs-manage",
                "start": 0,
                "end": 173,
                "quote": "ReplicaSet controller ensures that a specified number of pod replicas are running",
                "quote_start": 2,
                "quote_end": 83,
            }
        ],
    },
    {
        "doc_key": "k8s-ver-conflict-v1",
        "rev_id": "r-k8s-ver-v1",
        "title": "Kubernetes Compatibility Matrix v1.28",
        "body": "Kubernetes minimum supported runtime requirement: CRI-O version 1.28 or containerd 1.6 is required for cluster installation.",
        "uri": "fixture://k8s-ver-v1",
        "passages": [
            {
                "id": "p-ver-v1",
                "start": 0,
                "end": 126,
                "quote": "containerd 1.6 is required",
                "quote_start": 72,
                "quote_end": 98,
            }
        ],
    },
    {
        "doc_key": "k8s-ver-conflict-v2",
        "rev_id": "r-k8s-ver-v2",
        "title": "Kubernetes Compatibility Matrix v1.31",
        "body": "Kubernetes minimum supported runtime requirement: containerd 1.7 or CRI-O 1.30 is required. Older runtime 1.6 is deprecated and unsupported.",
        "uri": "fixture://k8s-ver-v2",
        "passages": [
            {
                "id": "p-ver-v2",
                "start": 0,
                "end": 142,
                "quote": "containerd 1.7 or CRI-O 1.30 is required",
                "quote_start": 50,
                "quote_end": 90,
            }
        ],
    },
    {
        "doc_key": "unrelated-doc",
        "rev_id": "r-unrelated-v1",
        "title": "Unrelated General Topic",
        "body": "This document discusses unrelated gardening techniques and seasonal plants for indoor cultivation.",
        "uri": "fixture://unrelated",
        "passages": [
            {
                "id": "p-unrelated",
                "start": 0,
                "end": 99,
                "quote": "gardening techniques",
                "quote_start": 24,
                "quote_end": 44,
            }
        ],
    },
]

# 오프셋은 손으로 세지 않고 본문에서 계산한다(손으로 센 값이 본문 길이를 넘거나 인용 위치가 어긋났었다).
for d in docs:
    for p in d["passages"]:
        assert d["body"].count(p["quote"]) == 1, (d["doc_key"], "quote must occur exactly once")
        p["start"], p["end"] = 0, len(d["body"])
        p["quote_start"] = d["body"].index(p["quote"])
        p["quote_end"] = p["quote_start"] + len(p["quote"])

sources_list = []
for d in docs:
    b_sha = sha256_hex(d["body"])
    passages = []
    for p in d["passages"]:
        passages.append({
            "id": p["id"],
            "start": p["start"],
            "end": p["end"],
        })
    sources_list.append({
        "doc_key": d["doc_key"],
        "source_revision_id": d["rev_id"],
        "raw_sha256": b_sha,
        "body_sha256": b_sha,
        "body": d["body"],
        "source_uri": d["uri"],
        "observed_at": "2026-10-25T00:00:00Z",
        "raw_utf8": d["body"],
        "passages": passages,
    })

cases_list = [
    # Q1: 한글 질문 -> 영문 문서 (Cross-lingual)
    {
        "id": "q1-cross-lingual",
        "group": "k8s-core",
        "split": "dev",
        "documents": ["k8s-deploy"],
        "answerable": True,
        "relevance": {"p-deploy-rollback": 3},
        "domain": "dev",
        "types": ["cross_lingual"],
        "language_pair": "ko-en",
        "query": "쿠버네티스 디플로이먼트 롤백 방법",
        "verification": "draft",
        "anchors": [
            {
                "doc_key": "k8s-deploy",
                "source_revision_id": "r-k8s-deploy-v1",
                "raw_sha256": sha256_hex(docs[0]["body"]),
                "body_sha256": sha256_hex(docs[0]["body"]),
                "source_uri": docs[0]["uri"],
                "observed_at": "2026-10-25T00:00:00Z",
                "quote": docs[0]["passages"][0]["quote"],
                "quote_sha256": sha256_hex(docs[0]["passages"][0]["quote"]),
                "context_before": docs[0]["body"][:docs[0]["passages"][0]["quote_start"]],
                "context_after": docs[0]["body"][docs[0]["passages"][0]["quote_end"]:],
                "answer_id": "a-rollback",
                "match_normalization_version": "exact-v1",
            }
        ],
    },
    # Q2: 약어 매칭 (Abbreviation)
    {
        "id": "q2-abbreviation",
        "group": "k8s-core",
        "split": "dev",
        "documents": ["k8s-hpa"],
        "answerable": True,
        "relevance": {"p-hpa-scale": 3},
        "domain": "dev",
        "types": ["abbreviation"],
        "language_pair": "ko-en",
        "query": "k8s HPA 자동확장 원리",
        "verification": "draft",
        "anchors": [
            {
                "doc_key": "k8s-hpa",
                "source_revision_id": "r-k8s-hpa-v1",
                "raw_sha256": sha256_hex(docs[1]["body"]),
                "body_sha256": sha256_hex(docs[1]["body"]),
                "source_uri": docs[1]["uri"],
                "observed_at": "2026-10-25T00:00:00Z",
                "quote": docs[1]["passages"][0]["quote"],
                "quote_sha256": sha256_hex(docs[1]["passages"][0]["quote"]),
                "context_before": docs[1]["body"][:docs[1]["passages"][0]["quote_start"]],
                "context_after": docs[1]["body"][docs[1]["passages"][0]["quote_end"]:],
                "answer_id": "a-hpa",
                "match_normalization_version": "exact-v1",
            }
        ],
    },
    # Q3: 다른 용어 / 유의어 (Synonym)
    {
        "id": "q3-synonym",
        "group": "k8s-core",
        "split": "dev",
        "documents": ["k8s-rs"],
        "answerable": True,
        "relevance": {"p-rs-manage": 3},
        "domain": "dev",
        "types": ["synonym"],
        "language_pair": "ko-en",
        "query": "파드 복제본 관리 컨트롤러 사양",
        "verification": "draft",
        "anchors": [
            {
                "doc_key": "k8s-rs",
                "source_revision_id": "r-k8s-rs-v1",
                "raw_sha256": sha256_hex(docs[2]["body"]),
                "body_sha256": sha256_hex(docs[2]["body"]),
                "source_uri": docs[2]["uri"],
                "observed_at": "2026-10-25T00:00:00Z",
                "quote": docs[2]["passages"][0]["quote"],
                "quote_sha256": sha256_hex(docs[2]["passages"][0]["quote"]),
                "context_before": docs[2]["body"][:docs[2]["passages"][0]["quote_start"]],
                "context_after": docs[2]["body"][docs[2]["passages"][0]["quote_end"]:],
                "answer_id": "a-rs",
                "match_normalization_version": "exact-v1",
            }
        ],
    },
    # Q4: 1-hop 의미 관계 (Semantic 1-hop relation)
    {
        "id": "q4-relation",
        "group": "k8s-core",
        "split": "dev",
        "documents": ["k8s-deploy", "k8s-rs"],
        "answerable": True,
        "relevance": {"p-deploy-rollback": 2, "p-rs-manage": 3},
        "domain": "dev",
        "types": ["graph_relation"],
        "language_pair": "ko-en",
        "query": "디플로이먼트가 관리하는 리소스 오브젝트",
        "verification": "draft",
        "anchors": [
            {
                "doc_key": "k8s-rs",
                "source_revision_id": "r-k8s-rs-v1",
                "raw_sha256": sha256_hex(docs[2]["body"]),
                "body_sha256": sha256_hex(docs[2]["body"]),
                "source_uri": docs[2]["uri"],
                "observed_at": "2026-10-25T00:00:00Z",
                "quote": docs[2]["passages"][0]["quote"],
                "quote_sha256": sha256_hex(docs[2]["passages"][0]["quote"]),
                "context_before": docs[2]["body"][:docs[2]["passages"][0]["quote_start"]],
                "context_after": docs[2]["body"][docs[2]["passages"][0]["quote_end"]:],
                "answer_id": "a-rel",
                "match_normalization_version": "exact-v1",
            }
        ],
    },
    # Q5: 충돌 / 버전 분기 (Conflict)
    {
        "id": "q5-conflict",
        "group": "k8s-core",
        "split": "dev",
        "documents": ["k8s-ver-conflict-v1", "k8s-ver-conflict-v2"],
        "answerable": True,
        "relevance": {"p-ver-v1": 1, "p-ver-v2": 3},
        "domain": "dev",
        "types": ["conflict"],
        "language_pair": "ko-en",
        "query": "쿠버네티스 최소 지원 런타임 버전 요구사항",
        "verification": "draft",
        "anchors": [
            {
                "doc_key": "k8s-ver-conflict-v2",
                "source_revision_id": "r-k8s-ver-v2",
                "raw_sha256": sha256_hex(docs[4]["body"]),
                "body_sha256": sha256_hex(docs[4]["body"]),
                "source_uri": docs[4]["uri"],
                "observed_at": "2026-10-25T00:00:00Z",
                "quote": docs[4]["passages"][0]["quote"],
                "quote_sha256": sha256_hex(docs[4]["passages"][0]["quote"]),
                "context_before": docs[4]["body"][:docs[4]["passages"][0]["quote_start"]],
                "context_after": docs[4]["body"][docs[4]["passages"][0]["quote_end"]:],
                "answer_id": "a-conf",
                "match_normalization_version": "exact-v1",
            }
        ],
    },
]

predictions_map = json.loads(OPTS.predictions.read_text(encoding="utf-8")) if OPTS.predictions else {}

bundle_data = {
    "schema_version": "kb-e0-v1",
    "kind": "fixture",
    "corpus_id": "kb3-benchmark-dev",
    "corpus_version": "1",
    "sources": sources_list,
    "cases": cases_list,
    "predictions": predictions_map,
    "links": [
        ["k8s-deploy", "k8s-rs"],
        ["k8s-ver-conflict-v1", "k8s-ver-conflict-v2"],
    ],
    "rights": [
        {
            "component": "code",
            "original_url": "fixture://authored-kb3",
            "version": "1",
            "checked_at": "2026-10-25T00:00:00Z",
            "storage_allowed": True,
            "transform_allowed": True,
            "redistribute_allowed": True,
            "status": "verified",
            "basis": "locally authored synthetic fixture for kb3 search",
            "provenance_kind": "synthetic",
        },
        {
            "component": "data",
            "original_url": "fixture://authored-kb3",
            "version": "1",
            "checked_at": "2026-10-25T00:00:00Z",
            "storage_allowed": True,
            "transform_allowed": True,
            "redistribute_allowed": True,
            "status": "verified",
            "basis": "locally authored synthetic fixture for kb3 search",
            "provenance_kind": "synthetic",
        },
        {
            "component": "qrels",
            "original_url": "fixture://authored-kb3",
            "version": "1",
            "checked_at": "2026-10-25T00:00:00Z",
            "storage_allowed": True,
            "transform_allowed": True,
            "redistribute_allowed": True,
            "status": "verified",
            "basis": "locally authored synthetic fixture for kb3 search",
            "provenance_kind": "synthetic",
        },
        {
            "component": "corpus",
            "original_url": "fixture://authored-kb3",
            "version": "1",
            "checked_at": "2026-10-25T00:00:00Z",
            "storage_allowed": True,
            "transform_allowed": True,
            "redistribute_allowed": True,
            "status": "verified",
            "basis": "locally authored synthetic fixture for kb3 search",
            "provenance_kind": "synthetic",
        },
    ],
    "manifest": {
        "code_sha": OPTS.code_sha,
        "build_id": OPTS.build_id,
        "model_id": OPTS.model_id,
        "model_sha256": "0" * 64,
        "analyzer_id": "exact-v1",
        "analyzer_sha256": "1" * 64,
        "quantization": OPTS.quantization,
        "query_policy_version": "kb3-v1",
        "seed": 20261025,
        "config": {
            "N": 40,
            "window": "none",
            "role": "query",
        },
    },
    "policy": {
        "margin": 0.05,
        "alpha": 0.05,
        "bootstrap_replicates": 10000,
        "live_tolerance": {
            "version": "initial-unvalidated",
            "cosine": 0.0001,
            "logit": 0.001,
        },
    },
    "exposure": {
        "benchmark_family": "none-synthetic",
        "split_overlap": "unverified",
        "checkpoint_lineage": "unverified",
    },
    "observations": {
        "kind": "fixture",
        "database_revision": None,
        "events": [
            {
                "id": "obs-kb3-1",
                "source_revision_id": "r-k8s-deploy-v1",
                "event_at": "2026-10-25T00:00:00Z",
                "received_at": "2026-10-25T00:01:00Z",
            }
        ],
    },
}

out_path = OPTS.out
out_path.write_text(json.dumps(bundle_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {out_path}: {len(cases_list)} queries, {len(sources_list)} documents, {len(predictions_map)} predictions")
