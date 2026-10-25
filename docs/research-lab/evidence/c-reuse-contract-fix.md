# C 재사용·해석 계약 교정 — 2026-10-26

기준 backend `0f675591` / frontend `e1158f0`.
독립 인수 보고서: `c-reuse-review-763dc03-e1158f0.md`.
frontend 교정: `5a2650fde8d5c46f50c9f08ce67f14917f14da98`.

## 판정과 교정

- F3: NodeIn은 extra를 거부하고 근거 없는 노드는 서버가 unverified로 저장한다.
  frontend에서 epistemic_status 전송을 제거했다. 성공/실패 해석의 실제 ASGI 저장,
  resume 재조회, 조건·본문·unverified 보존을 확인했다. 임의 지식 상태를 허용하도록
  서버 스키마를 넓히지 않았다.
- F4: 재사용은 원본 Configuration.parameters로 초기화하며 method/common 기본값을
  덧붙이지 않는다. 예를 들어 원본 k=5, lexical_overlap에 top_k=20이나 dense_weight=0.6이
  새로 생기지 않는다. 사용자가 k=7로 바꾸면 실제 요청·주지표도 7을 따른다.
  처음에는 single 설정으로 복제하고 이후 사용자가 탐색을 선택한다.
  생성 후 재사용 초기화 URL 쿼리도 제거한다.
- F1: top_k가 고정값이든 탐색 배열이든 k 탐색은 무시되므로 둘을 함께 탐색하면 거부한다.
  /validate와 생성 API가 같은 adapter_id/strategy를 받아 판정한다.
- F2: shadowed 검사는 retrieval_eval의 실제 탐색 전략(ab/grid/random/bayesian/ablation)에만
  적용한다. single/baseline/repeat와 external_report의 동명 필드는 막지 않는다.
  보고서의 fixed top_k + k sweep + top_k sweep은 k가 여전히 무시되므로 거부가 맞다.
  안내는 "k 탐색을 끄고 top_k만 탐색"으로 정정했다.
  상속된 top_k를 None으로 바꾸는 삭제 규칙은 추가하지 않았다. 원본 top_k가 있으면
  top_k만 탐색하면 된다. 실제 검색 개수에는 정수가 필요하다는 기존 계약을 유지한다.
- 인접 결함: source adapter 생략 시 resolve_aliases가 먼저 retrieval_eval을 넣어서
  external_report 원본 재사용이 422였다. 원본이 있으면 어댑터 기본값은 원본에서
  상속하고, 명시적으로 다른 어댑터를 요청한 경우의 거부는 유지한다.

source_configuration_id는 재사용에 필수(없으면 422)다. 설정이 없으면 404,
설정에서 유도한 study/experiment와 요청 ID가 다르면 422 mismatch,
명시한 run 자체가 없으면 404다. 기존 테스트/구조 계약을 그대로 유지한다.
마이그레이션 0073/출처 roundtrip은 변경하지 않았고 이전 독립 검증 범위를 재사용한다.
F5(짧은 출처 배지·모니터 표시)는 낮은 우선순위의 표시 개선으로 남아 있다.

## 실행 근거

Codex 명령 환경, 표준 주입 TTAKKARI_TEST_DB URL identity를
ttakkari_test / ttk_test / loopback으로 검사했다. 비밀 DSN은 출력하지 않았다.

제품 수정 전 실제 ASGI+PG 반례:

```text
pytest test_research_v2_api_contract.py -k 'shadowed_cutoff or interpretation_node'
3 failed, 5 passed, 57 deselected in 1.03s
pytest test_research_v2_api_contract.py -k reuse_defaults_to_source_adapter
1 failed, 66 deselected in 0.80s
```

frontend RED: 미지원 epistemic_status 필드 전송으로 1건 실패.
재사용 probe의 최초 실행은 scope 쿼리가 붙은 method URL을 잘못 매칭했다.
이를 정정한 뒤 제품 변경 전 다시 실행해 top_k 입력이 빈값 대신 20인 assertion 실패를 확인했다.

교정 후:

```text
.venv/bin/python -m pytest -q tests/test_research_v2_api_contract.py tests/test_stage2_defects_fix.py tests/test_research_v2_core.py tests/test_research_v2_e2e.py tests/test_research_adapters.py
135 passed in 6.22s
pnpm exec vitest run src/pages/research
75 passed
```

frontend tsc -b / 변경 파일 eslint / build(exit 0, PWA 93 entries), 양쪽 diff --check 통과.
backend 변경 4개 Python 파일은 parent 대비 새 Ruff 진단 0건(전체 lint 통과 주장은 아님).

첫 확장 회귀에서는 stage2 테스트의 adapters.get_adapter_registry 직접 대입이
뒤의 실제 RAG E2E로 새어 unknown adapter 실패도 났다. 테스트 autouse fixture로
원래 lookup을 매번 복원하여 같은 순서로 위 135건을 통과했다. 제품 registry를 우회하지 않았다.

이 증거는 API/폼 계약과 관련 회귀 범위다. 새 실제 브라우저 저장·reload 및
원본 k=5 무수정 재실행은 별도 새 독립 검증에 넘긴다. 실제 Metal과 배포는 미실행이다.
