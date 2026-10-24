# 운영 main d56455c8 · V2 통합

2026-10-25 root GPT Sol / Codex command sandbox. 시작 `90137190e03866356efb10af3dcd1f3ee581544e`, 통합한 main `d56455c816cf286e3cf367d083f3affcb45b00fe`. `git ls-remote origin refs/heads/main`도 같은 full SHA였다. 통합 브랜치에서만 작업했고 main이나 운영 DB는 변경하지 않았다.

## 결과와 선택

운영 main의 보관함 분류·보관 근거·도구 설명 변경 9커밋을 병합했다. 충돌은 `deploy/generation-contract.json` 하나였고, 보관함 변경과 V2 기능·manifest의 disabled 활성화 상태를 함께 유지했다.

병합 직후 Alembic 끝점은 `0068_library_intent`, `0070_background_consumer_queue` 두 개였다. 기존에 공개된 마이그레이션의 부모를 다시 쓰지 않고 `0071_library_platform_v2_merge`를 추가했다. 두 부모의 적용이 끝나야 새 head로 간다. [Alembic의 표준 merge 방식](https://alembic.sqlalchemy.org/en/latest/branches.html#merging-branches)을 사용했으며 자체 DDL은 없다. 계약의 schema_revision을 새 head에 맞추고 두 부모를 online_migration_from에 추가했다.

오프라인 SQL에서 `0068_library_intent → head`는 TTS·게임·NotebookLM·색인 컬럼 6개를 추가하며, 보관함 컬럼·백업 테이블·데이터 이동을 다시 처리하지 않았다. **실제 PostgreSQL에 실행한 결과는 아니다.**

주의: merge head를 한쪽 부모(`0068_library_intent`)로 downgrade하면 다른 분기의 스키마와 `0070_background_consumer_queue` 버전 행이 남는다. 이것을 main 스키마로 완전히 복원한 결과라고 쓰지 않는다. 런타임 롤백은 확장 컬럼을 유지하지만, 수동 DB downgrade 뒤의 재배포에서는 버전 행을 정합화해야 한다.

이와 연결해 `schema_plan()`이 버전 행 하나를 임의로 읽던 것을 고쳤다. 여러 행이면 순서에 관계없이 `schema_contract` 오류로 배포를 거부한다. 한 행·빈 테이블·알려진 이전 버전·unknown 버전의 기존 current/online/idle 정책은 유지했다.

## 실행 증거

공통 prefix: `PYTHONPATH=src:. ../back/.venv/bin/pytest -q -p no:cacheprovider --noconftest`.

- 새 그래프/online 계약 테스트 두 건: 교정 전 **2 failed in 0.30s**.
- 여러 DB head를 첫 행으로 오판하는 세 반례: 교정 전 **3 failed in 0.25s**.
- `unit_tests/test_executor_migration_contract.py unit_tests/test_schema_head_admission.py unit_tests/test_collector_migration_chain.py unit_tests/test_artifact_delivery_guidance.py unit_tests/test_tts_first_transition_failure_recovery.py unit_tests/test_generation_hardening.py unit_tests/test_residual_drain_supervision.py unit_tests/test_remote_api_residual_execution.py`: **80 passed in 10.58s**. 실제 Alembic offline upgrade/downgrade SQL 생성과 새 단일 head 검사 포함.
- `unit_tests/test_tts_claim_stop_race.py unit_tests/test_executor_integrated_drain.py unit_tests/test_executor_safety.py unit_tests/test_run_executor_drain.py unit_tests/test_run_executor_services.py unit_tests/test_executor_queued_consumers.py unit_tests/test_creative_v2_asset_generation_e2e.py unit_tests/test_context_index_queue.py unit_tests/test_local_api_retirement.py unit_tests/test_schema_head_admission.py`: **83 passed in 46.59s**. 위 묶음과 겹치는 파일이 있어 합산 수치로 보고하지 않는다.
- `tests/test_generation_deployment.py::test_schema_plan_classifies_stamp`: **4 passed in 0.23s**. 기존 fake DB에 ScalarResult에 해당하는 scalars 계약을 추가했으며 기존 기대값은 바꾸지 않았다.
- 변경한 Python 4파일 Ruff `All checks passed!`, staged/unstaged `git diff --check` exit 0.

### 검사 실행 범위 교정

기존 deployment 파일을 넓은 `-k 'schema_plan or prepare or online or idle or migration_head' --noconftest`로 골랐을 때는 **2 failed, 11 passed, 1 error**였다. 두 실패는 DB fixture를 빼고 선택한 경로가 기본 DB에 연결해 `InvalidPasswordError`를 받은 것이고, error는 `make_git_dir` fixture 부재다. DB 테스트 통과 증거로 계산하지 않는다. 해당 DB 연결을 재시도하지 않고 위의 정확한 DB-free schema 노드 네 건으로 범위를 바로잡았다. 제품의 운영 인증 장애라고 해석하지 않는다.

인벤토리 scanner는 `main(argv)`가 argv를 무시해 `--help`에도 스캔·파일 생성을 실행했다. 임시 수집은 routes581/tools135/tables106/migrations72/configs232/source_files391, coverage_partially_unverified였다. source는 dirty 상태이고 새 merge 분류도 unknown이어서 새 완료 증거로 쓰지 않았다. 이 호출이 만든 12개 generated 문서만 HEAD로 되돌렸고, 수동 작성문이나 main 병합 코드는 보존했다. 다음 정식 인벤토리는 이 CLI 특성을 알고 명시적으로 실행해야 한다.

## 남은 검증

FND1 전송 계층 반례는 아직 닫히지 않았다. 기존 prototype e522187을 이 병합에 포함하지 않았다. 실제 PostgreSQL base→head 왕복·rollback 데이터 복원·DB migration9건·새 library API와 V2의 DB 통합·전체 gate·운영 Run/모델/MCP/재개 검증도 미실행이다. 기존 initdb의 `shmget Operation not permitted`를 반복하지 않았다. 이번 단위/SQL 생성 결과로 운영 배포를 승인하지 않는다.
