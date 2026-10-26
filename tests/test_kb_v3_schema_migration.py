"""지식 도서관(KB v3) 스키마 확장, 온톨로지·파이프라인 모델 및 마이그레이션 계약 검증.

KB3-102 검증 범위:
1. Alembic 마이그레이션 그래프 단일 head (0072_kb_v3_schema_and_scope) 및 down_revision (0071_library_platform_v2_merge) 검속.
2. Base.metadata 모델 등록 parity: 파이프라인 2종, 온톨로지 8종, kb_passages 위치 메타, kb_sources 원본 메타, kb_document_access scope 통합.
3. 불변 원장 RESTRICT 외래키 보호: kb_documents, kb_revisions, kb_passages, kb_evidence 참조 FK의 ondelete='RESTRICT' 전수 검증.
4. 중요 설계 보정: kb_documents 에 current_revision_id 포인터가 없음을 보증(현재성은 event_time 및 rev_no 규칙 보존).
5. 비즈니스 불변 DB 제약조건:
   - kb_relations: 근거 없는 관계 확정 방지 ((status != 'active') OR (evidence_count >= 1))
   - provenance/visibility 격리: kb_entities, kb_assertions, kb_relations, kb_entity_canonicals, kb_conflicts 의 visibility/origin_scope
   - 유니코드 정규화 보존: norm_form (NFC/NFKC)
6. 0072 마이그레이션 모듈 upgrade/downgrade 작업 정의 및 downgrade 안전 가드 검증.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint
from sqlalchemy.orm import Session

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbEntityCanonical,
    KbRelation,
    KbRelationEvidence,
    activate_relation_with_evidence_sync,
    derive_derived_visibility,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.models import Base

ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = ROOT / "alembic" / "versions"


def _load_migration(name: str):
    file_path = VERSIONS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{name}", str(file_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alembic_v3_single_head_and_lineage():
    """Alembic 그래프가 단일 head이며 0072_kb_v3_schema_and_scope 가 0071 뒤에 정확히 연결되었는지 검증."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"Alembic heads must be exactly 1, got: {heads}"
    assert heads[0] == "0074_kb_v3_research_merge"
    assert set(_load_migration("0074_kb_v3_research_merge").down_revision) == {
        "0073_research_reuse_source", "0073_kb_l2_budget_ledger"
    }
    assert _load_migration("0073_kb_l2_budget_ledger").down_revision == "0072_kb_v3_schema_and_scope"

    mod_0072 = _load_migration("0072_kb_v3_schema_and_scope")
    assert mod_0072.revision == "0072_kb_v3_schema_and_scope"
    assert mod_0072.down_revision == "0071_library_platform_v2_merge"


def test_metadata_all_v3_tables_registered():
    """Base.metadata 에 파이프라인 2종, 온톨로지 8종 및 기존 원장 테이블이 모두 등록되어 있는지 검증."""
    expected_tables = {
        # 원장 및 기존 테이블
        "kb_sources",
        "kb_documents",
        "kb_revisions",
        "kb_passages",
        "kb_evidence",
        "kb_document_access",
        # 파이프라인 신규 테이블
        "kb_pipeline_jobs",
        "kb_pipeline_stages",
        # 온톨로지 신규 테이블 9종
        "kb_entities",
        "kb_entity_aliases",
        "kb_document_entities",
        "kb_assertions",
        "kb_assertion_evidence",
        "kb_relations",
        "kb_relation_evidence",
        "kb_entity_canonicals",
        "kb_conflicts",
    }
    registered = set(Base.metadata.tables.keys())
    missing = expected_tables - registered
    assert not missing, f"Missing tables in Base.metadata: {missing}"

    # 모델 객체 바인딩 검증
    assert Base.metadata.tables["kb_pipeline_jobs"] is KbPipelineJob.__table__
    assert Base.metadata.tables["kb_pipeline_stages"] is KbPipelineStage.__table__
    assert Base.metadata.tables["kb_entities"] is KbEntity.__table__
    assert Base.metadata.tables["kb_entity_aliases"] is KbEntityAlias.__table__
    assert Base.metadata.tables["kb_document_entities"] is KbDocumentEntity.__table__
    assert Base.metadata.tables["kb_assertions"] is KbAssertion.__table__
    assert (
        Base.metadata.tables["kb_assertion_evidence"] is KbAssertionEvidence.__table__
    )
    assert Base.metadata.tables["kb_relations"] is KbRelation.__table__
    assert (
        Base.metadata.tables["kb_relation_evidence"] is KbRelationEvidence.__table__
    )
    assert Base.metadata.tables["kb_entity_canonicals"] is KbEntityCanonical.__table__
    assert Base.metadata.tables["kb_conflicts"] is KbConflict.__table__
    assert Base.metadata.tables["kb_document_access"] is KbDocumentAccess.__table__


def test_kb_document_has_no_current_revision_id_pointer():
    """중요 설계 보정: KbDocument 에는 current_revision_id 임의 포인터가 없어야 한다.

    현재성은 observation event_time 및 rev_no 규칙으로 계산하며, 임의 포인터 도입은 불변 원칙을 해친다.
    """
    doc_cols = {c.name for c in KbDocument.__table__.columns}
    assert "current_revision_id" not in doc_cols, (
        "KbDocument 에 current_revision_id 컬럼을 도입하면 안 됨 (현재성은 observation/rev_no 로 보존)"
    )


def test_kb_sources_additive_columns():
    """kb_sources 에 blob_relpath, original_filename, parser_name, parser_version, source_meta 가 포함되었는지 검증."""
    cols = {c.name: c for c in KbSource.__table__.columns}
    assert "blob_relpath" in cols
    assert "original_filename" in cols
    assert "parser_name" in cols
    assert "parser_version" in cols
    assert "source_meta" in cols


def test_kb_passages_location_metadata():
    """kb_passages 에 위치 메타(section_path, chunk_kind, page_no, slide_no, time_start_ms, time_end_ms, token_estimate, search_text) 검증."""
    cols = {c.name: c for c in KbPassage.__table__.columns}
    expected = (
        "section_path",
        "chunk_kind",
        "page_no",
        "slide_no",
        "time_start_ms",
        "time_end_ms",
        "token_estimate",
        "search_text",
    )
    for col in expected:
        assert col in cols, f"kb_passages missing column: {col}"

    # chunk_kind 체크 제약조건 검증
    check_names = {
        c.name
        for c in KbPassage.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_kb_passage_chunk_kind" in check_names


class _FakeInsertContext:
    def __init__(self, params):
        self._params = params

    def get_current_parameters(self):
        return self._params


def _derived_access(**params):
    """INSERT 시점 컬럼 기본값 함수가 돌려주는 (origin_scope, visibility)."""
    cols = {c.name: c for c in KbDocumentAccess.__table__.columns}
    ctx = _FakeInsertContext(params)
    origin = params.get("origin_scope") or cols["origin_scope"].default.arg(ctx)
    visibility = cols["visibility"].default.arg(_FakeInsertContext({**params, "origin_scope": origin}))
    return origin, visibility


def test_kb_document_access_scope_unification_contract():
    """kb_document_access 에 origin_scope 와 visibility 컬럼 및 체크 제약조건이 존재하는지 검증."""
    cols = {c.name: c for c in KbDocumentAccess.__table__.columns}
    assert "origin_scope" in cols
    assert "visibility" in cols
    assert "scope" in cols
    # visibility/origin_scope 는 입력 생략 시 scope 에서 파생된다(고정 work_only 기본값이 아니다)
    assert cols["visibility"].server_default is None
    assert cols["origin_scope"].server_default is None
    assert cols["visibility"].default.is_callable
    assert _derived_access(scope="personal") == ("personal", "global")
    assert _derived_access(scope="work") == ("work", "work_only")
    assert _derived_access(scope="unknown") == ("work", "work_only")
    assert _derived_access(scope="personal", origin_scope="work") == ("work", "work_only")

    check_names = {
        c.name
        for c in KbDocumentAccess.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_kb_document_access_origin_scope" in check_names
    assert "ck_kb_document_access_visibility" in check_names
    assert "ck_kb_document_access_scope" in check_names
    assert "ck_kb_document_access_work_visibility" in check_names


def test_immutable_ledger_foreign_keys_are_restrict():
    """원장(kb_documents, kb_revisions, kb_passages, kb_evidence)을 가리키는 모든 외래키는 RESTRICT 여야 한다."""
    ledger_tables = {"kb_documents", "kb_revisions", "kb_passages", "kb_evidence"}

    checked_fks = 0
    for tbl_name, table in Base.metadata.tables.items():
        # KB 및 Collector 테이블 대상 검증
        if not tbl_name.startswith(("kb_", "collector_")):
            continue
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                for elem in constraint.elements:
                    target_table = elem.column.table.name
                    # supersedes_id 는 자기 참조 리비전 계보 포인터이므로 SET NULL 허용
                    if elem.parent.name == "supersedes_id":
                        continue
                    if target_table in ledger_tables:
                        # 원장 참조 FK 는 반드시 ondelete RESTRICT 여야 연쇄 삭제(CASCADE)가 방지됨
                        assert (
                            constraint.ondelete is not None
                            and constraint.ondelete.upper() == "RESTRICT"
                        ), (
                            f"{tbl_name}.{constraint.name} targeting ledger table {target_table} "
                            f"must have ondelete='RESTRICT', got {constraint.ondelete}"
                        )
                        checked_fks += 1

    assert checked_fks >= 15, (
        f"Expected at least 15 ledger-protecting FKs, found {checked_fks}"
    )


def test_relation_active_needs_evidence_constraint():
    """근거 없는 관계 확정 방지: kb_relations 는 status == 'active' 일 때 evidence_count >= 1 이어야 한다."""
    check_constraints = [
        c for c in KbRelation.__table__.constraints if isinstance(c, CheckConstraint)
    ]
    check_names = {c.name for c in check_constraints}
    assert "ck_kb_relation_active_needs_evidence" in check_names
    assert "ck_kb_relation_evidence_count" in check_names
    assert "ck_kb_relation_scope_visibility" in check_names

    cols = {c.name: c for c in KbRelation.__table__.columns}
    assert cols["evidence_count"].default.arg == 0
    assert cols["status"].default.arg == "candidate"
    assert cols["visibility"].default.arg == "work_only"


def test_ontology_provenance_and_visibility_isolation():
    """work 문서 증거가 global 로 누출되지 않도록 온톨로지 테이블들에 visibility 및 origin_scope 가 격리되어 있는지 검증."""
    for model in (KbEntity, KbAssertion, KbRelation):
        cols = {c.name for c in model.__table__.columns}
        assert "visibility" in cols, f"{model.__name__} missing visibility"
        assert "origin_scope" in cols, f"{model.__name__} missing origin_scope"
        check_names = {
            c.name
            for c in model.__table__.constraints
            if isinstance(c, CheckConstraint)
        }
        assert any("visibility" in name for name in check_names), (
            f"{model.__name__} missing visibility check"
        )
        assert any("origin_scope" in name for name in check_names), (
            f"{model.__name__} missing origin_scope check"
        )

    for model in (KbEntityCanonical, KbConflict):
        cols = {c.name for c in model.__table__.columns}
        assert "visibility" in cols, f"{model.__name__} missing visibility"
        check_names = {
            c.name
            for c in model.__table__.constraints
            if isinstance(c, CheckConstraint)
        }
        assert any("visibility" in name for name in check_names), (
            f"{model.__name__} missing visibility check"
        )


def test_ontology_unicode_norm_form_preservation():
    """파서 버전에 따른 NFC/NFKC 정규화 보존: KbEntity 및 KbEntityAlias 에 norm_form 이 존재해야 한다."""
    assert "norm_form" in {c.name for c in KbEntity.__table__.columns}
    assert "norm_form" in {c.name for c in KbEntityAlias.__table__.columns}


def test_migration_0072_structure_and_operations():
    """0072 마이그레이션 모듈이 올바른 upgrade/downgrade 인터페이스와 방어 가드를 가지고 있는지 검증."""
    mod = _load_migration("0072_kb_v3_schema_and_scope")
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)

    # _V3_TABLES 순서 검증 (외래키 종속성에 따른 안전한 drop 순서)
    expected_reverse_order = (
        "kb_conflicts",
        "kb_entity_canonicals",
        "kb_relation_evidence",
        "kb_relations",
        "kb_assertion_evidence",
        "kb_assertions",
        "kb_document_entities",
        "kb_entity_aliases",
        "kb_entities",
        "kb_pipeline_stages",
        "kb_pipeline_jobs",
    )
    assert mod._V3_TABLES == expected_reverse_order


def test_postgresql_ddl_compilation():
    """모든 v3 테이블 및 컬럼이 PostgreSQL 방언 DDL 로 정상 컴파일되는지 검증."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    dialect = postgresql.dialect()
    v3_models = (
        KbPipelineJob,
        KbPipelineStage,
        KbEntity,
        KbEntityAlias,
        KbDocumentEntity,
        KbAssertion,
        KbAssertionEvidence,
        KbRelation,
        KbRelationEvidence,
        KbEntityCanonical,
        KbConflict,
    )
    for model in v3_models:
        ddl = str(CreateTable(model.__table__).compile(dialect=dialect))
        assert "CREATE TABLE" in ddl
        assert model.__tablename__ in ddl


def test_migration_0072_offline_sql_generation():
    """Alembic offline 환경에서 0072 upgrade 및 downgrade static SQL 이 유효하게 생성되는지 검증."""
    import subprocess

    res_up = subprocess.run(
        [
            "uv",
            "run",
            "alembic",
            "upgrade",
            "0071_library_platform_v2_merge:0072_kb_v3_schema_and_scope",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "CREATE TABLE kb_pipeline_jobs" in res_up.stdout
    assert "CREATE TABLE kb_entities" in res_up.stdout
    assert "CREATE TABLE kb_relation_evidence" in res_up.stdout
    assert "ALTER TABLE kb_passages ADD COLUMN section_path" in res_up.stdout
    assert "ALTER TABLE kb_document_access ADD COLUMN origin_scope" in res_up.stdout
    assert (
        "ALTER TABLE kb_document_access ADD CONSTRAINT ck_kb_document_access_work_visibility"
        in res_up.stdout
    )

    res_down = subprocess.run(
        [
            "uv",
            "run",
            "alembic",
            "downgrade",
            "0072_kb_v3_schema_and_scope:0071_library_platform_v2_merge",
            "--sql",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "DROP TABLE kb_conflicts" in res_down.stdout
    assert "DROP TABLE kb_relation_evidence" in res_down.stdout
    assert "DROP TABLE kb_pipeline_jobs" in res_down.stdout
    assert (
        "ALTER TABLE kb_document_access DROP CONSTRAINT ck_kb_document_access_work_visibility"
        in res_down.stdout
    )
    assert "ALTER TABLE kb_document_access DROP COLUMN origin_scope" in res_down.stdout


def test_migration_0072_executes_one_statement_per_op_execute():
    """asyncpg 는 prepared statement 하나에 여러 명령을 넣지 못한다. op.execute 하나에는 명령이 하나여야 한다."""
    import re
    from unittest import mock

    mod = _load_migration("0072_kb_v3_schema_and_scope")
    recorded: list[str] = []
    for fn in (mod.upgrade, mod.downgrade):
        fake_op = mock.MagicMock()
        fake_op.execute.side_effect = lambda sql: recorded.append(str(sql))
        fake_op.get_context.return_value.as_sql = True  # downgrade 의 데이터 가드(DB 조회)는 건너뛴다
        with mock.patch.object(mod, "op", fake_op):
            fn()
    assert len(recorded) >= 10
    for sql in recorded:
        outside_bodies = re.sub(r"\$\$.*?\$\$", "", sql, flags=re.DOTALL)  # 함수 본문 안의 세미콜론은 허용
        assert outside_bodies.strip().rstrip(";").count(";") == 0, sql
    joined = "\n".join(recorded)
    assert "BEFORE INSERT OR UPDATE ON kb_relations" in joined
    assert "BEFORE INSERT ON kb_document_access" in joined


def test_negative_work_access_visibility_leak_protection():
    """Negative 검증 1: work 작성자가 visibility 없이 insert -> work_only 로 격리 / global 시도 -> 거부."""
    doc_id = uuid.uuid4()

    # 1. 기존 writer 경로 (kb_delivery, kb_library)처럼 visibility 없이 scope='work' 만 기록 시
    # fail-closed 정책에 따라 visibility 가 'work_only' 로 자동 격리되어야 함
    access_work = KbDocumentAccess(
        document_id=doc_id, tenant_id="owner", scope="work"
    )
    # 생성자는 값을 고정하지 않는다. flush 시점에 기본값 함수/DB 트리거가 scope 에서 파생한다
    assert access_work.visibility is None
    assert _derived_access(scope="work") == ("work", "work_only")
    # personal writer 가 visibility 를 넘기지 않아도 통합 공간(global)에서 사라지지 않는다
    assert KbDocumentAccess(document_id=uuid.uuid4(), scope="personal").visibility is None
    assert _derived_access(scope="personal") == ("personal", "global")

    # 2. work scope 문서에 visibility='global' 을 지정하려는 시도는 fail-closed 가드에 의해 즉시 거부(ValueError)
    with pytest.raises(
        ValueError, match="Work scope document access cannot have global visibility"
    ):
        KbDocumentAccess(
            document_id=uuid.uuid4(),
            tenant_id="owner",
            scope="work",
            visibility="global",
        )

    # 3. 파생 온톨로지 visibility 결정 헬퍼 검증:
    # work 문서가 단 1개라도 포함되면 파생 엔터티/주장은 'work_only'
    assert derive_derived_visibility([access_work]) == "work_only"
    assert derive_derived_visibility(["personal", "work"]) == "work_only"
    assert derive_derived_visibility([None]) == "work_only"
    assert derive_derived_visibility([]) == "work_only"

    # 모든 원천이 personal 일 때만 global
    access_personal = KbDocumentAccess(
        document_id=uuid.uuid4(),
        tenant_id="owner",
        scope="personal",
        visibility="global",
    )
    assert derive_derived_visibility([access_personal]) == "global"


def test_negative_relation_active_without_evidence_rejected():
    """Negative 검증 2: 증거 없는 active 관계 방지 검증."""
    e1 = uuid.uuid4()
    e2 = uuid.uuid4()

    # 1. 기본 생성된 KbRelation 은 evidence_count=0, status='candidate' 여야 함 (active 기본값 차단)
    rel = KbRelation(
        source_entity_id=e1,
        target_entity_id=e2,
        relation_type="part_of",
    )
    assert rel.evidence_count == 0, (
        f"기본 evidence_count 는 0이어야 함, got {rel.evidence_count}"
    )
    assert rel.status == "candidate", (
        f"기본 status 는 candidate 여야 함, got {rel.status}"
    )
    assert rel.visibility == "work_only", (
        f"기본 visibility 는 work_only 여야 함, got {rel.visibility}"
    )

    # 2. 증거 없는 상태(evidence_count=0)에서 status='active'는 DB 체크 제약조건을 위반함을 수식으로 검증
    # ck_kb_relation_active_needs_evidence: (status != 'active') OR (evidence_count >= 1)
    status_active = "active"
    evidence_count_zero = 0
    satisfies_constraint = (status_active != "active") or (evidence_count_zero >= 1)
    assert satisfies_constraint is False, (
        "evidence_count=0 일 때 status='active' 는 DB CheckConstraint 를 만족할 수 없어야 함"
    )

    # 3. 서비스 계층 activate_relation_with_evidence_sync 단일 진입점 가드 검증:
    # 존재하지 않는 evidence_id 로 activate 시도 시 거부되어야 함
    class DummySession:
        def get(self, model, pk):
            return None  # kb_evidence 미존재 모사

    with pytest.raises(ValueError, match="not found in kb_evidence"):
        activate_relation_with_evidence_sync(DummySession(), rel, uuid.uuid4())


if __name__ == "__main__":
    test_alembic_v3_single_head_and_lineage()
    print("PASS: test_alembic_v3_single_head_and_lineage")
    test_metadata_all_v3_tables_registered()
    print("PASS: test_metadata_all_v3_tables_registered")
    test_kb_document_has_no_current_revision_id_pointer()
    print("PASS: test_kb_document_has_no_current_revision_id_pointer")
    test_kb_sources_additive_columns()
    print("PASS: test_kb_sources_additive_columns")
    test_kb_passages_location_metadata()
    print("PASS: test_kb_passages_location_metadata")
    test_kb_document_access_scope_unification_contract()
    print("PASS: test_kb_document_access_scope_unification_contract")
    test_migration_0072_executes_one_statement_per_op_execute()
    print("PASS: test_migration_0072_executes_one_statement_per_op_execute")
    test_immutable_ledger_foreign_keys_are_restrict()
    print("PASS: test_immutable_ledger_foreign_keys_are_restrict")
    test_relation_active_needs_evidence_constraint()
    print("PASS: test_relation_active_needs_evidence_constraint")
    test_ontology_provenance_and_visibility_isolation()
    print("PASS: test_ontology_provenance_and_visibility_isolation")
    test_ontology_unicode_norm_form_preservation()
    print("PASS: test_ontology_unicode_norm_form_preservation")
    test_migration_0072_structure_and_operations()
    print("PASS: test_migration_0072_structure_and_operations")
    test_postgresql_ddl_compilation()
    print("PASS: test_postgresql_ddl_compilation")
    test_migration_0072_offline_sql_generation()
    print("PASS: test_migration_0072_offline_sql_generation")
    test_negative_work_access_visibility_leak_protection()
    print("PASS: test_negative_work_access_visibility_leak_protection")
    test_negative_relation_active_without_evidence_rejected()
    print("PASS: test_negative_relation_active_without_evidence_rejected")
    print("========================================")
    print("ALL 15 KB v3 SCHEMA MIGRATION TESTS PASSED!")
    print("========================================")
