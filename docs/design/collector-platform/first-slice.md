# 다음 구현 첫 패키지 — CP-S2-01 계약 검증과 멱등 등록

준비 상태: 아래 "파일과 순서"는 설계 시점 계획이다. 구현은 CP-S2-01 worker가 수행했고 독립 검증 FAILED 지적을 반영한 재작업이 끝났다(PMS review, 독립 재검증 대기). PMS 정확한 UUID는 wbs.csv의 CP-S2-01.

## 재작업 후 현재 상태 (2026-10-20)

- 실제 파일: `src/ttakkari/collector/{contracts,intake}.py`, `src/ttakkari/collector_models.py`, `alembic/versions/0059_collector_intake.py`(0058_kb_immutability_truncate 다음), `unit_tests/test_collector_contracts.py`, `tests/test_collector_intake.py`, `tests/test_collector_migration.py`. 표의 `<new>` revision은 0059다(0058은 KB 세션의 `0058_kb_immutability_truncate`).
- 테스트는 표준 pytest와 `tests/conftest.py`의 일회용 PostgreSQL DB(`ttakkari_test_<pid>_<hex>`)를 쓴다. SQLite/scratch 러너는 없다.
- **HTTP 경계는 S2-02로 이동했다.** HTTP 라우트, 인증 주체→`AuthContext`(tenant/scope/principal) 산출, HTTP 403/409/422 상태 코드 테스트는 CP-S2-02가 맡는다. 이유: 코드베이스에 tenant 개념이 없어 인증 주체 매핑을 새로 정해야 하고 `main.py` 공유 접점을 건드린다. S2-01은 서비스 `submit_envelope`와 예외의 `http_status`/`error_code`(422 `invalid_envelope`, 403 `scope_binding_mismatch`, 409 `idempotency_conflict`/`envelope_id_conflict`)까지 제공해 S2-02 라우트가 얇은 어댑터가 되게 한다. PMS·wbs.csv·check_delivery.py가 같은 경계를 쓴다.
- outbox 유일키는 `(tenant_id, sink, scope, envelope_id)`다. 다른 tenant의 같은 envelope_id는 독립이고, 같은 tenant/scope에서 다른 request key로 같은 envelope_id가 오면 409다.
- 기록: [intake-binding.md](intake-binding.md) 4절, `reports/collector/s2-01/verification.txt`, `reports/collector/s2-00/inventory.json`(v2.1.0).

## 결과와 범위

인증 주체에서 산출한 tenant/scope와 source key, request key가 같은 envelope를 두 번 제출하면 같은 job ID를 돌려주고 다른 payload는 409로 거부한다. envelope의 access 값은 신뢰하지 않고 산출값과 다르면 403으로 거부하며, payload 동등성은 RFC 8785 JCS에서 `recorded_at`만 제외한 SHA-256으로 판정한다. 정본은 [intake-binding.md](intake-binding.md)와 `contracts/fixtures/intake/` 10건이다. malformed/미지원 schema·tampered text hash·미보정 확률·scope 확대는 수용하지 않는다. 첫 패키지는 네트워크·브라우저·모델·knowledge write 없이 실행돼야 한다. 이 패키지가 끝나도 전체 crawler를 완료로 표시하지 않는다.

## 착수·소유권

**선행 gate (독립 검증 R03):** 1행 RED 계약 테스트와 `contracts.py`는 바로 진행할 수 있다. `intake.py`, `collector_models.py`, migration의 작성·커밋은 CP-S2-00 `fd87eb4c-29df-47d1-ad3a-346a8c6260ba`이 변경 소유권과 테스트 DB identity를 기록한 뒤에 한다. PMS에서 CP-S2-00은 CP-S2-01의 선행으로 등록돼 있고, 이 gate는 S2-01 description과 PMS 메모에 남겼다.

1. back `git status --short --branch`, `git rev-parse HEAD`, `git diff --name-only`, `alembic heads`를 새로 기록한다. 기존 dirty 변경을 commit/reset/stash하지 않는다. 공유 `models.py/main.py/config.py/generation_service.py`는 해당 작업 소유자와 접점을 확인하고 새 모듈만 먼저 진행한다.
2. back clean 분기/checkout에서 collector 전용 파일만 소유한다. 현재 초기 조사 당시 미추적이던 kb_* 및 0053/0054(마감 시 0055까지 커밋됨)는 다른 작업 소유이므로 수정/번호 재사용 금지. 새 migration revision은 당시 graph에 맞게 배정한다. test DB URL은 프로젝트 테스트 fixture의 독립 DB 경로를 따르며 운영 DB를 쓰지 않는다.
3. front 변경은 이 패키지에 없다. 다음 UI 패키지는 origin 기반 clean checkout에서 앞선 dirty UI 변경과 범위를 확인한다.

## 파일과 순서

| 순서 | 예정 파일(현재 없음) | 작업·검증 |
|---|---|---|
| 1 RED | unit_tests/test_collector_contracts.py | 납품 schema/fixture로 valid/unknown key/version/date/hash/calibration/scope 오류를 실제 실패로 확인 |
| 2 RED | tests/test_collector_intake.py | 병렬 동일키 중복, 다른 payload 409, scope 교차, transaction rollback, 빈 입력에 대한 실패를 독립 DB로 재현. `contracts/fixtures/intake/*.json` 10건을 그대로 기대값으로 쓴다(tenant/principal/scope 변조 403, key 순서·recorded_at 재전송 replay, 의미 필드·envelope_id 변경 409) |
| 3 GREEN | src/ttakkari/collector/contracts.py | strict schema/pydantic 모델 + hash/aware UTC/quality/scope semantic validator. 문서 검사 스크립트를 runtime 코드로 착각하지 않음 |
| 4 GREEN | src/ttakkari/collector_models.py, alembic/versions/<new>_collector_intake.py | 최소 job/request-key/payload-hash/outbox 상태, composite UNIQUE+CHECK. 기존 모델 선언 등록 접점은 통합 담당자 |
| 5 GREEN | src/ttakkari/collector/intake.py | 같은 transaction에서 검증·claimable queued job 생성, no network/no LLM/no sink |
| 6 REFACTOR | 위 모듈+테스트 | 의미 검증과 저장 책임 분리, regression 재실행·짧은 migration upgrade 검사. API route는 S2-02에서 붙임 |

실제 예정 명령(back 루트):

```sh
.venv/bin/python docs/design/collector-platform/contracts/verify_contracts.py
uv run pytest -q unit_tests/test_collector_contracts.py tests/test_collector_intake.py tests/test_collector_migration.py
uv run ruff check src/ttakkari/collector src/ttakkari/collector_models.py unit_tests/test_collector_contracts.py tests/test_collector_intake.py tests/test_collector_migration.py
uv run alembic heads
```

DB migration upgrade/replay는 테스트 fixture의 일회용 DB에서 수행하고 테스트가 사용한 DB identity를 receipt에 남긴다. 이번 설계 단계에서는 위 pytest/ruff/alembic 동작 검증 미실행이며 파일도 아직 없다. 테스트 실패→수정→통과 출력, rows 동일성·키 충돌·scope 음성 결과를 다음 worker가 보고해야 한다.

## 완료기준과 다음 연결

계약 valid/invalid 전부 기대대로 판정; intake fixture 10건이 서비스+일회용 PostgreSQL 경로에서도 같은 결과(HTTP 경로는 S2-02); 10개 병렬 동일 key 요청 후 job 1행; payload 충돌 409; cross-scope 정보 누출 0; 실패 transaction에 outbox/job 잔여 없음; 새 파일만 diff/commit됨. 일일 예산 구현 전 teacher 호출 0, sink write 0으로 검증한다. 산출: `reports/collector/s2-01/verification.txt`, migration head/DB identity, pytest·lint output, changed-files manifest. run이 끝난 뒤에는 실제 신규 파일·명령에 맞춰 WBS criterion을 갱신한다.

다음 S2-02에서 generation periodic/consume·owner token·durable cursor를 연결하고, S2-03 safe fetch, S2-04 atomic budget, S2-05 독립 UI, S2-06 export/knowledge adapter 순서로 간다. 강제 가능한 teacher port가 없으면 budget/ML 경로는 off로 두고 수집 플랫폼을 진행한다. 전체 테스트·clean commit/push 후 generation 배포 예약은 사용자 상시 정책을 따르되 이번 설계 worker에서는 수행하지 않는다.

## 블로커 분류

- 착수 전: back 파일/migration 소유권과 테스트 DB fixture 확인. 이 확인은 정적 계약 테스트 작업과 병행 가능하다.
- 후속 UI만: front 발산·dirty 정리 책임자/checkout.
- knowledge sink만: 병행 kb_* ACL/visibility·raw/normalized 재처리(ADR 참조 정정 확인), 5.4.7.3.6 의미 추출 gate. export 경로는 독립.
- 자동 teacher/운영 품질 승격만: 실제 bounded token/usage port, human gold/권리, cohort CI/성능. 이번에 충족됐다고 쓰지 않는다.
