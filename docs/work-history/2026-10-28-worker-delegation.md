# 작업 세션 1단계 위임 + spawn-queue 일반화

## 범위와 판정

사용자 요청 3건 중 코드 수정분이다: (a) "배포가 안된다는 세션"의 이상 현상 수정,
(b) 에이전트가 다른 에이전트를 편하게 호출. 배포 예약 자체는 이 Muse 세션에
예약 도구가 없어 작업지시서로 넘긴다(별도 파일).

핵심 관측: 배포된 세대는 e5b4dfa7(읽기 전용 도구 탐색 허용) 이전이다. 이 세션의
`tool_search` 호출이 구 가드의 거부 문구로 막힌 것이 증거다. main 에는 수정이
쌓여 있는데 배포가 안 되니 같은 증상이 반복된다. 배포 API(POST /api/agent/deploy)는
역할 제한이 없어 도구만 보이면 어떤 세션에서든 예약할 수 있다.

## 변경

- `src/ttakkari/runs/worker_routing.py`: 순수 규칙 `can_spawn`(오케스트레이터 항상,
  작업자는 부모가 오케스트레이터일 때만), `owns_worker_target`(직계 자식은 항상,
  손자는 오케스트레이터만), 상수 `WORKER_SPAWN_MAX_ACTIVE = 5`.
- `src/ttakkari/api/orchestrator.py`: `_manager`(오케스트레이터·작업자만),
  `_spawner`(1단계 상한), `_owned_worker`(손자 포함 소유권)로 spawn/message/
  verification/list/get/stop 6종을 일반화. metrics·context·stop-all·계정·벤치는
  오케스트레이터 전용 유지. chat/desk 는 기존대로 403.
- `src/ttakkari/orchestrator/mcp_server.py`: 작업 관리 5종(spawn/message/list/get/
  stop)을 양쪽 역할에 등록. list/get 은 query_tool 유지로 읽기 힌트 동일.
  docstring·에이전트 안내문에 1단계 규칙 명시.
- `src/ttakkari/security/muse_hook.py`: spawn/message 를 역할 알려진 세션에 허용.
  상한·소유권은 DB 를 보는 서버가 강제한다. 역할 없음/알 수 없음은 계속 거부.
- `src/ttakkari/runs/manager.py`: `_replay_spawn_queue` 호출 조건을
  `qwen-local and is_orch` 에서 `is_orch` 로 일반화. 큐가 없으면 기존과 동일 무동작.
- `src/ttakkari/orchestrator/runtime.py`: ORCHESTRATOR_BODY 에 한 줄 추가(작업자 1단계
  위임 가능 + 위임 도구가 안 보이면 spawn-queue/*.json 탈출구).
- 테스트: `unit_tests/test_worker_delegation.py` 신설(규칙·역할별 stdio 등록·훅),
  `test_muse_hook_admission.py`·`test_muse_guard.py` 계약 변경 반영,
  `tests/test_route_inventory.py` MCP_ORCHESTRATOR_ONLY 에서 5종 제외,
  `tests/test_orchestrator.py` 에 1단계 생성·소유권·상한·격리 e2e 2건 추가.

## 검증

- ruff check 변경 11파일 통과. ruff format 은 저장소 전체가 미준수라 맞추지 않음.
- 관련 유닛 89 passed(위임·Muse 가드/어댑터/바인딩·knowledge_mcp·Qwen 비상·Copilot parity).
- 음성 대조: src 만 원복하면 위임 테스트 11건 실패(변경 감지 확인).
- 전체 unit_tests 전후 비교: FAILED 24건·ERROR 13건 집합 완전 동일(둘 다 기존·환경성),
  passed 2212 → 2223(+11 신규 위임 테스트 전부 통과).
- DB/loopback 테스트(tests/)는 이 샌드박스에서 setup 불가라 미실행. 배포 게이트가
  전체를 다시 돌린다. e2e 2건은 기존 헬퍼(_session/_orch/wait_run/_wait)만 사용.

## 남은 일

- 배포 후 확인: 작업자 세션에서 spawn_worker 1단계 호출, 손자 spawn 403, 큐 재생.
- 호출부 일반화 e2e(비 Qwen 기획 Run 시작 시 재생)는 하네스상 작성이 어려워
  직접 호출 e2e + 리뷰로 대체했다. 여유가 되면 추가한다.
- Qwen 작업자(MCP 없음)는 기존대로 부모에게 보고하는 경로 유지. 설계상 한계다.
