# KB v3 L2 기본 활성화 및 실검증 호스트 오퍼레이션(`kb_v3_verify`) 구현 보고서

- **일자**: 2026-10-27
- **브랜치**: `feat/kb-v3-l2-activation-20261027` (Base: `c07ea7cd0440363abc6cacb909b3f0cd338ef43b`)
- **작업트리 위치**: `/Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027`
- **담당/원칙**: 리드 브랜치 격리 준수, `main/push/deploy` 미수행, 커밋 보존

---

## 1. 작업 개요

지식 도서관(KB v3) 전면 리팩토링 검증(R1, R7, R8)을 충족하기 위한 2가지 핵심 작업을 완결했습니다:
1. **작업 1 (R1/R7 제품 기본 활성화)**: 제품 기본 설정을 채택된 `subscription_cli`로 활성화하고, 앱 현재 카탈로그/계정이 지원하는 기본 모델 `gpt-6-luna`를 구성. 기존 명시적 `disabled` optout 및 과금 경로/예산 보호를 보존하고, 테스트 스위트 격리용 `TTAKKARI_KB_L2_ADAPTER="disabled"` 설정 및 회귀 검증 확보.
2. **작업 2 (R8 호스트 실검증 오퍼레이션 `kb_v3_verify`)**: 기존 승인된 `host_operation_start` API 및 MCP server에 고정 kind `kb_v3_verify`를 등록. 임의 쉘/경로/DB URL/프롬프트 입력을 엄격 차단(`extra='forbid'`)하고, 1건의 공개 합성 PDF와 2건의 상충 정책 문서(30일 vs 90일)를 제품 수집 파이프라인으로 처리하여 L2 추출 및 정본·상충(`value_contradiction`) 파이프라인의 종단간 동작을 finite 시간 내 검증.

---

## 2. 세부 구현 사항

### 작업 1: L2 기본 활성화 및 설정 안전성
- [`src/ttakkari/config.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/src/ttakkari/config.py):
  - `kb_l2_adapter: Literal[...] = "subscription_cli"`로 제품 기본값 변경.
  - `kb_l2_subscription_model: str = "gpt-6-luna"`로 앱 카탈로그 지원 모델 지정.
  - `kb_l2_subscription_engine: Literal["codex", "claude"] = "codex"`, concurrency=1, timeout=90s 등 기존 안전 제어 유지.
- [`tests/conftest.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/tests/conftest.py):
  - 일반 통합 테스트가 자동 클라우드 구독 CLI를 호출하지 않도록 `TTAKKARI_KB_L2_ADAPTER: "disabled"` 명시 추가 및 격리 주석 기재.
- [`tests/test_kb_v3_default_activation.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/tests/test_kb_v3_default_activation.py):
  - 기본 설정 환경에서 `SubscriptionCliL2Adapter` 및 `gpt-6-luna` 바인딩 검증.
  - 명시적 `disabled` optout 존중 검증.
  - 타 유료 API/임의 과금 경로 우회 차단 검증.
  - 일일 예산(`kb_l2_daily_budget`) 및 계정 가용성 부족 시 안전한 `degraded` 폴백 검증.

### 작업 2: 호스트 실검증 오퍼레이션(`kb_v3_verify`)
- **오퍼레이션 등록 및 엄격한 입력 스키마**:
  - [`src/ttakkari/operations/plans.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/src/ttakkari/operations/plans.py): `INLINE` 및 `KINDS`에 `"kb_v3_verify"` 추가.
  - [`src/ttakkari/api/operations.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/src/ttakkari/api/operations.py): `DESCRIPTIONS["kb_v3_verify"]` 및 `StartIn.kind` Literal에 추가 (`extra="forbid"`로 generic parameter 차단).
  - [`src/ttakkari/orchestrator/mcp_server.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/src/ttakkari/orchestrator/mcp_server.py): MCP 도구 `host_operation_start`의 Literal 및 docstring에 반영.
  - [`src/ttakkari/operations/inline.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/src/ttakkari/operations/inline.py): `verify_kb_v3` 라우팅 연결.
- **실검증 로직 ([`src/ttakkari/operations/kb_v3_verify.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/src/ttakkari/operations/kb_v3_verify.py))**:
  - `check_schema_support`: 활성 런타임/세대의 KB v3 스키마 유무 확인 (미배포 시 `unsupported / kb_v3_schema_missing` 반환).
  - 공개 합성 문서 3건 등록:
    - 1건 공개 합성 PDF (`synthetic:kb_v3_verify:pdf_spec`): 1359 bytes 내장 PDF, pypdf v1 파서 정상 파싱 및 세그먼트/잡 인큐.
    - 2건 상충 정책 문서 (`synthetic:kb_v3_verify:policy_30d`, `synthetic:kb_v3_verify:policy_90d`): 동일 엔터티(`Return Policy / 반품 규정`), 동일 predicate(`refers to / definition`)에 대해 30일 vs 90일 상충 본문 명시.
  - 재처리 경로: 이전 실행에서 L2 비활성으로 degraded된 잡이 있을 경우 `retry_pipeline_job(..., from_stage="extracting")`으로 안전 재처리.
  - Finite 자동 대기 루프: `MAX_VERIFY_WAIT_SECONDS = 30.0` 상한, `active_worker.pump()` 소비 및 터미널 상태(`ready`, `degraded`, `failed`, `held`) 도달 시 조기 탈출.
  - 결과 집계 및 보안: DB 조회 기반 실제 집계값(문서 수, 패시지 수, 엔터티 수, 주장 수, 충돌 수 및 충돌 ID 목록, 단계별 상태)만 반환. DB URL, 비밀값, 원문 본문, 프롬프트 일체 비노출.
- **인프라 버그 수정 ([`src/ttakkari/kb_pipeline/daily_budget.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-l2-activation-20261027/src/ttakkari/kb_pipeline/daily_budget.py))**:
  - SQLite/DB 방언 간 datetime 비교 시 naive datetime과 aware datetime 비교로 발생하는 `TypeError: can't compare offset-naive and offset-aware datetimes`를 방어하기 위해 `_to_aware()` 헬퍼를 도입하여 안전 비교 보장.

---

## 3. 검증 결과

### 1) 단위/통합 테스트 (27/27 통과)
```bash
PYTHONPATH=src:tests /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest tests/test_kb_v3_default_activation.py tests/test_kb_v3_verify_operation.py tests/test_kb_v3_visibility_and_l2_adapters.py
```
출력:
```
============================== 27 passed in 1.06s ==============================
```

- `test_kb_v3_default_activation.py`: 6 passed
  - 기본 subscription_cli 설정 바인딩, disabled optout, 비허용 유료 API 거부, 일일 예산 초과 시 degraded 보고, 계정 미가용 시 degraded 보고 등.
- `test_kb_v3_verify_operation.py`: 6 passed
  - 오퍼레이션 레지스트리 및 generic 파라미터 엄격 거부(`extra='forbid'`)
  - MCP 도구 연결 및 스키마 검증
  - 스키마 부재 시 `unsupported / kb_v3_schema_missing` 보고
  - 공개 합성 PDF + 상충 정책 파이프라인 통과, L2 추출 및 상충(`value_contradiction`) 감지 확인
  - L2 disabled 시 degraded 정직 보고
  - 멱등 재실행 및 문서 중복 미발생 확인
- `test_kb_v3_visibility_and_l2_adapters.py`: 15 passed
  - 기존 L2 어댑터 격리 및 가시성 회귀 전체 통과.

### 2) 정적 분석 (Ruff)
```bash
/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/config.py src/ttakkari/operations/plans.py src/ttakkari/operations/inline.py src/ttakkari/operations/kb_v3_verify.py src/ttakkari/api/operations.py src/ttakkari/orchestrator/mcp_server.py src/ttakkari/kb_pipeline/daily_budget.py tests/test_kb_v3_default_activation.py tests/test_kb_v3_verify_operation.py
```
출력:
```
All checks passed!
```

---

## 4. 변경 파일 목록

- `src/ttakkari/config.py`: 기본 L2 어댑터 및 모델 설정
- `src/ttakkari/operations/plans.py`: `kb_v3_verify` kind 및 inline 등록
- `src/ttakkari/operations/inline.py`: `kb_v3_verify` 인라인 핸들러 연결
- `src/ttakkari/operations/kb_v3_verify.py`: 실검증 오퍼레이션 구현 신규 파일
- `src/ttakkari/api/operations.py`: `kb_v3_verify` 설명 및 StartIn kind 허용
- `src/ttakkari/orchestrator/mcp_server.py`: MCP tool enum 및 docstring 추가
- `src/ttakkari/kb_pipeline/daily_budget.py`: DB 방언 timezone naive-aware 비교 안전 헬퍼 추가
- `tests/conftest.py`: 시험 격리용 L2 disabled 설정
- `tests/test_kb_v3_default_activation.py`: 기본 활성화 단위 테스트 신규 파일
- `tests/test_kb_v3_verify_operation.py`: 실검증 오퍼레이션 단위/인라인 테스트 신규 파일

---

## 5. 미검증 사항 및 리드 검증 영역 (Honest Reporting)

- **호스트 LLM 실제 호출 및 PDF 파서 E2E**:
  - 이번 작업에서는 단위/통합 테스트 환경에서 합성 파서 및 mock adapter를 통해 등록/파이프라인/충돌/재실행 전 과정을 격리 검증했습니다.
  - 실제 호스트 환경에서의 `subscription_cli`(Codex CLI + gpt-6-luna) 실제 실행 및 실제 PostgreSQL DB 상의 E2E 실행은 **배포 후 리드 검증 영역**(`검증 못함: 운영 배포 및 호스트 LLM 실제 실행은 리드 소유`)입니다.
- **Git 상태**:
  - 리드 원칙에 따라 `main` 브랜치에 직접 커밋하거나 `git push`, `request_backend_deploy`를 수행하지 않고, 전용 sibling worktree 브랜치(`feat/kb-v3-l2-activation-20261027`)에서 커밋까지 완료하여 보존합니다.
