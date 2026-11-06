# Reverse platform v2 검증 기록

날짜: 2026-11-07 (Asia/Seoul)

저장소 브랜치: `feat/reverse-evidence-20261107`

DB: `tests/conftest.py`가 생성한 프로세스별 임시 PostgreSQL 클러스터와 전용 `ttakkari_test`; 운영 DB는 사용하지 않았다.

## 최종 실행 결과

```text
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py
.......................... [100%]
26 passed in 1.62s
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_reverse_compare.py tests/test_reverse_reconstruction.py tests/test_reverse.py unit_tests/test_reverse_static.py
............................................................             [100%]
60 passed in 9.92s
```

```text
$ /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py src/ttakkari/reverse.py src/ttakkari/api/reverse.py src/ttakkari/reverse_compare.py tests/test_reverse_reconstruction.py tests/test_reverse_compare.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py
All checks passed!
```

```text
$ git diff --check
(출력 없음, exit 0)
```

총 `86 passed`다. pytest는 같은 기본 모듈명을 쓰는 두 비교 테스트 파일의 충돌을 피하려고 단위·API 호출을 분리했다. 이 결과는 Location 비밀 쿼리 반례 수정까지 포함한다.

## 실제 검증 범위

- Unit: legacy report fallback, 직접 비교 파일·SHA-256·JSON·run_id·입력/환경/출력 정합성, 서버 측 match/mismatch 재계산, 원본 불가/timeout의 null·unverified, analyze-only 및 README 필수 항목.
- 비교 실행기: CLI argv/바이트 캡처·timeout·원본 누락·증거 파일 충돌·CLI 명령 인터페이스, API 응답·상태 비교·redirect 차단·URL 쿼리 비밀값 마스킹, 웹 관찰의 미검증 승격 방지.
- API/DB: 요청 필드·PATCH 검증, analyze→reconstruct 관찰 재사용, 후속 회차 이력, 별도 독립 검증 세션, mismatch 기반 제한 repair, `passed` 단독 승격 차단, 실제 Run manager와 ZIP 산출 회귀.
- 기존 reverse API·정적 리버스 테스트와 workspace/session 범위 검사.

## 원본 직접 비교 증거

```text
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python scripts/reverse_compare_basename.py --output /private/tmp/reverse-basename-audit --run-id basename-audit-20261107
{"run_id": "basename-audit-20261107", "cases": 9, "results": {"match": 8, "mismatch": 1, "unverified": 0}}
```

```text
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python scripts/reverse_compare_recheck.py --workspace docs/reverse-platform/evidence/basename-direct-run-20261107 --report docs/reverse-platform/evidence/basename-direct-run-20261107/comparisons.json --output /private/tmp/basename-server-recheck.json --run-id basename-audit-20261107
{"count": 9, "results": {"match": 8, "mismatch": 1, "unverified": 0}}
```

두 번째 명령은 서버 `verify_comparisons`로 각 case의 `.reverse/comparisons/*.json` SHA-256·run_id·실행 로그·출력을 다시 읽어 direct로 재계산했고, 8 match/1 mismatch/0 unverified를 assert했다. 원본은 실제 `/usr/bin/basename`, 복제는 fixture 소스다. 전체 보고서와 원시 캡처는 [evidence/basename-direct-run-20261107](evidence/basename-direct-run-20261107)에 보존했다. `-z` 오류 메시지의 명령 접두사 차이는 기대된 불일치로 유지했다.

## 실행 중 발견해 수정한 테스트/검증 이슈

- 비교 테스트 두 파일의 모듈명이 같아 pytest import 충돌이 났다. 단위 검사와 API 검사 명령을 별도로 실행했다.
- 기존 fake raw evidence가 v2 `schema_version/target_type/run_id/complete` 계약과 맞지 않아 새 서버 검증에서 미검증으로 분류됐다. 실제 v2 캡처 fixture로 갱신했고 최종 전체 검사가 통과했다.
- 독립 감사에서 API redirect allowlist 우회, URL 쿼리 비밀값 노출, 비정형 target_type TypeError, state_path 요청에서 상태 전/후 캡처 누락, 응답 Location의 비밀 쿼리 캡처 승인을 확인했다. 이를 수정하고 반례 테스트를 추가했다.

## 미실행 및 한계

- UI/browser 및 실제 운영 원본 대상 비교는 실행하지 않았다. 이번 작업 저장소 테스트에서는 운영 시스템 접근을 하지 않았다.
- work-history 원격 ref `feat/reverse-evidence-20261107`는 `git fetch`에서 `fatal: couldn't find remote ref`가 나 최신화하지 못했다. 시작 시 checkout 기준으로 작업했다.
- 자동 repair는 최대 3회까지 구현 세션에 요청을 큐잉한다. 외부 모델이 실제 결함을 고치는지 여부는 fixture 기반 DB 통합 테스트 범위이며 운영 모델 작업은 실행하지 않았다.
- 비교 raw capture는 최대 1 MiB JSON 및 지정 출력/timeout 한도 내에서만 검증한다. 해시와 파일 정합성은 사후 변조를 탐지하지만 원본 대상의 진실성 자체를 증명하지 않는다.

## 후속 판정 교정 — 2026-11-07

기존 5b65f345/5c45fc0b/c016b6e6/90bfefea와 위의 이전 실행 기록은 보존한다. 이번 후속은 CLI/API 완료 판정 및 응답 계약에 한정하며 웹 캡처·픽셀 판정은 확장하지 않았다.

### 실행 결과

먼저 지정 반례를 회귀로 고정했다. 새 테스트는 기존 코드에서 verifier가 비교 0건인데 `done`이 되는 것을 재현했다.

```text
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_reverse_reconstruction.py::test_completion_requires_files_then_queues_independent_verification
FAILED: AssertionError: assert 'done' == 'partial'
```

```text
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py tests/test_reverse_compare_api.py tests/test_reverse_reconstruction.py tests/test_reverse.py unit_tests/test_reverse_static.py
91 passed in 11.26s
```

같은 `test_reverse_compare` 기본 이름을 쓰던 API 테스트 파일을 `tests/test_reverse_compare_api.py`로 바꿔 단일 pytest 호출에서 충돌하지 않게 했다. 단일 호출은 독립 verifier 자체 비교 0건/mismatch, verifier 필수검사 실패, verifier mismatch repair→새 verification, callback 중복·늦은 callback, 중지 후 retry/history, 상세 v2 응답, 관찰 수준·분석 전용 추천, 안전한 ZIP 내보내기를 포함한다.

```text
$ /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py src/ttakkari/api/reverse.py tests/test_reverse_reconstruction.py tests/test_reverse_compare_api.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py
All checks passed!
$ git diff --check
(출력 없음, exit 0)
```

### 판정 범위

- 독립 verifier는 자신의 검증된 비교와 필수 검사 증거를 모두 통과해야 한다. 실패 비교·검사 보고는 구현자의 비교와 분리해 latest에 보존된다.
- verifier mismatch 또는 불충분한 검사 증거는 설정된 repair 한도를 따르며, repair 뒤 새 verifier Run을 만든다. 취소/중단은 부분 상태로 남고 retry 시 직전 결과를 history에 보존한다.
- 상세 API의 `result`는 v2 JSON을 유지한다. `latest`, `history`, `result_state`는 같은 저장 결과를 반영한다.
- 관찰에 `direct|document|inferred|unverified` 수준을 보존하며 기존 수준 누락은 미확인이다. 분석 전용 요청 의도와 짧은 제목 추천을 추가했다.
- ZIP은 안전한 의존성 파일, 테스트, 허용 비교 manifest/raw capture를 담고 경로탈출·symlink·민감 파일·감지한 비밀 문자열은 제외한다.
- 실제 원본 시스템, 플랫폼 에이전트 E2E, 웹의 서버측 실제 증거 검증은 미실행/미검증이다. SHA-256은 캡처 사후 변조만 확인하며 원본 실행의 진실성을 입증하지 않는다.
