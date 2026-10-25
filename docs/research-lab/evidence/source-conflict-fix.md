# 외부 보고 출처 충돌 교정

2026-10-25. 기준 제품 소스 c707aea, 착수 HEAD 1622a48.
독립 검증 [backend-stage2-fix-review-c707aea.md](backend-stage2-fix-review-c707aea.md)의 실제 PG/ASGI 충돌 반례를 보완한다.

## 원인과 정책

`record_external_report`는 최상위/원본/환경 입력에서 출처를 선택한 뒤 원본만 덮어썼다.
환경·측정 meta에는 같은 키가 이미 있으면 다른 출처를 남겼고,
adapter 경로는 측정 meta에 출처 자체를 상속하지 않았다.
이는 독립 검증의 conflict_roundtrip 관측과 일치한다.

한 보고서의 출처 제목·위치(URL/locator)·연도·provenance·execution_origin이
입력 위치 사이에서 충돌하면 422로 거부한다. 서로 다른 출처의 측정은 별도 보고서로 반입한다.
문자열의 앞뒤 공백만 무시하여 비교하며 URL/DOI의 동등성을 추정하지 않는다.
정상 입력은 정해진 우선순위로 출처를 원본·환경·측정 meta에 상속한다.
부가 환경·meta 필드는 보존하고 manual_evaluation/imported_log의 무출처 허용은 유지한다.

## 실행 근거

실행 환경: 리드 Codex command sandbox, `back-research-v2/.venv/bin/python`.
Run에 주입된 `TTAKKARI_TEST_DB`의 database=ttakkari_test, user=ttk_test,
host=127.0.0.1을 먼저 확인했다. 전체 URL/비밀번호는 출력하지 않았다.
기존 실패 PG 복원이나 수동 initdb는 실행하지 않았다.

선행 RED:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_v2_api_contract.py tests/test_research_adapters.py -k 'conflicting_source or canonical_source or external_report_adapter_lifecycle' -q --tb=short
10 failed, 86 deselected in 1.25s
```

충돌 8건은 기존 201, 정상 입력은 환경의 빈 제목 유지,
adapter lifecycle은 measurement meta의 source_title 누락으로 실패했다.
첫 GREEN 시도에서는 앱 오류 응답을 detail 키라고 가정한 테스트 오류와
추가 루프의 metric key 변수 재사용을 발견해 각각 수정했다.

변경 묶음 회귀:

```text
PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_v2_api_contract.py tests/test_research_adapters.py tests/test_stage2_defects_fix.py -q --tb=short
104 passed in 4.93s
```

새 API 회귀는 실제 ASGI 요청과 PostgreSQL을 사용하며 충돌마다 Run/Measurement 0건을 확인한다.
정상 반입은 새 GET 요청과 별도 DB session에서 제목·DOI·연도·provenance를 대조한다.
fixture는 example.invalid와 synthetic DOI이며 실제 외부 논문/측정 증거가 아니다.

4개 변경 코드/테스트 파일의 ruff JSON 진단을 HEAD 원본과 비교:
각 파일 new_diagnostics=[]; 기존 진단은 남아 있다. git diff --check는 exit 0.

## 범위

전체 suite·브라우저·A/B/C/D 실제 연구 인수·운영 배포 증거가 아니다.
기존 통합1/프론트 검증은 유효 범위에서 재사용하며 전체 완료 판정은 보류한다.
