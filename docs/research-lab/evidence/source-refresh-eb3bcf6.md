# 출처 새 버전 반입과 조건 불확실성 보존 인수

2026-10-26, 제품 backend `eb3bcf6f49638b9f76d269d5da9f79d76966545e`.
Codex 명령 환경의 실제 ASGI API와 표준 Run PostgreSQL을 사용했다.
DB의 실제 SELECT 결과가 `ttakkari_test / ttk_test`임을 검사했다.
자료는 `example.invalid`를 주소로 쓰는 합성 텍스트이며 실제 논문이나 웹 수집 결과가 아니다.

## 실행한 여정

같은 source key의 첫 텍스트를 반입하고 해당 passage의 정확한 인용을 가진 method node를 저장했다.
조건은 Metal 및 데이터셋 A이며 원인 불명이라는 문장도 그대로 유지했다.
같은 source key로 데이터셋 B의 상충 결과를 반입한 뒤 같은 바이트를 한 번 더 반입했다.
새 인용과 조건부 상충 relation을 저장하고 새 HTTP resume 및 별도 DB session으로 재조회했다.

관측:

- 문서는 하나, revision은 1/2 두 개이며 원본 hash도 각각 보존됐다.
- 같은 바이트 재반입은 revision 2를 재사용하고 `created=false`를 반환했다.
- 원래 method node는 갱신 전후 전체 필드가 동일하다. 옛 인용은 `active=false`, 새 인용은 `active=true`다.
- revision 2의 passage에 revision 1 인용을 붙이면 HTTP 422이며 잘못된 claim은 남지 않았다.
- 두 node는 `source_reported`다. 자동 검증이나 로컬 실측으로 승격되지 않았다.
- 후속 연구에서 데이터셋 미입력은 `needs_review`/missing dataset, A는 `candidate`, B는 `excluded`다.
  모든 경우 예상 효과는 `unknown_until_measured`이며 근거가 있다는 이유로 효과를 예측하지 않았다.

## 명령과 결과

```sh
RESEARCH_SOURCE_REFRESH_RECEIPT=<현재 Run outbox>/research-source-refresh.json PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest tests/test_research_source_refresh_acceptance.py -q --timeout=30 --tb=short
```

첫 실행 `1 passed in 0.82s`. 영수증에 제품 파일 hash를 추가한 최종 실행은 `1 passed in 0.85s`.
`ruff check tests/test_research_source_refresh_acceptance.py` → `All checks passed!`.
별도 Python에서 원시 JSON의 DB identity와 제품 4파일 SHA256 일치를 확인했다.

검사기의 부정 대조는 임시 pytest plugin만으로 `service.snapshot`의 모든 evidence를 `active=true`로 바꿨다.
같은 검사 명령에 `PYTHONPATH=src:/private/tmp/research-refresh-control-9aadd327` 및 `-p force_current`를 사용했다.
`saved["active"] is active`에서 `True is False`로 예상 실패했다(`1 failed in 0.87s`).
제품 소스 및 정상 영수증은 수정하지 않았다. 이 반례는 이전 인용을 최신으로 잘못 표시하는 오류를 검출한다.

원시 영수증: [source-refresh-eb3bcf6.json](source-refresh-eb3bcf6.json), 19,740 bytes,
SHA256 `47c5b40776e68d9a69ea976062e57068a06d0f7822f0b2183e55d3a845151cff`.
첫/두 번째/멱등 반입 응답, 갱신 전후 resume, 후속 조건별 selection, 코드 hash를 보존했다.

## 인수 범위

R11/R13의 출처·과거 연구 보존과 PMS 7.1/7.2의 버전·불확실 조건 보존 일부를 보완한다.
Collector Platform의 자동 재수집·갱신 연결, 자동 주장 추출, 선택 편향/정보가치 calibration은 실행하지 않았다.
PMS 7.1/7.2 전체 완료로 처리하지 않는다. 브라우저·운영 MCP·Metal·운영 배포 증거도 아니다.
배포 `b8bb518b`는 이번 Run 첫 조회에서 testing/skip_tests=false/error=null이며 반복 조회하지 않았다.
새 제품 변경이나 중복 배포 예약은 없고 controller가 필수 전체 검사의 실행 소유자다.
