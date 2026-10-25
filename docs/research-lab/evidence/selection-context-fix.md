# 방법 선택의 조건부 상충 문맥 누락 교정

2026-10-26, base `f99eed118303d3cf2b5e8688bbfa28af80c812a6`.
PMS7.2의 원래 조건 중 기존 방법 선택 API가 저장된 상충 관계를 읽지 않는 공백을 교정했다.
운영 배포 기다림과 독립적인 구현이다. 기존 R3/D/UI 인수나 전체 suite를 반복하지 않았다.

## 재현과 변경

기존 `/studies/{id}/select`는 같은 scope의 method Node를 찾아 조건·비용·evidence 링크 수만 사용했다.
따라서 A 환경에서 상충한다고 기록한 관계가 있어도 A 환경에 candidate를 반환했다.
새 단위 반례 5개는 `candidate != needs_review` 또는 `reason.research_context` 누락으로 실패했다:
`5 failed, 6 deselected in 0.03s`.
실제 ASGI/Run PostgreSQL 반례도 `candidate != needs_review`로 `1 failed in 0.69s`였다.

추가한 `selection_context`는 같은 scope/같은 연구의 양 끝 Node를 확인해 관련 Relation 최대200개를 읽는다.
관계의 predicate·조건·rationale·상대 Node의 ID/종류/인식 상태/근거 ID를 유지한다.
잘린 경우 truncated=true를 전달하며, 완전한 문맥으로 오인하지 않는다.
현재 환경에 적용 가능하거나 조건이 불명인 challenges_under_conditions/contradicts/conflicts_with는
검토 필요로 내린다. 다른 환경의 상충은 문맥에 보존하고 자동 제외하지 않는다.
조건/예산이 명확히 맞지 않는 경우의 기존 excluded 판정은 유지한다.

과거 실패 관계와 조합 가설은 그대로 보여주며 단독으로 방법을 제외하거나 조합 효과를 만들지 않는다.
expected_effect=unknown_until_measured를 유지하고 evidence_quality/information_value=unassessed를 명시한다.
근거 링크 수를 품질이나 효과의 수치 calibration으로 승격하지 않는다.
판단 당시 문맥은 기존 selection_recorded 이벤트에 함께 저장되어 fresh resume으로 복원된다.
스키마·실행기·리소스 가드·프론트 변경은 없다.

## 실행 근거

```sh
PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_research_rules.py -q --tb=short
# 11 passed in 0.01s
RESEARCH_SELECTION_RECEIPT=<현재 Run outbox>/selection-context-receipt.json PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest tests/test_research_selection_context.py tests/test_research_lab.py tests/test_research_source_refresh_acceptance.py unit_tests/test_research_rules.py -q --timeout=30 --tb=short
# 26 passed in 1.32s
```

새 실제 API 검사는 같은 scope의 이전 연구 관계를 후속 연구에서 읽고 A→needs_review/B→candidate,
원본 상대 Node의 unverified 및 fresh 이벤트 문맥을 확인했다.
별도 work 연구의 method/관계가 개인 선택 결과에 없으며,
201개 관계를 넣었을 때 200개와 truncated=true/needs_review가 반환되는 것도 실제 PG에서 확인했다.
기존 source 갱신/조건 불확실성 및 연구 lifecycle 회귀를 함께 통과했다.
DB 이름/명시 user는 주입 URL과 실제 SELECT를 비교하며 운영 ttakkari를 거절했다.
비밀 없는 [영수증](selection-context-receipt-f99eed11.json)에 fresh HTTP/이벤트·DB identity와 제품3파일 hash가 있다.

추가 scope fixture의 첫 실행은 연구가 다른 Node를 연결하려다가 기존 DB trigger의
`research relation study mismatch`에 거절됐다(1failed25passed1.57s).
이를 제품 결함으로 바꾸지 않고 정상 work 연구 내부 관계를 만든 뒤 개인 응답 부재를 검증하도록 시험만 교정했다.
trigger/권한/격리 정책은 완화하지 않았다. 위26PASS가 교정 후 결과다.

rules 및 신규/수정 시험 ruff PASS, diff --check exit0.
API/service 기존 ruff 진단은 각각16/3개이며 HEAD 기준 code/message multiset 대조에서 증가0이다.
전체 파일 lint PASS라고 주장하지 않는다.

## 남은 범위

실제 실행의 실패 Run을 자동 수집하는 기능이나 근거 원문 질 평가, calibrated 정보가치/비용 예측,
v2 Method Registry 자동 추천 및 새 프론트 문맥 표시는 이번 변경에 없다.
따라서 PMS7.2 전체 done은 아니며 새 독립 검증과 운영 반영이 필요하다.
이전 인수 source-diff0 기록은 당시 revision의 사실로 보존한다. 이번 제품 차이는 select 경로로 제한된다.

기존 controller02141811/12aaf56를 이번 Run에1회조회: testing/skip_tests=false/error=null.
ops summary71줄/5549bytes/92%/오류section0. 추가 poll·중복예약은 없다.
이 새 제품 수정은 해당 후보에 포함되지 않았고 별도 독립 인수 후 누적 배포해야 한다.
프론트734/Vercel·A Metal/lease/raw·운영MCP/원UX10·PMS7.x 원조건은 계속 남는다.
