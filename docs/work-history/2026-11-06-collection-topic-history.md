# 수집 주제와 실행 내역 분리

사용자 요청: “이건 뭐 아직도 진행중임? 그리고 내역이랑 그 주제랑 분류해야할듯? 지금이게 자꾸 문제가있으니까 같은 요청이 여러개가쌓이는게 좀 이상하네”

운영 목록을 2026-11-06 14:31~33Z 인증된 ttk로 확인했다. 로보틱스·하네스는 각각 최초·1차 복구(cancelled)와 2차 복구(running), 총 3개 mission이다. 각 주제의 세 요청은 goal SHA가 같고 user_reference는 없다. 혼입 결함 복구의 완료와 백과사전 전체 수집의 완료는 별개다. 당시 적재는 robotics 56, harness 127→131로 진행했다.

변경: 원 지시문·사용자 맥락·명시 참고 URL로 topic_key를 파생한다. 데이터 이관·삭제 없이 기존 시도들을 연결한다. reuse_existing을 사용한 제출은 기존 임무를 열며, 동시 제출은 주제 advisory lock으로 직렬화한다. 명시 복구 request_key와 기존 회차 API는 보존한다. 프론트는 주제/실행 내역/새 수집으로 분리하고 이전 시도에는 현재 수집 링크를 제공한다. 과거 시도의 재실행·주기 등 제어는 숨겨 현재 수집에서 이어가게 한다.

검증: 변경 전 새 통합 검사 4 failed(필드 미지원/topic_key 없음). 변경 후 임시 PostgreSQL `pytest -q --tb=short tests/test_collection_topic_history.py tests/test_collection_missions.py tests/test_collection_scope_operations.py unit_tests/test_collection_domain_routing.py` → 61 passed/9.57s, ruff passed. 두 동시 제출의 동일 ID·mission/job 행 수 보존, 기존 회복본 우선 선택, 맥락/지시문/참고 주소 차이 분리, 페이지 경계와 인증/preview 비변경을 포함한다.

프론트 worktree: ../front-collect-topic-history-20261106. 전체 vitest 1596 passed/10 skipped, 변경 표면 34 passed, TypeScript·대상 ESLint·ui-lint(기존 기준선 138) 및 Vite production build 통과. 브라우저/운영 배포는 이 기록 시점에는 검증 중이다. 최종 상태는 영속 목표 계약과 뒤의 배포 기록을 참조한다.
