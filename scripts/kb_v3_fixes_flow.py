"""KB v3 교정(접근 행 없는 문서 격리, L1 대소문자, 검색 필터, 현재성) 실 PostgreSQL 흐름 확인.

운영 DB 에는 붙이지 않는다. 직접 띄운 임시 pgvector 컨테이너(alembic upgrade head 완료)에만 쓴다.
필수 환경: KB_V3_FLOW_DB_URL=postgresql+asyncpg://...127.0.0.1:<port>/<db>  (DB 이름에 'kbv3' 포함, 호스트는 loopback)
사용: KB_V3_FLOW_DB_URL=... uv run python scripts/kb_v3_fixes_flow.py
임베딩은 결정적 해시(TTAKKARI_CONTEXT_EMBEDDER=hash)다: 벡터 경로 결과는 의미 품질 증거가 아니다.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

URL = os.environ.get("KB_V3_FLOW_DB_URL", "")
parsed = make_url(URL) if URL else None
if parsed is None or "kbv3" not in (parsed.database or "") or parsed.host not in ("127.0.0.1", "localhost"):
    sys.exit("KB_V3_FLOW_DB_URL 이 임시 컨테이너(loopback, DB 이름에 kbv3)를 가리키지 않아 중단한다")

HOME = tempfile.mkdtemp(prefix="kbv3-fixes-home-")
os.environ.update(
    {
        "TTAKKARI_NO_ENV_FILE": "1",
        "TTAKKARI_ENV": "dev",
        "TTAKKARI_DATABASE_URL": URL,
        "TTAKKARI_HOME": HOME,
        "TTAKKARI_CONTEXT_EMBEDDER": "hash",
        "TTAKKARI_KB_SEMANTIC_ENABLED": "true",
        "TTAKKARI_COLLECTOR_CONTROL_ENABLED": "false",
    }
)

from sqlalchemy import func, select

from ttakkari.common.lexical import terms
from ttakkari.db import sessionmaker
from ttakkari.kb_access import (
    create_owner_context,
    create_personal_agent_context,
    create_work_agent_context,
)
from ttakkari.kb_access_backfill import run_backfill
from ttakkari.kb_library import register_text
from ttakkari.kb_models import KbPassage
from ttakkari.kb_ontology.retrieval import find_matched_entities
from ttakkari.kb_ontology_models import KbEntity, KbEntityAlias
from ttakkari.kb_pipeline import (
    KbPipelineOrchestrator,
    KbPipelineWorker,
    get_default_worker,
)
from ttakkari.kb_retrieval import search
from ttakkari.kb_store import ingest_source, segment_revision

K8S_DOC = (
    "# Platform Notes\n\n"
    "We deploy every workload on Kubernetes clusters managed by the platform team and review upgrades monthly.\n"
)
ROWLESS_WORK_DOC = "# Roadmap\n\nZebrafoo confidential roadmap for the internal platform, rowless work document.\n"


def say(msg: str = "") -> None:
    print(msg, flush=True)


async def drain() -> int:
    sf = sessionmaker()
    worker = KbPipelineWorker(
        session_factory=sf, orchestrator=KbPipelineOrchestrator(session_factory=sf, base_backoff_seconds=0.0)
    )
    pumped = 0
    for _ in range(8):
        n = await worker.pump()
        if worker._tasks:
            await asyncio.gather(*list(worker._tasks))
        pumped += n
        if n == 0:
            break
    return pumped


async def main() -> None:
    get_default_worker().pause_claiming()  # 앱이 띄운 기본 워커가 이 흐름의 잡을 가져가지 못하게 한다
    owner, personal, work = create_owner_context(), create_personal_agent_context(), create_work_agent_context()

    say("== A. 영어 Kubernetes 문서(정의문 없음, 대문자 표기만) -> 쿠버네티스/k8s alias 매칭")
    async with sessionmaker()() as db:
        await register_text(
            db, title="Platform Notes", text=K8S_DOC, source_key=None, lang="en",
            locator="https://Docs.Example.org/platform/notes", scope="personal",
        )
        await db.commit()
    say(f"  pipeline jobs pumped={await drain()}")
    async with sessionmaker()() as db:
        ent = await db.scalar(select(KbEntity).where(KbEntity.canonical_name == "Kubernetes"))
        say(f"  entity Kubernetes exists={ent is not None}")
        for query in ("쿠버네티스", "k8s"):
            for label, ctx in (("owner", owner), ("personal", personal)):
                matched = await find_matched_entities(db, query, terms(query), ctx)
                out = await search(db, query=query, access_context=ctx, mode="graph", limit=5)
                say(f"  q={query!r} ctx={label}: matched={[(m['canonical_name'], m['matched_by']) for m in matched]} items={len(out['items'])}")
        lexical = await search(db, query="쿠버네티스", access_context=owner, mode="lexical", limit=5)
        say(f"  q='쿠버네티스' lexical-only items={len(lexical['items'])} (한국어 본문이 없으므로 0이어야 별칭 경로 증거)")
        bodies = " ".join(await db.scalars(select(KbPassage.body))).casefold()
        extracted = [a for a, s in (await db.execute(select(KbEntityAlias.alias, KbEntityAlias.source))).all() if s == "extracted"]
        ungrounded = [a for a in extracted if a.casefold() not in bodies]
        say(f"  extracted aliases={len(extracted)} 근거 없는 extracted alias={len(ungrounded)}")
        dom = await search(db, query="Kubernetes", access_context=owner, mode="lexical", filters={"domain": "docs.example.org"})
        say(f"  domain=docs.example.org 필터 결과={len(dom['items'])} 표시 domain={dom['items'][0]['document']['domain'] if dom['items'] else None}")

    say("== B. 접근 행이 없는 work 문서(수정 전 CLI ingest 와 같은 경로) -> personal 0건")
    async with sessionmaker()() as db:
        row = await ingest_source(db, source_key="file:/work/roadmap.md", data=ROWLESS_WORK_DOC.encode(), title="Roadmap", lang="en")
        await segment_revision(db, row.revision_id)
        await db.commit()
        rowless_id = row.document_id
    async with sessionmaker()() as db:
        for label, ctx in (("personal", personal), ("work", work), ("owner", owner)):
            out = await search(db, query="Zebrafoo", access_context=ctx, mode="lexical", limit=5)
            say(f"  q='Zebrafoo' ctx={label}: items={len(out['items'])}")

    say("== C. CLI kb-ingest 기본값(--scope work) -> 접근 행 work_only, personal 0건")
    sample = Path(HOME) / "cli-note.md"
    sample.write_text("# CLI note\n\nCliquokka confidential body from the CLI ingest path.\n", encoding="utf-8")
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "ttakkari.cli", "kb-ingest", str(sample), "--source-key", "file:cli-note"],
        env=os.environ.copy(), capture_output=True, text=True, timeout=120, check=False,
    )
    info = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
    say(f"  returncode={proc.returncode} scope={info.get('scope')}")
    async with sessionmaker()() as db:
        for label, ctx in (("personal", personal), ("work", work)):
            out = await search(db, query="Cliquokka", access_context=ctx, mode="lexical", limit=5)
            say(f"  q='Cliquokka' ctx={label}: items={len(out['items'])}")

    say("== D. 백필: dry-run 은 쓰지 않고, apply 는 미상을 work_only 로 채운다(임시 컨테이너만)")
    async with sessionmaker()() as db:
        dry = await run_backfill(db, apply=False)
        say(f"  dry-run: {json.dumps(dry.as_dict(), ensure_ascii=False)[:300]}")
        applied = await run_backfill(db, apply=True)
        say(f"  apply: rows_inserted={applied.inserted}")
        from ttakkari.collector.control_models import KbDocumentAccess

        row = await db.get(KbDocumentAccess, rowless_id)
        say(f"  rowless doc access -> {(row.scope, row.origin_scope, row.visibility) if row else None}")
        n = await db.scalar(select(func.count()).select_from(KbDocumentAccess))
        say(f"  access rows total={n}")
        out = await search(db, query="Zebrafoo", access_context=personal, mode="lexical", limit=5)
        say(f"  after backfill q='Zebrafoo' ctx=personal: items={len(out['items'])}")
    say("DONE")


if __name__ == "__main__":
    asyncio.run(main())
