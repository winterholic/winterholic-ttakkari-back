"""Retrieval and RAG evaluation adapter."""

from __future__ import annotations

import asyncio
import math
import re
import time
import unicodedata
from collections import Counter
from typing import Any
from uuid import UUID

from ttakkari.research.adapters.base import (
    AdapterEnvironmentStatus,
    AdapterRunResult,
    ExecutionAdapter,
    MeasurementRecord,
    ResourceEstimate,
)
from ttakkari.research.adapters.presets import load_retrieval_preset

STOPWORDS_KO = {
    "프로젝트", "관련", "알려줘", "찾아줘", "어디", "무엇", "어떻게",
    "대한", "관한", "있나요", "인가요", "하는", "그리고", "에서", "으로",
    "the", "is", "a", "what", "how", "and", "or", "in", "to",
}

VALID_RETRIEVER_MODES = {
    "lexical_overlap",
    "bm25",
    "dense_hash",
    "dense_e5",
    "hybrid_rrf",
    "hybrid_linear",
}
VALID_CHUNKING_STRATEGIES = {"none", "paragraph", "fixed_window"}
VALID_RERANK_MODES = {"none", "token_overlap_top_n"}


def _tokenize(text: str, remove_stopwords: bool = True) -> list[str]:
    """Tokenize Korean/mixed text into alphanumeric words and subwords."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = re.findall(r"[\w]+(?:[-.][\w]+)*", normalized)
    if remove_stopwords:
        tokens = [t for t in tokens if t not in STOPWORDS_KO and len(t) > 1]
    return tokens


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2, strict=False))
    return float(dot)


def _chunk_text(
    text: str, strategy: str, chunk_size: int = 500, chunk_overlap: int = 50
) -> list[str]:
    """Split text into chunks according to chosen strategy."""
    if strategy == "none":
        return [text]

    if strategy == "paragraph":
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        if not paragraphs:
            return [text]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for p in paragraphs:
            p_len = len(p)
            if current_len + p_len > chunk_size and current:
                chunks.append("\n\n".join(current))
                current = [p]
                current_len = p_len
            else:
                current.append(p)
                current_len += p_len + 2
        if current:
            chunks.append("\n\n".join(current))
        return chunks

    if strategy == "fixed_window":
        if len(text) <= chunk_size:
            return [text]
        step = max(1, chunk_size - chunk_overlap)
        chunks = []
        for i in range(0, len(text), step):
            chunk = text[i : i + chunk_size].strip()
            if chunk:
                chunks.append(chunk)
            if i + chunk_size >= len(text):
                break
        return chunks or [text]

    return [text]


class RetrievalEvalAdapter:
    """Bounded, reproducible retrieval evaluation adapter for RAG pipelines."""

    id: str = "retrieval_eval"
    version: str = "1.0.0"
    name_ko: str = "검색 및 RAG 평가 어댑터"
    domains: list[str] = ["rag", "embedding", "reranker"]
    supported_methods: list[str] = [
        "bm25*",
        "hybrid*",
        "chunking*",
        "rerank*",
        "lexical*",
        "dense*",
    ]
    supported_parameters: list[str] = [
        "retriever_mode",
        "chunking_strategy",
        "chunk_size",
        "chunk_overlap",
        "normalize_korean_stopwords",
        "bm25_k1",
        "bm25_b",
        "rrf_k",
        "hybrid_alpha",
        "rerank_mode",
        "rerank_window",
        "k",
        "preset_id",
        "corpus",
        "queries",
    ]
    required_inputs: list[str] = ["preset_id"]

    def check_environment(self, *, scope: str) -> AdapterEnvironmentStatus:
        """Check availability of evaluation runtime and embedders."""
        available_models = ["hash_embedder"]
        details: dict[str, Any] = {
            "scope": scope,
            "hash_embedder_ready": True,
            "bm25_ready": True,
        }

        # Check E5 cached model availability
        e5_ready = False
        try:
            from ttakkari.config import get_settings
            s = get_settings()
            cache_dir = s.models_dir
            if cache_dir and cache_dir.is_dir():
                # check if e5 model folder exists in cache
                e5_candidates = list(cache_dir.glob("*multilingual-e5*"))
                if e5_candidates:
                    e5_ready = True
                    available_models.append("multilingual-e5-large")
        except Exception:
            e5_ready = False

        details["e5_ready"] = e5_ready

        return AdapterEnvironmentStatus(
            adapter_id=self.id,
            ready=True,
            execution_mode="in_process",
            reason="내장 BM25 및 해시 임베더 검색 평가 상시 지원",
            available_models=available_models,
            details=details,
        )

    def validate_config(self, config: dict[str, Any], *, scope: str) -> list[str]:
        errors: list[str] = []

        preset_id = config.get("preset_id")
        corpus = config.get("corpus")
        queries = config.get("queries")

        if not preset_id and (not corpus or not queries):
            errors.append("preset_id 또는 (corpus 와 queries) 설정이 필수입니다.")

        if preset_id:
            try:
                load_retrieval_preset(str(preset_id))
            except FileNotFoundError:
                errors.append(f"존재하지 않는 프리셋 ID: '{preset_id}'.")

        retriever_mode = config.get("retriever_mode", "bm25")
        if retriever_mode not in VALID_RETRIEVER_MODES:
            errors.append(
                f"지원되지 않는 retriever_mode: '{retriever_mode}'. 허용 값: {sorted(VALID_RETRIEVER_MODES)}"
            )

        chunking_strategy = config.get("chunking_strategy", "none")
        if chunking_strategy not in VALID_CHUNKING_STRATEGIES:
            errors.append(
                f"지원되지 않는 chunking_strategy: '{chunking_strategy}'. 허용 값: {sorted(VALID_CHUNKING_STRATEGIES)}"
            )

        rerank_mode = config.get("rerank_mode", "none")
        if rerank_mode not in VALID_RERANK_MODES:
            if "cross_encoder" in str(rerank_mode) or "neural" in str(rerank_mode):
                errors.append(
                    f"신경망 리랭커('{rerank_mode}')는 현재 로컬 환경에서 미지원됩니다 (대체: token_overlap_top_n 또는 none)."
                )
            else:
                errors.append(
                    f"지원되지 않는 rerank_mode: '{rerank_mode}'. 허용 값: {sorted(VALID_RERANK_MODES)}"
                )

        k = config.get("k", 5)
        if not isinstance(k, int) or k < 1 or k > 50:
            errors.append("k 파라미터는 1 이상 50 이하의 정수여야 합니다.")

        return errors

    def estimate_resources(self, config: dict[str, Any]) -> ResourceEstimate:
        return ResourceEstimate(
            estimated_seconds=6,
            estimated_memory_mb=128,
            requires_gpu_lease=False,
            requires_tts_lock=False,
            requires_cpu_lock=True,
        )

    async def execute_run(
        self,
        run_id: UUID,
        config: dict[str, Any],
        *,
        scope: str,
        cancel_event: Any = None,
    ) -> AdapterRunResult:
        errors = self.validate_config(config, scope=scope)
        if errors:
            return AdapterRunResult(
                status="failed",
                raw_output={"errors": errors, "config": config},
                environment_snapshot={"scope": scope},
                error_code="invalid_config",
                error_message="; ".join(errors),
                wall_seconds=0.0,
            )

        started_wall = time.monotonic()

        # Load dataset
        preset_id = config.get("preset_id")
        if preset_id:
            preset_data = load_retrieval_preset(str(preset_id))
            corpus = preset_data.get("corpus", [])
            queries = preset_data.get("queries", [])
            dataset_name = preset_data.get("name", preset_id)
        else:
            corpus = config.get("corpus", [])
            queries = config.get("queries", [])
            dataset_name = "custom_corpus"

        retriever_mode = config.get("retriever_mode", "bm25")
        chunking_strategy = config.get("chunking_strategy", "none")
        chunk_size = int(config.get("chunk_size", 500))
        chunk_overlap = int(config.get("chunk_overlap", 50))
        remove_stopwords = bool(config.get("normalize_korean_stopwords", True))
        bm25_k1 = float(config.get("bm25_k1", 1.2))
        bm25_b = float(config.get("bm25_b", 0.75))
        rrf_k = int(config.get("rrf_k", 60))
        hybrid_alpha = float(config.get("hybrid_alpha", 0.5))
        rerank_mode = config.get("rerank_mode", "none")
        rerank_window = int(config.get("rerank_window", 10))
        k = int(config.get("k", 5))

        # Check cancellation
        if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
            return AdapterRunResult(
                status="cancelled",
                raw_output={"reason": "cancelled_before_chunking"},
                environment_snapshot={"scope": scope},
                wall_seconds=time.monotonic() - started_wall,
            )

        # 1. Chunking
        chunk_units: list[dict[str, Any]] = []
        for doc in corpus:
            doc_id = doc["id"]
            title = doc.get("title", "")
            text = doc["text"]
            text_to_chunk = f"{title}\n{text}".strip() if title else text
            chunks = _chunk_text(
                text_to_chunk,
                chunking_strategy,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            for ci, ctext in enumerate(chunks):
                chunk_units.append({
                    "chunk_id": f"{doc_id}#c{ci}",
                    "doc_id": doc_id,
                    "text": ctext,
                })

        # 2. Embedding or Token Indexing
        needs_dense = retriever_mode in {"dense_hash", "dense_e5", "hybrid_rrf", "hybrid_linear"}
        dense_chunk_vectors: list[list[float]] = []

        if needs_dense:
            if retriever_mode == "dense_e5":
                from ttakkari.context.embedder import E5Embedder
                embedder = E5Embedder()
                texts = [cu["text"] for cu in chunk_units]
                try:
                    dense_chunk_vectors = await embedder.embed(texts, query=False)
                except Exception as exc:
                    return AdapterRunResult(
                        status="failed",
                        raw_output={"error": str(exc)},
                        environment_snapshot={"scope": scope},
                        error_code="dense_e5_failed",
                        error_message=f"E5Embedder 실패: {exc}",
                        wall_seconds=time.monotonic() - started_wall,
                    )
            else:
                # dense_hash or hybrid with HashEmbedder
                from ttakkari.context.embedder import HashEmbedder
                embedder = HashEmbedder()
                texts = [cu["text"] for cu in chunk_units]
                dense_chunk_vectors = await embedder.embed(texts, query=False)

        # Precompute BM25 / Lexical statistics
        docs_tokens = [_tokenize(cu["text"], remove_stopwords) for cu in chunk_units]
        docs_counts = [Counter(dt) for dt in docs_tokens]
        doc_lengths = [len(dt) for dt in docs_tokens]
        avg_doc_len = (sum(doc_lengths) / len(doc_lengths)) if doc_lengths else 1.0
        df = Counter(token for doc in docs_counts for token in doc)
        num_docs = len(chunk_units)

        # 3. Query Execution
        query_rows: list[dict[str, Any]] = []
        query_latencies: list[float] = []

        for q_item in queries:
            if cancel_event and getattr(cancel_event, "is_set", lambda: False)():
                return AdapterRunResult(
                    status="cancelled",
                    raw_output={"reason": "cancelled_during_queries"},
                    environment_snapshot={"scope": scope},
                    wall_seconds=time.monotonic() - started_wall,
                )

            q_id = q_item["id"]
            query_str = q_item["query"]
            relevant_ids = set(q_item.get("relevant_ids") or q_item.get("relevant") or [])
            relevance_grades: dict[str, int] = q_item.get("relevance_grades") or {
                rid: 3 for rid in relevant_ids
            }

            q_start = time.monotonic()
            q_tokens = _tokenize(query_str, remove_stopwords)
            q_token_set = set(q_tokens)

            # Calculate scores for all chunks
            lexical_scores = [0.0] * num_docs
            bm25_scores = [0.0] * num_docs
            dense_scores = [0.0] * num_docs

            if retriever_mode in {"lexical_overlap", "hybrid_rrf", "hybrid_linear"}:
                for idx, ccount in enumerate(docs_counts):
                    if not q_tokens:
                        continue
                    overlap = sum(ccount.get(t, 0) > 0 for t in q_tokens)
                    lexical_scores[idx] = overlap / len(q_tokens)

            if retriever_mode in {"bm25", "hybrid_rrf", "hybrid_linear"}:
                for idx, (ccount, length) in enumerate(zip(docs_counts, doc_lengths, strict=False)):
                    score = 0.0
                    for t in q_token_set:
                        freq = ccount.get(t, 0)
                        if freq > 0:
                            doc_freq = df.get(t, 0)
                            idf = math.log(1.0 + (num_docs - doc_freq + 0.5) / (doc_freq + 0.5))
                            num = freq * (bm25_k1 + 1.0)
                            den = freq + bm25_k1 * (1.0 - bm25_b + bm25_b * (length / avg_doc_len))
                            score += idf * (num / den)
                    bm25_scores[idx] = score

            if needs_dense:
                q_vec_list = await embedder.embed([query_str], query=True)
                q_vec = q_vec_list[0]
                dense_scores = [_cosine_similarity(q_vec, d_vec) for d_vec in dense_chunk_vectors]

            # Aggregate chunk scores to doc_id
            doc_scores: dict[str, float] = {}

            if retriever_mode == "lexical_overlap":
                for idx, cu in enumerate(chunk_units):
                    did = cu["doc_id"]
                    doc_scores[did] = max(doc_scores.get(did, 0.0), lexical_scores[idx])

            elif retriever_mode == "bm25":
                for idx, cu in enumerate(chunk_units):
                    did = cu["doc_id"]
                    doc_scores[did] = max(doc_scores.get(did, 0.0), bm25_scores[idx])

            elif retriever_mode in {"dense_hash", "dense_e5"}:
                for idx, cu in enumerate(chunk_units):
                    did = cu["doc_id"]
                    doc_scores[did] = max(doc_scores.get(did, -1.0), dense_scores[idx])

            elif retriever_mode == "hybrid_rrf":
                # Rank chunks by BM25 and Dense, then combine ranks with RRF
                ranked_bm25 = sorted(range(num_docs), key=lambda i: bm25_scores[i], reverse=True)
                ranked_dense = sorted(range(num_docs), key=lambda i: dense_scores[i], reverse=True)
                bm25_ranks = {idx: rank + 1 for rank, idx in enumerate(ranked_bm25)}
                dense_ranks = {idx: rank + 1 for rank, idx in enumerate(ranked_dense)}

                chunk_rrf: list[float] = [
                    (1.0 / (rrf_k + bm25_ranks[i])) + (1.0 / (rrf_k + dense_ranks[i]))
                    for i in range(num_docs)
                ]
                for idx, cu in enumerate(chunk_units):
                    did = cu["doc_id"]
                    doc_scores[did] = max(doc_scores.get(did, 0.0), chunk_rrf[idx])

            elif retriever_mode == "hybrid_linear":
                # Min-max normalize bm25 and dense
                max_b = max(bm25_scores) if bm25_scores else 1.0
                min_b = min(bm25_scores) if bm25_scores else 0.0
                range_b = (max_b - min_b) or 1.0

                max_d = max(dense_scores) if dense_scores else 1.0
                min_d = min(dense_scores) if dense_scores else 0.0
                range_d = (max_d - min_d) or 1.0

                for idx, cu in enumerate(chunk_units):
                    nb = (bm25_scores[idx] - min_b) / range_b
                    nd = (dense_scores[idx] - min_d) / range_d
                    combined = hybrid_alpha * nb + (1.0 - hybrid_alpha) * nd
                    did = cu["doc_id"]
                    doc_scores[did] = max(doc_scores.get(did, 0.0), combined)

            # Sort documents
            ranked_docs = sorted(doc_scores.keys(), key=lambda d: doc_scores[d], reverse=True)

            # Optional reranking on top N
            if rerank_mode == "token_overlap_top_n":
                top_n = ranked_docs[:rerank_window]
                rest = ranked_docs[rerank_window:]

                # Document text map for reranking
                doc_text_map = {d["id"]: d["text"] for d in corpus}

                def _rerank_score(did: str) -> float:
                    d_tokens = set(_tokenize(doc_text_map.get(did, ""), remove_stopwords))
                    if not q_token_set or not d_tokens:
                        return 0.0
                    jaccard = len(q_token_set & d_tokens) / len(q_token_set | d_tokens)
                    return jaccard

                reranked_top = sorted(top_n, key=_rerank_score, reverse=True)
                ranked_docs = reranked_top + rest

            q_latency_ms = (time.monotonic() - q_start) * 1000.0
            query_latencies.append(q_latency_ms)

            # Evaluate metrics at k
            hits_at_k = ranked_docs[:k]
            relevant_hits = [did for did in hits_at_k if did in relevant_ids]

            recall_k = len(relevant_hits) / len(relevant_ids) if relevant_ids else 0.0
            mrr_k = 0.0
            for rank_idx, did in enumerate(hits_at_k):
                if did in relevant_ids:
                    mrr_k = 1.0 / (rank_idx + 1)
                    break

            # Graded NDCG@k
            dcg = 0.0
            for rank_idx, did in enumerate(hits_at_k):
                grade = relevance_grades.get(did, 0)
                dcg += (2**grade - 1) / math.log2(rank_idx + 2)

            ideal_grades = sorted(relevance_grades.values(), reverse=True)[:k]
            idcg = sum((2**g - 1) / math.log2(rank_idx + 2) for rank_idx, g in enumerate(ideal_grades))
            ndcg_k = (dcg / idcg) if idcg > 0.0 else 0.0

            query_rows.append({
                "query_id": q_id,
                "query": query_str,
                "latency_ms": round(q_latency_ms, 2),
                "recall_at_k": round(recall_k, 4),
                "mrr_at_k": round(mrr_k, 4),
                "ndcg_at_k": round(ndcg_k, 4),
                "ranked_hits": hits_at_k,
                "relevant_ids": sorted(relevant_ids),
            })

        total_wall = time.monotonic() - started_wall
        mean_recall = sum(r["recall_at_k"] for r in query_rows) / len(query_rows) if query_rows else 0.0
        mean_mrr = sum(r["mrr_at_k"] for r in query_rows) / len(query_rows) if query_rows else 0.0
        mean_ndcg = sum(r["ndcg_at_k"] for r in query_rows) / len(query_rows) if query_rows else 0.0
        mean_latency = sum(query_latencies) / len(query_latencies) if query_latencies else 0.0
        sorted_latencies = sorted(query_latencies)
        p95_idx = int(math.ceil(0.95 * len(sorted_latencies))) - 1
        p95_latency = sorted_latencies[max(0, p95_idx)] if sorted_latencies else 0.0

        raw_output = {
            "dataset_name": dataset_name,
            "corpus_count": len(corpus),
            "chunk_count": len(chunk_units),
            "query_count": len(queries),
            "k": k,
            "retriever_mode": retriever_mode,
            "chunking_strategy": chunking_strategy,
            "rerank_mode": rerank_mode,
            "summary": {
                "recall_at_k": round(mean_recall, 4),
                "mrr_at_k": round(mean_mrr, 4),
                "ndcg_at_k": round(mean_ndcg, 4),
                "query_latency_ms_mean": round(mean_latency, 2),
                "query_latency_ms_p95": round(p95_latency, 2),
                "wall_seconds": round(total_wall, 3),
            },
            "queries": query_rows,
        }

        return AdapterRunResult(
            status="succeeded",
            raw_output=raw_output,
            environment_snapshot={
                "scope": scope,
                "dataset_name": dataset_name,
                "corpus_size": len(corpus),
                "retriever_mode": retriever_mode,
            },
            wall_seconds=round(total_wall, 3),
        )

    def to_measurements(
        self, result: AdapterRunResult, config: dict[str, Any]
    ) -> list[MeasurementRecord]:
        if result.status != "succeeded":
            return []

        summary = result.raw_output.get("summary", {})
        k = result.raw_output.get("k", config.get("k", 5))

        return [
            MeasurementRecord(
                metric_key=f"recall_at_{k}",
                metric_label=f"재현율 (Recall@{k})",
                value=float(summary.get("recall_at_k", 0.0)),
                unit="ratio",
                direction="higher_is_better",
                aggregator="mean",
            ),
            MeasurementRecord(
                metric_key=f"mrr_at_{k}",
                metric_label=f"상호 역순위 (MRR@{k})",
                value=float(summary.get("mrr_at_k", 0.0)),
                unit="ratio",
                direction="higher_is_better",
                aggregator="mean",
            ),
            MeasurementRecord(
                metric_key=f"ndcg_at_{k}",
                metric_label=f"정규화 할인 누적 이득 (NDCG@{k})",
                value=float(summary.get("ndcg_at_k", 0.0)),
                unit="ratio",
                direction="higher_is_better",
                aggregator="mean",
            ),
            MeasurementRecord(
                metric_key="query_latency_ms_mean",
                metric_label="평균 질의 지연 시간",
                value=float(summary.get("query_latency_ms_mean", 0.0)),
                unit="ms",
                direction="lower_is_better",
                aggregator="mean",
            ),
            MeasurementRecord(
                metric_key="query_latency_ms_p95",
                metric_label="p95 질의 지연 시간",
                value=float(summary.get("query_latency_ms_p95", 0.0)),
                unit="ms",
                direction="lower_is_better",
                aggregator="p95",
            ),
            MeasurementRecord(
                metric_key="chunk_count",
                metric_label="분할된 총 청크 수",
                value=float(result.raw_output.get("chunk_count", 0)),
                unit="count",
                direction="lower_is_better",
                aggregator="single",
            ),
            MeasurementRecord(
                metric_key="wall_seconds",
                metric_label="전체 소요 시간",
                value=float(summary.get("wall_seconds", result.wall_seconds)),
                unit="s",
                direction="lower_is_better",
                aggregator="single",
            ),
        ]
