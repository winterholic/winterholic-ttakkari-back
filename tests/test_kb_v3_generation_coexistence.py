"""세대 배포 공존 증명: 0071 세대 코드의 writer 가 0072/0073 스키마에서 계속 통과해야 한다.

deploy/generation-contract.json 은 0071/0072 를 online_migration_from 에 둔다. 새 세대 마이그레이션은 구 세대가
서비스 중일 때 적용되므로, 구 세대의 INSERT/UPDATE 모양(origin_scope·visibility·새 컬럼을 넘기지 않는다)이 head 스키마에서
거부되면 계약이 거짓이다. 여기서는 0071 코드가 실제로 내는 SQL 모양을 그대로 재현한다.
실제 0071 코드 체크아웃으로 돌리는 증명은 scripts/kb_v3_generation_coexistence_proof.py 다.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from test_kb_v3_schema_realdb import HEAD, Db, db  # noqa: F401 - db 는 모듈 범위 Docker 픽스처

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[1]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_contract_declares_head_and_both_intermediate_revisions_online():
    contract = json.loads((ROOT / "deploy/generation-contract.json").read_text())
    assert contract["schema_revision"] == HEAD
    assert {"0071_library_platform_v2_merge", "0072_kb_v3_schema_and_scope"} <= set(contract["online_migration_from"])
    assert HEAD not in contract["online_migration_from"]


def test_old_generation_writers_pass_on_head_schema(db: Db):  # noqa: F811
    db.alembic_ok("upgrade", "0071_library_platform_v2_merge")
    assert db.version() == "0071_library_platform_v2_merge"
    # 0071 시점에 이미 있던 문서(신규 컬럼 이전의 행)
    legacy = uuid.uuid4()
    db.execute(
        "INSERT INTO kb_documents (id, doc_key, title, lang, created_at, updated_at) VALUES ($1, 'legacy:coexist', 'legacy', 'ko', now(), now())",
        legacy,
    )
    db.execute("INSERT INTO kb_document_access (document_id, tenant_id, scope, hidden) VALUES ($1, 'owner', 'personal', false)", legacy)

    db.alembic_ok("upgrade", "head")
    assert db.version() == HEAD

    # 0071 writer 모양 1: personal/work 접근 행(scope 만 넘김). BEFORE INSERT 트리거가 파생해야 한다.
    personal, work = uuid.uuid4(), uuid.uuid4()
    for doc, key in ((personal, "old:personal"), (work, "old:work")):
        db.execute(
            "INSERT INTO kb_documents (id, doc_key, title, lang, created_at, updated_at) VALUES ($1, $2, $2, 'ko', now(), now())",
            doc, key,
        )
    db.execute("INSERT INTO kb_document_access (document_id, tenant_id, scope, hidden) VALUES ($1, 'owner', 'personal', false)", personal)
    db.execute("INSERT INTO kb_document_access (document_id, tenant_id, scope, hidden) VALUES ($1, 'owner', 'work', false)", work)
    rows = {
        r["document_id"]: (r["origin_scope"], r["visibility"])
        for r in db.fetch("SELECT document_id, origin_scope, visibility FROM kb_document_access")
    }
    assert rows[personal] == ("personal", "global")
    assert rows[work] == ("work", "work_only")
    assert rows[legacy] == ("personal", "global"), "0072 백필이 기존 행을 채웠다"

    # 모양 2: 구 writer 의 소스·리비전·패시지 INSERT(신규 컬럼 없음, 서버 기본값으로 채워진다)
    body = "구 세대 본문\n두 번째 줄"
    source, rev = uuid.uuid4(), uuid.uuid4()
    db.execute(
        "INSERT INTO kb_sources (id, source_key, kind, locator, sha256, byte_size, media_type, store_path, collected_at, origin_meta, created_at) "
        "VALUES ($1, 'old:personal', 'upload', null, $2, 10, 'text/plain', 'aa/bb/x', now(), '{}', now())",
        source, _sha(body),
    )
    db.execute(
        "INSERT INTO kb_revisions (id, document_id, source_id, rev_no, parser_version, body, text_sha256, char_count, created_at) "
        "VALUES ($1, $2, $3, 1, 'raw-v1', $4, $5, $6, now())",
        rev, personal, source, body, _sha(body), len(body),
    )
    passage = uuid.uuid4()
    db.execute(
        "INSERT INTO kb_passages (id, revision_id, ord, heading, body, char_start, char_end, sha256, created_at) "
        "VALUES ($1, $2, 0, null, $3, 0, $4, $5, now())",
        passage, rev, body, len(body), _sha(body),
    )
    defaults = db.fetch("SELECT section_path, chunk_kind, token_estimate, search_text FROM kb_passages WHERE id = $1", passage)[0]
    assert (list(defaults["section_path"]), defaults["chunk_kind"], defaults["token_estimate"], defaults["search_text"]) == ([], "prose", 0, "")

    # 모양 3: 구 writer 의 UPDATE(hidden 토글, collect_control 정책 철회)
    db.execute("UPDATE kb_document_access SET hidden = true WHERE document_id = ANY($1::uuid[])", [personal, work])
    db.execute("UPDATE kb_document_access SET hidden = false WHERE document_id = $1", personal)

    # 모양 4: 새 세대가 잡을 붙인 문서에 구 세대가 다음 리비전을 쌓는다(잡 FK 는 삽입을 막지 않는다)
    job = uuid.uuid4()
    db.execute("INSERT INTO kb_pipeline_jobs (id, document_id, revision_id) VALUES ($1, $2, $3)", job, personal, rev)
    body2 = body + "\n세 번째 줄"
    rev2 = uuid.uuid4()
    db.execute(
        "INSERT INTO kb_revisions (id, document_id, source_id, rev_no, parser_version, body, text_sha256, char_count, supersedes_id, created_at) "
        "VALUES ($1, $2, $3, 2, 'raw-v1', $4, $5, $6, $7, now())",
        rev2, personal, source, body2, _sha(body2), len(body2), rev,
    )
    assert db.scalar("SELECT count(*) FROM kb_revisions WHERE document_id = $1", personal) == 2

    # 반례: 구 writer 가 work 범위 문서를 global 로 만들 방법이 없다(visibility 를 넘기지 않으면 work_only, 넘겨도 거부)
    work2 = uuid.uuid4()
    db.execute("INSERT INTO kb_documents (id, doc_key, title, lang, created_at, updated_at) VALUES ($1, 'old:work2', 'w', 'ko', now(), now())", work2)
    db.rejected("INSERT INTO kb_document_access (document_id, tenant_id, scope, visibility, hidden) VALUES ($1, 'owner', 'work', 'global', false)", work2)
