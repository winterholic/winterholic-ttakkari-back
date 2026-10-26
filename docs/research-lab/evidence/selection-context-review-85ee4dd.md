# PMS7.2 selection-context 독립 검증 — 85ee4dd

## 판정

**좁은 교정 범위 판정: FAIL.** /studies/{id}/select가 저장 관계를 읽고 같은 scope로 제한하며 200개 경계를 표시하는 것은 확인했다. 그러나 실제 ASGI/PostgreSQL 반례에서 관계 방향과 상대 Node 조건을 판정에 반영하지 않는 두 문제가 관찰됐다. 제품 코드는 수정하지 않았다.

이는 PMS7.2 전체 완료 판정이 아니다. 실제 실패 Run 자동 수집, 근거 질 평가, 정보가치/효과 수치 calibration, Registry 추천 및 UI 표시는 미구현 범위다.

## 대상과 불변 범위

- Branch: research-v2
- 요청 HEAD: 85ee4dd90677f349351c5b2fd0a5b5483fc15ba6
- Base: f99eed118303d3cf2b5e8688bbfa28af80c812a6
- 변경 대상: select_methods의 context 연결, selection_context, rank_methods, 지정된 단위/API 테스트 및 §7 요구사항.
- 기존 제품 diff는 이 경로 세 파일에 국한된다. 메인 back/, front/ checkout은 수정하지 않았다.
- DB identity는 secret을 출력하지 않고 확인했다: select current_database(), current_user → ('ttakkari_test', 'ttk_test'); injected URL의 database도 ttakkari_test이며 ttakkari가 아니었다.
- 원래 변경 기록의 6 RED → 26 PASS와 첫 cross-study scope fixture가 PostgreSQL trigger의 research relation study mismatch로 거절된 이력은 [selection-context-fix.md](selection-context-fix.md)에 남아 있다. 본 검증은 이를 재현하거나 trigger/격리 정책을 바꾸지 않았다.

## 재현 증거

전체 응답 및 요청/관계 fixture의 비밀 제거 raw 자료는 [selection-context-review-85ee4dd-raw.json](selection-context-review-85ee4dd-raw.json)에 있다. 본 검증의 실제 ASGI 요청은 테스트 fixture의 httpx.AsyncClient → ASGI app 경로였고, DB 작업은 위 ttakkari_test였다.

### FAIL — 관계 방향이 선택 영향에 반영되지 않음

API fixture에서 predicate=challenges_under_conditions를 from_id=method, to_id=hypothesis 방향으로 저장하고, 환경 및 관계 조건을 dataset=B로 맞췄다. method가 challenge를 가리키는 방향인데도 선택 결과의 method는 needs_review였고, 관계는 그 method의 context에 붙었다.

    DIRECTION={"eligibility": "needs_review", "from_id": "d2b381a6-27cc-4eeb-b73b-9d46ef39837c", "method_id": "d2b381a6-27cc-4eeb-b73b-9d46ef39837c", "relation_count": 1}

현재 구현은 관련 관계를 양 endpoint 모두에 복제한 뒤 predicate만으로 unresolved challenge 여부를 계산한다. 기존 시험의 방향(hypothesis → method)과 대조하면 predicate가 방향성을 갖는 경우 method를 반대 endpoint에서 challenge 받은 것으로 잘못 취급한다. 이 판정은 저장된 관계 방향의 의미를 따른다는 해석에 기반한다.

### FAIL — 상대 Node의 명시 조건을 applicability에 반영하지 않음

상대 hypothesis Node 조건은 dataset=A, Relation 조건은 dataset=B, 요청 환경은 dataset=B였다. 응답은 관계 applicability를 applicable로 두고 method를 needs_review로 내렸다. raw 응답에는 other_node.conditions={"dataset":"A"}가 보존돼 있지만 이를 별도 평가하거나 관계 applicability와 결합하지 않았다.

    ENDPOINT_CONDITION={"eligibility": "needs_review", "environment": {"backend": "metal", "dataset": "B"}, "other_node_conditions": {"dataset": "A"}, "relation_applicability": "applicable", "relation_conditions": {"dataset": "B"}}

따라서 관계와 상대 Node의 적용 조건이 서로 어긋날 때 현재 선택 판정은 관계 조건만으로 내려진다. 서로 다른 조건이 함께 저장된 입력에서 어느 조건을 우선할지 정하는 로직도 확인되지 않았다.

## PASS 및 관찰된 경계

- DB trigger가 허용하는 같은 연구 안의 관계만 사용했다. 기존 tests/test_research_selection_context.py는 다른 work scope의 Node/관계가 personal 선택 응답에 섞이지 않는지 검사한다.
- 실제 API 경계: 관계 200개일 때 200 returned, truncated=false; 201개일 때 200 returned, truncated=true.
- 링크 포화 반례: 한 method에 최근 조합 관계 200개를 연결하고 별도 method에 오래된 조건부 challenge를 두자, 후자의 context는 relations=[], truncated=true, eligibility=needs_review였다. 안전한 보수 판정은 유지하지만 인기 method의 링크가 다른 method의 관계 설명을 cap 밖으로 밀어낸다. raw JSON에 숨겨진 relation ID와 후보 응답을 보존했다.
- 근거 수는 evidence_links에 count로만 표시됐고, 응답은 expected_effect=unknown_until_measured, evidence_quality=unassessed, information_value=unassessed였다. 이 축을 calibration된 효과/품질 점수로 표현하지 않는 것을 코드와 단위 테스트에서 확인했다.
- 조합 관계는 후보를 자동 제외하지 않고 관계 context로 보존된다. 다른 환경의 incompatible challenge는 자동 제외로 승격되지 않는다. 단, 기존 자동시험의 같은 조건/다른 조건 경로를 재실행하지는 않았으므로 독립 실 API 확인 범위는 위 raw 자료에 기록된 입력들이다.

## 실행 명령 및 출력

초기 Git 확인:

    $ git -C .../back-research-v2 rev-parse HEAD
    85ee4dd90677f349351c5b2fd0a5b5483fc15ba6
    $ git -C .../back-research-v2 rev-parse research-v2
    85ee4dd90677f349351c5b2fd0a5b5483fc15ba6

안전한 DB 확인 명령은 TTAKKARI_TEST_DB 값을 출력하지 않고 URL database와 실제 서버 identity만 조회했다.

    TTAKKARI_TEST_DB_PRESENT=True
    configured_database=ttakkari_test
    configured_user=ttk_test
    server_database=ttakkari_test
    server_user=ttk_test

독립 지정 API 테스트:

    $ .venv/bin/python -m pytest tests/test_research_selection_context.py -q --tb=short
    ..                                                                       [100%]
    db reset: 2 selective (avg 4.0/115 tables), 0 forced full, 0 fell back to full
    2 passed in 0.67s

독립 임시 ASGI 반례 수집 명령(테스트 파일은 실행 후 삭제, raw 결과만 보존):

    $ .venv/bin/python -m pytest tests/test_selection_context_independent_raw_temp.py -q -s --tb=short
    RAW_EVIDENCE=docs/research-lab/evidence/selection-context-review-85ee4dd-raw.json
    DB_IDENTITY=["ttakkari_test", "ttk_test"]
    DIRECTION={"eligibility": "needs_review", "from_id": "d2b381a6-27cc-4eeb-b73b-9d46ef39837c", "method_id": "d2b381a6-27cc-4eeb-b73b-9d46ef39837c", "relation_count": 1}
    ENDPOINT_CONDITION={"eligibility": "needs_review", "environment": {"backend": "metal", "dataset": "B"}, "other_node_conditions": {"dataset": "A"}, "relation_applicability": "applicable", "relation_conditions": {"dataset": "B"}}
    TRUNCATION_BOUNDARY={"at_201": {"returned": 200, "truncated": true}, "exactly_200": {"returned": 200, "truncated": false}}
    POPULARITY_SATURATION={"hidden_relation_id": "d0a4d7cc-17ce-41d2-9c04-92d2f9734ab4", "popular_context_relation_count": 100, "rare_context_relation_count": 0, "rare_eligibility": "needs_review", "rare_truncated": true}
    .
    db reset: 1 selective (avg 3.0/115 tables), 0 forced full, 0 fell back to full
    1 passed in 1.51s

직접 규칙 단위 확인:

    $ .venv/bin/python -m pytest unit_tests/test_research_rules.py -q --tb=short
    ...........                                                              [100%]
    11 passed in 0.01s

diff whitespace 및 변경 범위 확인:

    $ git diff --check f99eed118303d3cf2b5e8688bbfa28af80c812a6...85ee4dd
    # exit 0, 출력 없음

## 미실행 / 미검증

- 6 RED → 26 PASS 및 이전 API scope/truncation/lifecycle 회귀 전체는 이전 fix receipt의 기록이며 이번 독립 검증에서 전체 suite로 재실행하지 않았다.
- 테스트 framework 시작은 명시된 ttakkari_test 세션 DB를 사용했다. tests/conftest.py는 해당 test DB schema를 rebuild/reset한다. 운영 DB, 별도 initdb, 기존 실패 대상 복원은 사용하지 않았다.
- 프론트/UI, UI/Metal/TTS/host GPU/browser/deploy는 요청대로 반복하지 않았다. 합성 연구 메모 외 실제 model/GPU 실행은 없다.
- 실제 실패 Run 자동 수집, evidence quality의 실질 평가, 수치 정보가치/효과 calibration, Registry recommendation, UI의 문맥 표시, 전체 PMS7.2 및 R1~R16 완료는 검증하지 않았다.

## Git 상태

검증 전 branch는 research-v2, HEAD 85ee4dd90677f349351c5b2fd0a5b5483fc15ba6, origin/main 대비 ahead 3였다. 최종 status에서는 원격 추적 기준이 움직여 ahead 3, behind 1로 표시됐다. 원격 변경은 fetch/rebase/pull하지 않았다.

최종 명령 출력:

    $ git status --short --branch
    ## research-v2...origin/main [ahead 3, behind 1]
    ?? docs/research-lab/evidence/selection-context-review-85ee4dd-raw.json
    ?? docs/research-lab/evidence/selection-context-review-85ee4dd.md

제품 수정, commit, push, merge, deploy는 하지 않았다.
