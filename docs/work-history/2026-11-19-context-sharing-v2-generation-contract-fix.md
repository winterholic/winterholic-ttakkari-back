# Context Sharing v2 generation 계약의 0080 head 반영

## 범위와 기준

작업 브랜치 `back-context-sharing-v2-20261119`에서 generation 계약과 현재 head/계보 검사만 교정했다. 제품 코드, migration 구현, generation controller, 운영 DB, 배포, push, 공개 활성화는 변경하거나 실행하지 않았다. 시작 시 별도 수정 상태였던 `docs/work-history/2026-11-19-context-sharing-v2-lead.md`는 유지했다.

## 코드 근거와 판단

`alembic/versions/0080_ctx_share_v2_keys.py`의 revision은 `0080_ctx_share_v2_keys`, parent는 `0079_context_sharing`이다. DDL은 기존 `ctx_share_projects` 및 `ctx_share_keys`에 컬럼만 추가한다. nullable인 `revoke_error`, `pinned_snapshot_id`, `last_used_at`은 기존 행에 NULL을 허용한다. NOT NULL 컬럼에는 상수 server default(`active`, `single_use_v1`, `latest`, `'[]'::jsonb`)가 있어 구 애플리케이션이 INSERT에서 이 컬럼들을 생략해도 신규 행이 유효하다. 기존 키는 `single_use_v1` 기본값으로 보존되고 기존 project/key 컬럼·인덱스·제약은 제거 또는 재정의되지 않는다. 마지막 scopes UPDATE는 NULL만 대상으로 하며 default/backfill의 빈 JSON 배열 의미와 일치한다.

따라서 0079 코드가 배포된 상태에서 적용 가능한 additive online 확장으로 판단했다. 계약의 `schema_revision`은 실제 단일 head 0080으로 올리고, 이전 세대가 인식할 online migration 범위에는 0080의 parent인 0079를 추가했다. 현재 0080 자체는 online predecessor 목록에 넣지 않는다.

## 검사 교정

`tests/test_collection_combined_migration_contract.py`에서 계약 head를 0079에 고정하던 검사를 0080 migration revision으로 연결하고, 0080→0079 계보 및 parent의 online 허용을 함께 확인한다. `tests/test_kb_v3_schema_migration.py`의 단일 head 검사는 0080 및 parent 0079를 확인하도록 바꿨다. `tests/test_kb_v3_schema_realdb.py`의 현재 head 설정을 0080으로 갱신했다. 역사적 0079 migration 자체를 시험하는 테스트와 이전 세대 동작을 구성하는 fixture는 수정하지 않았다. 기존 KB lineage, 보안 assertion은 제거하거나 완화하지 않았다.

## 검증

실행 결과는 작업 종료 보고의 명령/출력과 대조한다. 지정 DB는 `test_cg_v2_private_20261119_b9f28799`, 포트는 5432다. 운영 DB 작업은 없다.

## 실행 결과와 영향 범위

- RED: combined contract 검사에서 parent 기대를 임시로 `0078_repo_appraisals`로 바꾸자 `AssertionError`가 발생했고 실제 parent `0079_context_sharing`을 출력했다. 임시 변경을 원복했다.
- 지정 DB/포트에서 generation head + online chain 두 검사와 combined contract 및 KB schema migration 파일: `21 passed in 1.93s`; DB reset 보고는 `21 selective`, `0 forced full`, `0 fell back to full`.
- 지정 DB/포트 환경에서 KB realdb: `1 passed in 9.80s`. 이 검사는 Docker PostgreSQL 격리 DB에 migration을 적용해 현재 HEAD 상수 0080을 확인했다.
- 변경 Python 3개 Ruff: `All checks passed!`; `git diff --check`: exit 0.
- 0079 참조 영향 검사에서 `tests/test_context_sharing_migration.py`는 명시적으로 0079 migration 자체를 시험하고, `tests/test_collection_scope_merge.py` 및 `tests/test_kb_v3_collection_access_delta.py`는 구 generation fixture version을 사용한다. 이 과거 대상은 유지했다. 현재 head 검사는 KB schema 테스트의 실제 Alembic single-head assertion과 realdb `HEAD` 설정을 갱신했다.
- 작업 중 최초 status 이후 `docs/work-history/2026-11-19-context-sharing-v2-private-verification.md`가 추가로 수정 상태로 나타났다. 다른 작업과의 동시 변경으로 보고 이 파일은 읽거나 편집하지 않았다. 최초부터 dirty였던 `docs/work-history/2026-11-19-context-sharing-v2-lead.md`와 `tests/test_context_sharing_v2_private_acceptance.py`도 변경하지 않았다.
