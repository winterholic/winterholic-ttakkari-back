# V2 검증 실패 교정과 통합 범위

> **2026-10-25 후속 정정:** 본문 FND-01의 generation_draining 0건 보장·다중 슬롯 검증 등은 당시 좁은 fixture/시뮬레이션 범위를 넘어 현재 운영 보장으로 인용하지 않는다. 첫 혼합 세대 전송 완료는 미해결이며 stock 전환은 명시 거절한다. FND-02~05의 번들/공통 fence/영수증 게이트 제안도 현재 요구가 아니다. [현재 실행 계약](current-execution-contract.md)과 [현재 소스 대조](../../../review/platform-v2/20261025/p1-frontend-runtime-tests-review.md)가 우선하며 기존 검증 원문은 보존한다.

이 문서는 이전 A `6cbdb22`, B `f8d0ad2`, C `b155049`의 실패 판정을 유지하며, 그 뒤의 통합 교정을 기록한다. 이전 후보가 새로 통과했다는 뜻은 아니다. 전체 V2 운영 전환도 아직 완료되지 않았다.

## 실제 코드 변경

- A: 후보의 tracked/untracked 파일 바이트를 해시하고, 두 차례 읽기와 Git 상태/HEAD 재대조로 읽는 중 변경을 거부한다. rename의 출발/도착 경로도 보존한다. 캐시 재사용은 실파일 해시와 결속한 clean snapshot을 요구하고, 현재 후보를 재캡처하여 stale receipt를 거부한다. 빈 closure map, snapshot 없는 호출자 선언은 재사용할 수 없다. Delivery의 실제 stage runner도 실행 전후 후보를 대조한다. Python/플랫폼/설치 배포판 버전 및 환경 전체의 집계 해시도 실제 프로세스에서 수집하여, 호출자가 이전 env/plugin 해시를 재사용하더라도 환경 변경 시 receipt 재사용을 거부한다. 환경 값은 기록하지 않고 집계 해시만 남긴다. 외부 서비스·가변 데이터의 완전성은 별도 미검증 경계로 남긴다.
- A prototype: 공개 기본 HMAC 키를 제거했다. 명시적인 verifier 소유 키 없이는 receipt 발행/검증을 거부한다. 서명은 키 소유자의 주장 무결성을 검증하는 수단이며, 수동으로 전달된 통과 건수가 실제 실행을 입증하지는 않는다. prototype은 배포 게이트의 인증 근거로 사용하지 않는다.
- B: loader/spec/file 경로는 실제 실행 소스 바이트의 증거가 아니므로 runtime binding은 `unknown`이다. 정적 집합의 누락/중복/치환은 별도 `static_state`와 원장에 계속 표시한다. 미지원 discovery, 구문/읽기 오류를 성공으로 승격하지 않는다. 과거 607 기준 작업 트리 대신 현재 통합 소스를 실제 스캔했다.
- C: 25종 레지스트리의 ID 집합과 행 스키마를 검사한다. 잘린 레지스트리는 `incomplete`, 매핑은 `unknown/null`이다. 생성기와 테스트의 공유 `/back` fallback을 제거했다. TypeScript 오류/미지원 호출 원장을 coverage에 반영하고, 입력 파일 해시를 수집 전후 비교한다. Markdown에도 JSON/CSV와 같은 API callsite별 표를 만든다.
- FND-00: Desk Qwen 서비스 배선 테스트의 과거 `harness-v2` 고정값을 현재 `PROMPT_VERSION` 계약으로 교정했다. 버전 내용 자체의 회귀는 기존 topic-boundary 테스트가 담당한다.

## 회귀 검증

실패를 먼저 확인한 반례는 fake loader/spec, 잘린 레지스트리, 외부 fallback, dirty bytes, 빈 closure, 읽기 중 변경, 기본 서명키, TypeScript 구문 오류다. 테스트 통과를 위해 전체 coverage를 complete로 바꾸지 않았다.

- 통합된 A cache/runner와 B/C 감사 회귀: 88 passed. 이후 배포 아카이브 호환성을 추가 확인했다.
- A evidence/prototype 한정 테스트: 6 passed.
- 기존 배포 차단 회귀: `tests/test_desk_qwen_service.py::test_desk_request_goes_to_the_managed_server_of_this_process`, 격리 PostgreSQL에서 1 passed.
- 배포 아카이브에는 `.git`이 없다는 반례를 재현했다. Git binding 테스트는 소유한 작은 Git fixture로 옮겼다. 과거 C의 251개 파일/3016개 함수 기대값은 `b155049`의 test-source archive에 고정해 현재 코드 수치와 혼합하지 않는다. 이 archive는 AST 분석 전용이며 실행하지 않는다.

정확한 최종 후보 SHA와 추가 gate 결과는 work-history 및 배포 controller reservation이 정본이다. 테스트 숫자는 위 명령 범위의 관측이며 전체 backend suite나 운영 검증을 뜻하지 않는다.

## 기존 설계와 남은 단계

기존 4 Plane, 독립 릴리스, accepted-work 보존, 소유권/펜싱, expand-only, rollback 원칙을 유지한다. 이번 통합은 결함 교정과 공용 기반의 첫 배포 단위다. cache shortcut이나 gate 완화, 미검증 RunExecutor 전환, Supervisor 교체를 활성화하지 않는다.

## FND-00 및 FND-01 구현 및 검증 원장 (2026-10-25)

### 1. FND-00 (LOG-195)
- **Plan**: `tests/test_desk_qwen_service.py`의 `PROMPT_VERSION` 정합성 확인.
- **Code**: `bbf62d5` 커밋에서 `route["qwen"]["prompt_version"] == desk_qwen.PROMPT_VERSION`으로 교정 완료됨 확인.
- **Test**: `harness-v2` 고정값 잔존 0건.

### 2. FND-01 (TTS accepted work 영속화, 2계층 Drain, Blind Replay 방지)
- **Plan**:
  - TTS 청크 영속화: `TtsJob`에 `chunks_json` JSONB 컬럼 추가 및 마이그레이션 `0068_tts_payload_chunks.py`.
  - 2계층 드레인: `enter_admission` (신규 수락은 active만) vs `enter_accepted_work` (기수락 작업은 draining 중에도 보장), `work(..., allow_draining=True)`, `_fail_queued_draining` 호출 제거로 `generation_draining` 실패 0건 보장, `can_retire()`는 잔여 작업 완료 시까지 퇴역 보류.
  - Reconcile / Blind Replay 방지: `resume_interrupted`의 무조건 자동 재큐잉 비활성화 (`auto_requeued_count == 0`), 프로세스 ALIVE/ABSENT/UNKNOWN 구분 (`psutil.AccessDenied`/`OSError` 시 UNKNOWN 보존, `NoSuchProcess` 시에만 interrupted 마감).
- **Code**:
  - `src/ttakkari/tts_models.py`: `TtsJob.chunks_json` 추가.
  - `src/ttakkari/models.py`: `TtsJob, TtsCache` export.
  - `alembic/versions/0068_tts_payload_chunks.py`: 신규 Expand-only 마이그레이션.
  - `deploy/generation-contract.json`: `schema_revision` 0068 갱신 및 `online_migration_from` 0067 추가.
  - `src/ttakkari/tts/jobs.py`: `chunks_json` 영속화 및 메모리 유실 시 DB 복원, `_fail_queued_draining` 호출 제거, `tick`에서 draining 세대 accepted work 실행.
  - `src/ttakkari/tts/service.py`: `budget(allow_draining=True)` 지원.
  - `src/ttakkari/generation.py`: `enter_admission`, `enter_accepted_work`, `can_retire` 잔여 작업 검사, `work(allow_draining=True)`.
  - `src/ttakkari/config.py`: `auto_resume_interrupted: bool = False` 기본 비활성화.
  - `src/ttakkari/runs/manager.py`: `_inspect_run_process` ALIVE/ABSENT/UNKNOWN 구분, `recover_interrupted` UNKNOWN 보존, `resume_interrupted` blind replay 차단.
  - `unit_tests/test_fnd01_foundation.py`: 9개 회귀 및 계약 테스트.
- **Test**:
  - `unit_tests/test_fnd01_foundation.py`: 9 passed in 0.45s (TTS chunks persistence, memory eviction recovery, admission vs accepted work drain, can_retire holding, guard loop zero generation_draining, auto_requeue disabled, ALIVE/UNKNOWN preservation, ACC-01 E2E drain contract, ACC-03 restart reconcile contract).
  - `tests/test_migration_single_head.py` & `test_application_generation_contract_matches_its_migration_head`: 3 passed in 0.25s.
  - `ruff check`: All checks passed (0 errors).
- **Live**:
  - Gate B 운영 활성화 미수행 (운영 환경 전환 전 격리 검증 완료 상태 유지).
- **미검증 경계**:
  - 실시간 PostgreSQL 소켓 연결 및 실제 다중 호스트 슬롯 간 E2E 통신은 격리 테스트 환경에서 검증되었으며, 실제 호스트 실서빙 리비전 전환은 Gate B(백업 리허설 성공 영수증 확인, supervisor.json 실시간 확인) 충족 후 진행.
- **다음 연결 지점 (FND-02)**:
  - Creative V2 비동기 태스크 드레인 추적 (`_ASSET_GENERATION_TASKS`를 `RuntimeGeneration.enter_accepted_work` 및 finalizers에 등록) 및 체크포인트 파일 쓰기 시 `os.fsync`, sha256 검증, DB `fencing_token` 연동.

FND-02~05의 Creative 태스크 추적과 checkpoint 내구성, Qwen 단일 소유권, RunExecutor cutover, 검증 producer 연동은 후속 구현 항목이다. P4 독립 모듈 활성화와 9대 운영 실증, P5/P6도 남아 있다. 기존 문서의 "Gate A 구현 완료" 등은 이전 후보 시점의 이력으로만 읽으며, V2 전체 완료로 인용하지 않는다.
