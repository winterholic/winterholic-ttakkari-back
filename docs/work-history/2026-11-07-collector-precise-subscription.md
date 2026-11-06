# 수집 정밀 판정 구독 에이전트 전환

## 기준과 변경

- 시작 시 공식 `runtime_report` (2026-11-06 15:40Z): 운영 활성 revision `efe07c4bf9332473c60743bf31bb73111be00c4a`, generation `d31bc932-f2ef-48f3-9a42-1814172fe1da`. 이 revision의 격리 worktree에서 수정했다. 작업 중 운영은 알림 설정 변경 revision `5f5cbd997addcb4ea084ab908790f8f6eec54122`로 이동했으며, 정밀 판정 관련 경로의 두 revision 간 diff는 없었다. 최종 변경 커밋은 이 최신 revision 위로 rebase했다.
- 정밀 판정만 기존 Qwen 런타임 대신 KB L2의 `SubscriptionCliL2Adapter`와 tool-free 구독 JSON transport에 연결했다. L2 실행기 설정의 Codex/`gpt-6-luna`를 기본 요청 모델로 쓰며, 문서 scope별 pool, 임시 profile 순위, 한도 쿨다운, 동시성, timeout은 기존 profiles/transport 경로를 따른다.
- 기존 결과 JSON 필드·판정 enum·held 문서 구조와 품질 판정 의미를 유지했다. 입력은 기존 UTF-8 보수적 토큰 상한으로 자르고, 엄격한 출력 schema와 128 KB 응답 상한을 적용한다. 외부 본문은 데이터로만 주고, 도구 실행을 비활성화한다.
- collector teacher 토글은 이 구독 판정에 적용하지 않는다. 기존 collector daily 호출·토큰 상한을 적용하고 account profile의 subscription quota/cooldown을 따른다. persistent source job마다 4회인 collector job cap은 구독 retry를 영구 중단할 수 있어 사용하지 않는다. 일일 상한을 만나면 다음 KST 자정까지 pending으로 둔다. 구독의 추가 API 비용은 0으로 기록한다. CLI가 provider token receipt를 주지 않으므로 토큰 사용은 예약 상한 추정치로 기록하며, 실제 계정명·엔진·요청 모델을 영수증에 남긴다. provider가 확정한 실제 모델명은 이 transport 응답으로 관측되지 않는다.
- 출력 schema 자체에 설명 800자·signals 32개×80자 상한도 지정하고, JSON 출력 전체는 128 KB로 제한한다. known usage-limit/scope-capacity/비소비 오류는 대기 상태·reset 시각·다음 retry attempt로 남긴다. scoped profile이 사전 점검에서 없으면 budget reservation 없이 기다린다. 결과 불명 또는 소비된 오류는 같은 요청을 재호출하지 않고 unresolved로 둔다. claim lease는 240초이며 settled 결과 복구와 DB claim token으로 중복 적용을 막는다.

## 검증과 운영 범위

- RED: `UV_CACHE_DIR=/private/tmp/uv-cache-precise uv run pytest -q unit_tests/test_collection_precise_subscription.py` → 구현 전 예상한 누락 API/status 4건 실패.
- RED 추가: `UV_CACHE_DIR=/private/tmp/uv-cache-precise uv run pytest -q unit_tests/test_collection_precise_subscription.py::test_precise_output_schema_bounds_variable_length_fields` → 구현 전 `KeyError: 'maxLength'`.
- RED 추가: `UV_CACHE_DIR=/private/tmp/uv-cache-precise uv run pytest -q unit_tests/test_collection_precise_subscription.py::test_pending_claim_waits_until_profile_reset_time unit_tests/test_collection_precise_subscription.py::test_unavailable_scoped_profile_does_not_reserve_a_call` → retry wait 누락과 availability API 누락 2건 실패.
- RED 추가: `UV_CACHE_DIR=/private/tmp/uv-cache-precise uv run pytest -q unit_tests/test_collection_precise_subscription.py::test_first_subscription_attempt_keeps_legacy_idempotency_key unit_tests/test_collection_precise_subscription.py::test_day_budget_wait_uses_next_kst_midnight` → key 유지 함수와 KST 일일 reset 함수가 없어 2건 실패.
- GREEN: 전체 collector/L2/precise 회귀 모음과 legacy Qwen 예약 복구·최대 timeout claim lease 경계 회귀를 포함해 실행 → 70 passed.
- 독립 검토 1차에서 day-budget 영구 종결(P1)과 기존 멱등 키 변경(P2)을 발견했다. 수정은 일일 한도 대기 시 KST 다음 자정까지 pending 유지, persistent source-job cap 제외, 최초 시도 키 유지와 legacy Qwen 결과 안전 복구(같은 job/purpose에 한정), 이후 시도만 suffix, subscription CLI의 semaphore+호출 timeout을 덮는 claim lease로 반영했다.
- Lint/whitespace: 변경 Python 파일 대상 `uv run ruff check` → All checks passed; `git diff --check` → no output. 독립 검토자가 P1/P2 수정, legacy 미확정 예약 차단, claim token 경합, settled 결과 복구를 확인했고 남은 결함을 찾지 못했다. DB 지연은 timeout 산식의 보장 범위 밖으로 잔여 한계다.
- 모두 격리 worktree의 모의/fixture 검증이다. 새 실제 구독 호출은 0건이며 운영에는 반영하지 않았다.
- 최종 운영 확인: 활성 revision `5f5cbd997addcb4ea084ab908790f8f6eec54122`; 로보틱스 mission `aa88050a-43a6-4912-bbb8-aad55927e295` running, `precise_waiting=39`, `precise_unresolved=0`; Harness mission `5faff33d-3586-4827-b3a3-71390acb834a` running, `precise_waiting=197`, `precise_unresolved=0`. 이 작업은 큐를 재시작하거나 새 mission을 만들지 않았다. 배포 후 기존 runner가 기존 pending부터 처리할 수 있다.
- 기존 R4 계정 우선순위 확인: 앞선 운영 조율 기록(2026-11-06 15:32Z)은 개인 임시 우선순위에 회사 Codex ai2가 단독 등록되어 2026-11-07 04:24 KST 만료 예정이었고, 실제 배정 worker에서 ai2/Codex/`gpt-6-luna` 사용을 확인했다고 남겼다. 현재 시각에는 임시 기간이 만료됐다. 이후 독립 검토가 같은 계정 내 모델 후보 순서 반례를 발견해 별도 수정을 맡겼으나, 그 수리 상태는 이 세션의 worker 소유권 밖이라 미확인이다. profiles 공통 경로는 여기서 수정하지 않았다.

## 다음 소유자

- 후속 독립 검증 worker가 P1/P2 수정과 scope isolation, account priority/cooldown fallback, schema, 재개 중복 방지를 확인한다.
- 후속 검증 통과 후 배포 소유자가 승인된 배포를 수행하고 기존 큐의 pending/evaluated/engine/account 필드를 재확인한다. 실제 호출 때는 요청 모델과 응답에서 확인된 provider 모델을 구분한다.
