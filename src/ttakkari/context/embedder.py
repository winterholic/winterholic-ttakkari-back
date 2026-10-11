"""임베딩. 기본은 로컬 multilingual-e5-large(fastembed), 테스트는 결정적 해시 임베딩(ADR 0007)."""

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


class HashEmbedder:
    """글자 2-gram 을 해시로 흩뿌린 벡터. 뜻은 모르지만 같은 단어가 겹치면 가깝다. 테스트·모델 준비 전 대체용."""

    name = "hash"

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            s = re.sub(r"\s+", " ", t.lower())
            for i in range(max(1, len(s) - 1)):
                h = int.from_bytes(hashlib.blake2b(s[i:i + 2].encode(), digest_size=4).digest(), "big")
                v[h % EMBED_DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class E5Embedder:
    """intfloat/multilingual-e5-large. 질의는 'query: ', 문서는 'passage: ' 접두사가 필요하다."""

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

    @property
    def ready(self) -> bool:
        return self._model is not None

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        prefix = "query: " if query else "passage: "
        return [list(map(float, v)) for v in self._load().embed([prefix + t for t in texts])]


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = E5Embedder() if get_settings().context_embedder == "e5" else HashEmbedder()
    return _embedder


async def embed(texts: list[str], *, query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    return await asyncio.to_thread(get_embedder().embed, texts, query=query)


def warmup() -> None:
    """모델 로드(약 23초)를 서버 시작 직후 백그라운드에서 끝내 둔다."""
    e = get_embedder()
    if isinstance(e, E5Embedder):
        def _go() -> None:
            try:
                e.embed(["warmup"], query=True)
                log.info("context embedder ready")
            except Exception:
                log.exception("context embedder failed to load")

        threading.Thread(target=_go, name="embedder-warmup", daemon=True).start()
