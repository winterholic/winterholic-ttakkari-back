"""회의록 벡터 저장용 임베딩(ADR 0021). 기본은 로컬 multilingual-e5-large(fastembed), 테스트는 결정적 해시 임베딩.

ADR 0009 의 키워드 검색은 그대로 두고, 회의록 조각만 벡터를 더해 표현이 달라도 찾게 한다.
모델은 ~/.ttakkari/models 에 받아 두고 처음 쓸 때 올린다(약 3~25초). 외부 API 로 텍스트를 보내지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import threading
from typing import Protocol

from ttakkari.config import get_settings
from ttakkari.models import EMBED_DIM

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]: ...


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class HashEmbedder:
    """글자 2-gram 을 해시로 흩뿌린 벡터. 뜻은 모르지만 같은 단어가 겹치면 가깝다. 테스트·모델 없는 환경용."""

    name = "hash"

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
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
    """intfloat/multilingual-e5-large. 질의는 'query: ', 문서는 'passage: ' 접두사가 필요하고, 출력은 정규화되지 않아 직접 맞춘다."""

    name = "e5"

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                s = get_settings()
                self._model = TextEmbedding(s.context_model, cache_dir=str(s.models_dir))
        return self._model

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        prefix = "query: " if query else "passage: "
        return [_unit(list(map(float, v))) for v in self._load().embed([prefix + t for t in texts], batch_size=16)]


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
    return await asyncio.to_thread(get_embedder().embed, texts, query=query)
