"""회의록 벡터 저장용 임베딩(ADR 0021). 기본은 로컬 multilingual-e5-large(fastembed), 테스트는 결정적 해시 임베딩.

ADR 0009 의 키워드 검색은 그대로 두고, 회의록 조각만 벡터를 더해 표현이 달라도 찾게 한다.
모델 파일은 ~/.ttakkari/models 에 미리 준비한다. 요청별 자식 프로세스에서 로드하고 종료하며, 외부 API로 텍스트를 보내지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from typing import Protocol

from ttakkari.config import get_settings
from ttakkari.model_process import run_json_worker
from ttakkari.models import EMBED_DIM

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str], *, query: bool) -> list[list[float]]: ...


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class HashEmbedder:
    """글자 2-gram 을 해시로 흩뿌린 벡터. 뜻은 모르지만 같은 단어가 겹치면 가깝다. 테스트·모델 없는 환경용."""

    name = "hash"

    async def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            s = re.sub(r"\s+", " ", t.lower())
            for i in range(max(1, len(s) - 1)):
                h = int.from_bytes(hashlib.blake2b(s[i:i + 2].encode(), digest_size=4).digest(), "big")
                v[h % EMBED_DIM] += 1.0
            out.append(_unit(v))
        return out


class E5Embedder:
    """One request at a time; weights and native runtime live only in the child."""

    name = "e5"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if not texts:
            return []
        s = get_settings()
        async with self._lock:
            result = await run_json_worker(
                module="ttakkari.context.embedder_worker",
                request={"texts": texts, "query": query, "model": s.context_model,
                         "cache_dir": str(s.models_dir), "threads": s.context_embedding_cpu_threads},
                timeout=s.context_embedding_timeout_seconds, error_prefix="embedding_worker",
            )
        vectors = result.get("vectors", [])
        if len(vectors) != len(texts) or any(
            len(v) != EMBED_DIM or not all(isinstance(x, (float, int)) and math.isfinite(x) for x in v)
            for v in vectors
        ):
            raise ValueError("invalid_embedding_output")
        return vectors


_embedder: Embedder | None = None


def enabled() -> bool:
    return get_settings().context_embedder != "off"


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = E5Embedder() if get_settings().context_embedder == "e5" else HashEmbedder()
    return _embedder


def model_name() -> str:
    s = get_settings()
    return s.context_model if s.context_embedder == "e5" else s.context_embedder


async def embed(texts: list[str], *, query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    return await get_embedder().embed(texts, query=query)
