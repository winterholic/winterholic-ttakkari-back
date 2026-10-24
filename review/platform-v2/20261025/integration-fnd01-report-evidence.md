# FND-01 + report 병합 교정 통합 후보 증거

- 통합 브랜치: `integration/platform-v2-fnd01-report-20261025` (기준 `origin/main` = `fb746022af2f961e82cfb5ec33440369a18b05bf`)
- 병합한 원 후보: FND-01 `bf2d566243feab5845fe384d44c9579e61ebdec4`, report `a371677b7ea273bff07c0242ad8ac80d73928c09` (둘 다 `git ls-remote`로 원격 SHA 확인)
- 병합 방식: `git merge --no-ff` 2회. 충돌 없음(manager.py는 자동 3-way). 두 후보 커밋 ancestry 보존.

## 보존 확인 (코드 경로)
- `runs/manager.py`: `recover_interrupted` ALIVE/ABSENT/UNKNOWN, `resume_interrupted(force=False)`, report enqueue 잠금과 `is_owned(pending.id, db=db)` 모두 존재.
- `generation.py`: `enter_admission`/`enter_accepted_work`, claim의 ChatSession→Run 잠금·queued 재확인.
- `tests/conftest.py` 전후 runtime 청소, 0068 migration(head 단일), `TtsJob.chunks_json`, `deploy/generation-contract.json` schema_revision 0068.

## 통합 중 발견·교정 (별도 커밋 a4e7340, 필요 시 root가 분리 가능)
FND 후보 `bf2d566` 단독에서 이미 실패하던 5건(기준 917ea4b에서는 통과). 새 FND-01 계약과 옛 테스트의 충돌이며 제품 코드 결함이 아니다.
- `tests/test_api_runs.py` 4건: ALIVE PID 보존(복구 0건)·`resume_interrupted` 기본 무-replay 계약. 테스트를 ABSENT 복구 / 살아있는 PID 보존 / `force=True` 재개로 정렬.
- `unit_tests/test_creative_v2_asset_generation_e2e.py` 1건: 가짜 runtime `work()`가 `allow_draining` 키워드를 받지 못함.

## 실DB 경쟁 fixture
`tests/test_report_merge_real_db_race.py`(a371 검증 전용 fixture를 정식 tests로 편입, docstring과 미사용 변수만 정리). 정상 conftest의 격리 PostgreSQL 사용. 통합 트리에서 통과(아래).

## 실행 결과 (통합 후보, `PYTHONPATH=src:.` + `../back/.venv/bin/pytest`, uv 미사용)
- 핵심 20건 `tests/test_report_merge_real_db_race.py unit_tests/test_orchestrator_report_merge.py unit_tests/test_fnd01_foundation.py tests/test_orchestrator.py::test_reports_queued_behind_busy_orchestrator_are_merged`: `20 passed in 1.43s`
- 관련 묶음(TTS/generation/deployment/deploy_gate/bootstrap/processes/orchestrator/planning/route/migration_single_head/run_executor/api_runs/failed_runs/race + unit_tests 전체): `1 failed, 2072 passed, 1 skipped, 5014 subtests passed in 346.93s`
- 실패 1건 `unit_tests/test_platform_v2_inventory.py::test_report_schema_separates_extraction_classification_scope_coverage`: **origin/main fb74602 단독 detached worktree에서도 동일 실패**(통합 이전부터 존재). 원인: main의 planning 커밋이 추가한 MCP 도구 `manage_work`가 인벤토리 분류기에서 `unmatched`(tools collected 134 / classified 133). 이번 범위 밖이라 수정하지 않았고, 인벤토리 산출물 재생성이 동반되므로 root 판단이 필요하다.
- 환경 분류: `unit_tests/test_backup_*` 16건은 worktree에 `.venv`가 없어(`scripts/../.venv/bin/python` 없음) 실패했다. git이 무시하는 `.venv` 심볼릭 링크(→ `../back/.venv`)를 두고 재실행해 `22 passed, 1 skipped`. 제품 실패 아님.
- `alembic heads`: `0068_tts_payload_chunks (head)` 단일. ruff check: 변경 .py 전체 `All checks passed!`.

## 미검증 / 한계
- FND 전체 독립검증 worker `86d63945`는 조회 시점에 `status: running`, 결과 없음. 이 통합 후보는 그 결과를 반영하지 않는다.
- `get_worker` 도구는 이 Run에 없어 `read_session`으로 1회 조회했다.
- 운영 배포, main push, `request_backend_deploy`는 수행하지 않았다(FND 독립 passed 미확인).
- 구세대 917ea4b가 드레인 시 queued TTS를 실패 처리하는 첫 혼합 세대 전환 손실은 완전히 제거되지 않는다(30초 대기 + job.warnings는 완화일 뿐).
