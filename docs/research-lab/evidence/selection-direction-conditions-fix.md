# 독립 FAIL 두 반례의 방향·조건 교정

2026-10-26, 교정 base `85ee4dd90677f349351c5b2fd0a5b5483fc15ba6`.
[독립 검증](selection-context-review-85ee4dd.md)의 실제 ASGI/PG 원문과 명령을 리드가 직접 읽었다.
검증자의 FAIL은 유효하며 기존 단위11/API2 PASS로 이를 덮지 않았다.

## 원인과 조건

호출 경로는 POST select→service.selection_context→rules.rank_methods다.
selection_context는 관련 관계를 양 endpoint의 조회 문맥에 보존한다.
기존 rank_methods는 이 관계의 from/to를 보지 않고 predicate와 Relation 조건만 검사했다.
따라서 outbound challenge도 받은 challenge처럼 처리하고, 상대 Node의 명시 조건은 무시했다.
독립 raw의 from_id=method 및 Node.dataset=A/Relation.dataset=B/요청B 값을 그대로 대조했다.
이는 격리 RunPG/실ASGI의 결정론적 판정 오류이며 GPU·운영 인증·LLM·네트워크 실패가 아니다.

리드가 단위 반례8개와 실제 API 반례2개로 재현했다:
단위 `8 failed, 11 deselected in 0.03s`, API `2 failed, 2 deselected in 0.75s`.
환경·의존성을 바꾸지 않고 규칙만 교정해 반례가 달라지는지 확인했다.

교정 계약:

- `challenges_under_conditions`는 명시 to_id인 방법에만 영향을 준다. outbound 관계도 원문은 보존한다.
- `contradicts`/`conflicts_with`는 대칭 상충으로 양 endpoint에 적용한다.
- Relation 조건과 상대 Node 조건은 모두 제약이다. 어느 하나도 다른 쪽을 덮지 않는다.
  하나라도 현재 환경과 불일치하면 해당 상충은 현재 환경의 반례로 적용하지 않되 그대로 보존한다.
  누락 조건은 unknown으로 남고, 관련·불명 상충은 needs_review다.
- 빈 조건 한쪽은 추가 제약이 없다는 뜻이다. 양쪽 모두 비면 unknown이다.
- relation_applicability/other_node_applicability/합성 applicability 및 affects_selection을 이유에 저장한다.
  이 판정은 보고의 적용성일 뿐 원문의 진위나 효과를 판정하지 않는다.

## 결과

```sh
PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_research_rules.py -q --tb=short
# 19 passed in 0.01s
RESEARCH_SELECTION_RECEIPT=<Run outbox>/selection-direction-receipt.json PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest tests/test_research_selection_context.py tests/test_research_lab.py tests/test_research_source_refresh_acceptance.py -q --timeout=30 --tb=short
# 17 passed in 1.41s
```

方向/조건 두 API 반례는 모두 candidate로 돌아오며, 관계 원문과 각각의 조건은 유지된다.
같은 조건의 incoming challenge, 대칭 상충, missing hardware의 unknown, 명시 hardware mismatch,
truncated의 검토 필요·확정 불일치의 excluded도 단위 경계로 검사했다.
영수증용 선택4건 재실행은 저장 JSON 수집 목적이며 새 suite로 누적해 세지 않는다.
rules/test3파일 ruff PASS, diff --check exit0.
[일반 여정 영수증](selection-direction-receipt-85ee4dd.json)과
[두 API 반례 교정 원문](selection-direction-two-cases-85ee4dd.json)을 보존했다.
제품3파일 SHA를 현재 파일과 대조했다. 자료는 합성 연구 메모이며 실GPU/실패Run 측정이 아니다.

검증자 f76의 FAIL 보고 자체는 리드 재현과 일치해 인수했다.
처음 message_worker에 failed를 기록했으나 그 필드는 구현자 완료 오판용이었다.
따라서 보고 정확성의 passed/false_completion=false로 바로 정정했다.
대상 제품85ee4dd의 FAIL 판정은 그대로 유지한다. 이번 교정은 새 독립 검증자 인수 전이다.

## 별개 배포 실패

controller02141811/12a는 `test_report_schema_separates_extraction_classification_scope_coverage`에서 실패했다.
ops 원문: 1failed/4971passed/13skipped/2xfailed/4961subtests/1233.82s.
해당 단일검사를 현재 소스에서 실행해 같은 all_collected_classified=False로 재현했다
(`1 failed, 54 deselected in 4.12s`).
최신 main4053ee12는 0073_research_reuse_source 분류 누락을 이미 교정하고
연구 migration 두 이름의 회귀를 추가했다. 해당 commit과 work-history를 직접 읽었다.
기존 누적 예약831bcd9a-98db-4475-b072-bccef6c70a29/4053ee12는testing으로 발견됐다.
그 교정을 재작성하거나 같은 후보를 다시 예약하지 않고 main 변경을 통합한다.
선택 로직의 이번 신제품은 이 기존 후보에 없으며 독립 인수 후 누적 반영해야 한다.

PMS7.2 전체·원문UX10 운영·Metal/lease/raw·프론트Vercel·운영MCP 및 R4/7/12~16은 미완이다.
기존 유효 인수와 실패 이력을 보존하고 전체 배포 gate/새 검증을 생략하지 않는다.
