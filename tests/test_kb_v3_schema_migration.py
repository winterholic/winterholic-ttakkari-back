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
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_models import KbDocument, KbPassage, KbSource
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbEntityCanonical,
    KbRelation,
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
    assert heads[0] == "0072_kb_v3_schema_and_scope"

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
        # 온톨로지 신규 테이블 8종
        "kb_entities",
        "kb_entity_aliases",
        "kb_document_entities",
        "kb_assertions",
        "kb_assertion_evidence",
        "kb_relations",
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


def test_kb_document_access_scope_unification_contract():
    """kb_document_access 에 origin_scope 와 visibility 컬럼 및 체크 제약조건이 존재하는지 검증."""
    cols = {c.name: c for c in KbDocumentAccess.__table__.columns}
    assert "origin_scope" in cols
    assert "visibility" in cols
    assert "scope" in cols

    check_names = {
        c.name
        for c in KbDocumentAccess.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_kb_document_access_origin_scope" in check_names
    assert "ck_kb_document_access_visibility" in check_names
    assert "ck_kb_document_access_scope" in check_names


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
    assert "ALTER TABLE kb_passages ADD COLUMN section_path" in res_up.stdout
    assert "ALTER TABLE kb_document_access ADD COLUMN origin_scope" in res_up.stdout

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
    assert "DROP TABLE kb_pipeline_jobs" in res_down.stdout
    assert "ALTER TABLE kb_document_access DROP COLUMN origin_scope" in res_down.stdout


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
    print("========================================")
    print("ALL 13 KB v3 SCHEMA MIGRATION TESTS PASSED!")
    print("========================================")
