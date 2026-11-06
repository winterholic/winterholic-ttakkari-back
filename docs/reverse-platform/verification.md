# Reverse platform v2 검증 기록

날짜: 2026-11-07 (Asia/Seoul)

저장소 브랜치: `feat/reverse-evidence-20261107`

DB: `tests/conftest.py`가 생성한 프로세스별 임시 PostgreSQL 클러스터와 전용 `ttakkari_test`; 운영 DB는 사용하지 않았다.

## 최종 실행 결과

```text
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py
......................                                                   [100%]
22 passed in 0.90s
$ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_reverse_compare.py tests/test_reverse_reconstruction.py tests/test_reverse.py unit_tests/test_reverse_static.py
............................................................             [100%]
60 passed in 9.45s
```

```text
$ /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py src/ttakkari/reverse.py src/ttakkari/api/reverse.py src/ttakkari/reverse_compare.py tests/test_reverse_reconstruction.py tests/test_reverse_compare.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py
All checks passed!
```

```text
$ git diff --check
(출력 없음, exit 0)
```

총 `82 passed`다. pytest는 같은 기본 모듈명을 쓰는 두 비교 테스트 파일의 충돌을 피하려고 단위·API 호출을 분리했다. 마지막 코드 수정 뒤 실행한 결과다.

## 실제 검증 범위

- Unit: legacy report fallback, 직접 비교 파일·SHA-256·JSON·run_id·입력/환경/출력 정합성, 서버 측 match/mismatch 재계산, 원본 불가/timeout의 null·unverified, analyze-only 및 README 필수 항목.
- 비교 실행기: CLI argv/바이트 캡처·timeout·원본 누락·증거 파일 충돌·CLI 명령 인터페이스, API 응답·상태 비교와 민감 헤더 비저장, 웹 관찰의 미검증 승격 방지.
- API/DB: 요청 필드·PATCH 검증, analyze→reconstruct 관찰 재사용, 후속 회차 이력, 별도 독립 검증 세션, mismatch 기반 제한 repair, `passed` 단독 승격 차단, 실제 Run manager와 ZIP 산출 회귀.
- 기존 reverse API·정적 리버스 테스트와 workspace/session 범위 검사.

## 실행 중 발견해 수정한 테스트/검증 이슈

- 비교 테스트 두 파일의 모듈명이 같아 pytest import 충돌이 났다. Unit 테스트 파일을 `test_reverse_compare_unit.py`로 분리했다.
- 기존 fake raw evidence가 v2 `schema_version/target_type/run_id/complete` 계약과 맞지 않아 새 서버 검증에서 미검증으로 분류됐다. 실제 v2 캡처 fixture로 갱신했고 최종 전체 검사가 통과했다.
- 초반 전체 실행에서 오래된 fixture 관련 4건 실패와 비교기 단위 3건 실패가 있었다. 실행기 timeout/null·사람이 읽는 difference와 test fixture를 맞춘 뒤 최종 지정 범위에서 80건 통과를 확인했다.

## 미실행 및 한계

- UI/browser 및 실제 운영 원본 대상 비교는 실행하지 않았다. 이번 작업 저장소 테스트에서는 운영 시스템 접근을 하지 않았다.
- work-history 원격 ref `feat/reverse-evidence-20261107`는 `git fetch`에서 `fatal: couldn't find remote ref`가 나 최신화하지 못했다. 시작 시 checkout 기준으로 작업했다.
- 자동 repair는 최대 3회까지 구현 세션에 요청을 큐잉한다. 외부 모델이 실제 결함을 고치는지 여부는 fixture 기반 DB 통합 테스트 범위이며 운영 모델 작업은 실행하지 않았다.
- 비교 raw capture는 최대 1 MiB JSON 및 지정 출력/timeout 한도 내에서만 검증한다. 해시와 파일 정합성은 사후 변조를 탐지하지만 원본 대상의 진실성 자체를 증명하지 않는다.
