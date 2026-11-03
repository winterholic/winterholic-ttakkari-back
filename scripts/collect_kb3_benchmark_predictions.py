"""KB3 dev benchmark 의 predictions 를 실제 검색 함수 실행으로 만든다(사람이 쓴 순위·coverage·점수가 아니다).

운영 DB 에는 붙이지 않는다. 직접 띄운 임시 pgvector 컨테이너(alembic upgrade head 완료)에만 쓴다.
필수 환경: KB_V3_FLOW_DB_URL=postgresql+asyncpg://...127.0.0.1:<port>/<db>  (DB 이름에 'kbv3' 포함, loopback)

사용:
  python scripts/generate_kb3_benchmark_fixture.py --out /tmp/kb3_corpus.json          # predictions 없는 코퍼스/질문
  KB_V3_FLOW_DB_URL=... python scripts/collect_kb3_benchmark_predictions.py /tmp/kb3_corpus.json /tmp/kb3_predictions.json
  python scripts/generate_kb3_benchmark_fixture.py --predictions /tmp/kb3_predictions.json \\
      --code-sha <측정한 코드 SHA> --build-id kb3-search-dev-hash --model-id hash-embedder-deterministic --quantization none

측정 조건(그대로 manifest 에 남겨야 하는 사실):
 - 임베딩은 결정적 해시(TTAKKARI_CONTEXT_EMBEDDER=hash)다. 벡터 경로는 의미 품질을 재지 않는다.
 - ranked 는 kb_retrieval.search(owner 컨텍스트, hybrid, limit=10) 의 passage 순서를 fixture passage id 로 옮긴 것이다. 같은 fixture
   passage 로 옮겨지는 항목은 첫 순위만 남긴다. candidates 는 반환된 같은 목록이다(내부 후보 풀은 노출되지 않는다).
 - score_bytes 는 search 가 반환한 score(소수 4자리 반올림) 의 big-endian float64 16진수다. 반올림 전 점수가 아니다.
 - coverage 는 search 가 반환한 값이다. answer_correct 는 판정하지 않았으므로 넣지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

URL = os.environ.get("KB_V3_FLOW_DB_URL", "")
parsed = make_url(URL) if URL else None
if parsed is None or "kbv3" not in (parsed.database or "") or parsed.host not in ("127.0.0.1", "localhost"):
    sys.exit("KB_V3_FLOW_DB_URL 이 임시 컨테이너(loopback, DB 이름에 kbv3)를 가리키지 않아 중단한다")

os.environ.update(
    {
        "TTAKKARI_NO_ENV_FILE": "1",
        "TTAKKARI_ENV": "dev",
        "TTAKKARI_DATABASE_URL": URL,
        "TTAKKARI_HOME": tempfile.mkdtemp(prefix="kbv3-bench-home-"),
        "TTAKKARI_CONTEXT_EMBEDDER": "hash",
        "TTAKKARI_KB_SEMANTIC_ENABLED": "true",
        "TTAKKARI_COLLECTOR_CONTROL_ENABLED": "false",
    }
)

from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.kb_access import create_owner_context
from ttakkari.kb_library import register_text
from ttakkari.kb_pipeline import (
    KbPipelineOrchestrator,
    KbPipelineWorker,
    get_default_worker,
)
from ttakkari.kb_pipeline_models import KbPipelineJob
from ttakkari.kb_retrieval import search


def score_bytes(score: float) -> str:
    return struct.pack(">d", float(score)).hex()


async def drain(worker: KbPipelineWorker) -> int:
    total = 0
    while True:
        n = await worker.pump()
        if worker._tasks:
            await asyncio.gather(*list(worker._tasks))
        total += n
        if n == 0:
            return total


async def main(corpus_path: Path, out_path: Path) -> None:
    bundle = json.loads(corpus_path.read_text(encoding="utf-8"))
    get_default_worker().pause_claiming()
    passage_of_doc = {s["doc_key"]: s["passages"][0]["id"] for s in bundle["sources"]}
    assert all(len(s["passages"]) == 1 for s in bundle["sources"]), "fixture passage = whole body 가정"

    doc_key_by_document: dict[str, str] = {}
    async with sessionmaker()() as db:
        for s in bundle["sources"]:
            res = await register_text(
                db, title=s["doc_key"], text=s["body"], source_key=f"kb3-bench:{s['doc_key']}", lang="en",
                locator=s["source_uri"], scope="personal",
            )
            await db.commit()
            doc_key_by_document[str(res.document_id)] = s["doc_key"]
    sf = sessionmaker()
    worker = KbPipelineWorker(session_factory=sf, orchestrator=KbPipelineOrchestrator(session_factory=sf, base_backoff_seconds=0.0),
                              worker_id="kb3-bench")
    pumped = await drain(worker)
    async with sf() as db:
        jobs = list(await db.scalars(select(KbPipelineJob)))
    print(f"pipeline jobs pumped={pumped} statuses={sorted({j.status for j in jobs})}", flush=True)

    owner = create_owner_context()
    predictions: dict[str, dict] = {}
    async with sf() as db:
        for case in bundle["cases"]:
            res = await search(db, query=case["query"], access_context=owner, limit=10, mode="hybrid")
            ranked: list[str] = []
            scores: list[str] = []
            for item in res["items"]:
                fixture_passage = passage_of_doc[doc_key_by_document[item["document"]["id"]]]
                if fixture_passage in ranked:
                    continue
                ranked.append(fixture_passage)
                scores.append(score_bytes(item["score"]))
            predictions[case["id"]] = {
                "ranked": ranked, "candidates": list(ranked), "coverage": res["coverage"], "score_bytes": scores,
            }
            print(f"{case['id']}: coverage={res['coverage']} ranked={ranked}", flush=True)
    out_path.write_text(json.dumps(predictions, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
