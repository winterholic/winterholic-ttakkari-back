# 수집 정밀 판정 구독 에이전트 전환

## 기준과 변경

- 시작 시 공식 `runtime_report` (2026-11-06 15:40Z): 운영 활성 revision `efe07c4bf9332473c60743bf31bb73111be00c4a`, generation `d31bc932-f2ef-48f3-9a42-1814172fe1da`. 이 revision의 격리 worktree에서 수정했다. 작업 중 운영은 알림 설정 변경 revision `5f5cbd997addcb4ea084ab908790f8f6eec54122`로 이동했으며, 정밀 판정 관련 경로의 두 revision 간 diff는 없었다. 최종 변경 커밋은 이 최신 revision 위로 rebase했다.
- 정밀 판정만 기존 Qwen 런타임 대신 KB L2의 `SubscriptionCliL2Adapter`와 tool-free 구독 JSON transport에 연결했다. L2 실행기 설정의 Codex/`gpt-6-luna`를 기본 요청 모델로 쓰며, 문서 scope별 pool, 임시 profile 순위, 한도 쿨다운, 동시성, timeout은 기존 profiles/transport 경로를 따른다.
- 기존 결과 JSON 필드·판정 enum·held 문서 구조와 품질 판정 의미를 유지했다. 입력은 기존 UTF-8 보수적 토큰 상한으로 자르고, 엄격한 출력 schema와 128 KB 응답 상한을 적용한다. 외부 본문은 데이터로만 주고, 도구 실행을 비활성화한다.
- collector teacher 토글은 이 구독 판정에 적용하지 않는다. 기존 collector daily 호출·토큰 상한을 적용하고 account profile의 subscription quota/cooldown을 따른다. persistent source job마다 4회인 collector job cap은 구독 retry를 영구 중단할 수 있어 사용하지 않는다. 일일 상한을 만나면 다음 KST 자정까지 pending으로 둔다. 구독의 추가 API 비용은 0으로 기록한다. CLI가 provider token receipt를 주지 않으므로 토큰 사용은 예약 상한 추정치로 기록하며, 실제 계정명·엔진·요청 모델을 영수증에 남긴다. provider가 확정한 실제 모델명은 이 transport 응답으로 관측되지 않는다.
- 출력 schema 자체에 설명 800자·signals 32개×80자 상한도 지정하고, JSON 출력 전체는 128 KB로 제한한다. known usage-limit/scope-capacity/비소비 오류는 대기 상태·reset 시각·다음 retry attempt로 남긴다. scoped profile이 사전 점검에서 없으면 budget reservation 없이 기다린다. 결과 불명 또는 소비된 오류는 같은 요청을 재호출하지 않고 unresolved로 둔다. claim lease는 `max(240초, 2 × ceil(설정 timeout) + 60초)`이며, settled 결과 복구와 DB claim token으로 중복 적용을 막는다.

## 검증과 운영 범위

- RED: `UV_CACHE_DIR=/private/tmp/uv-cache-precise uv run pytest -q unit_tests/test_collection_precise_subscription.py` → 구현 전 예상한 누락 API/status 4건 실패.
- RED 추가: `UV_CACHE_DIR=/private/tmp/uv-cache-precise uv run pytest -q unit_tests/test_collection_precise_subscription.py::test_precise_output_schema_bounds_variable_length_fields` → 구현 전 `KeyError: 'maxLength'`.
- RED 추가: `UV_CACHE_DIR=/private/tmp/uv-cache-precise uv run pytest -q unit_tests/test_collection_precise_subscription.py::test_pending_claim_waits_until_profile_reset_time unit_tests/test_collection_precise_subscription.py::test_unavailable_scoped_profile_does_not_reserve_a_call` → retry wait 누락과 availability API 누락 2건 실패.
- RED 추가: `UV_CACHE_DIR=/private/tmp/uv-cache-precise uv run pytest -q unit_tests/test_collection_precise_subscription.py::test_first_subscription_attempt_keeps_legacy_idempotency_key unit_tests/test_collection_precise_subscription.py::test_day_budget_wait_uses_next_kst_midnight` → key 유지 함수와 KST 일일 reset 함수가 없어 2건 실패.
- GREEN: collector/L2/precise, legacy 예약 복구·claim lease 경계, 두 concurrent unhold/precise race 회귀 → 72 passed (6.81s).
- 독립 검토 1차에서 day-budget 영구 종결(P1)과 기존 멱등 키 변경(P2)을 발견했다. 수정은 일일 한도 대기 시 KST 다음 자정까지 pending 유지, persistent source-job cap 제외, 최초 시도 키 유지와 legacy Qwen 결과 안전 복구(같은 job/purpose에 한정), 이후 시도만 suffix, subscription CLI의 semaphore+호출 timeout을 덮는 claim lease로 반영했다.
- Lint/whitespace: 변경 Python 파일 대상 `uv run ruff check` → All checks passed; `git diff --check` → no output. 독립 검토자는 내장 subagent `/root/precise_eval_independent_review`로, Ttakkari 공식 자식 worker UUID는 없다. 최종 검토 revision `b51df6fd70c9dfa008d3ae3977b184bf8d9180e9`; `git diff --check 2de842e8477e9a2538f7862a507b68dbcfb3b3e7..b51df6fd70c9dfa008d3ae3977b184bf8d9180e9` → 무출력. 검토자는 final diff의 11개 파일이 같은 scoped patch이고, 직전 승인 revision `6c83932a3f5ce7365bda34584210a74873d300b9` 이후 변경은 테스트 fixture 2곳뿐임을 확인했다. 앞서 보고된 대상 회귀 70 passed (6.56s)의 정확한 argv는 현재 보존된 검토 메시지에 없다. reviewer test log를 지어내지 않고, 최종 revision의 diff-check를 정확한 실행 증거로 보존한다. DB 지연은 timeout 산식의 보장 범위 밖이다.
- 모두 격리 worktree의 모의/fixture 검증이다. 새 실제 구독 호출은 0건이다. main에 push 후 배포 예약은 접수됐지만, 운영 적용은 배포 상태 확인 전이라 미확정이다.
- 배포 전 기준 스냅샷: 활성 revision `5f5cbd997addcb4ea084ab908790f8f6eec54122`; 로보틱스 mission `aa88050a-43a6-4912-bbb8-aad55927e295` running, `precise_waiting=39`, `precise_unresolved=0`; Harness mission `5faff33d-3586-4827-b3a3-71390acb834a` running, `precise_waiting=197`, `precise_unresolved=0`. 이 작업은 큐를 재시작하거나 새 mission을 만들지 않았다.
- 기존 R4 계정 우선순위 확인: 앞선 운영 조율 기록(2026-11-06 15:32Z)은 개인 임시 우선순위에 회사 Codex ai2가 단독 등록되어 2026-11-07 04:24 KST 만료 예정이었고, 실제 배정 worker에서 ai2/Codex/`gpt-6-luna` 사용을 확인했다고 남겼다. 공식 account_usage의 관측 시각 `2026-11-07 01:25 KST`에는 `04:24 KST` 만료 전이므로 임시 우선순위가 아직 유효했다. UTC 시각을 KST와 혼동하지 않는다. 이후 공식 runtime_report의 `2026-11-06 16:10Z` 스냅샷에는 “임시 순서 내 모델 선택 반례 교정” run이 running으로 나타났고, 완료 여부는 확인하지 않았다. profiles 공통 경로는 여기서 수정하지 않았다.

## 이번 후속 확인과 남은 일

- 공식 배포 예약 `4974a443-e8d4-4223-82d1-bb63f67b3980`, revision `b51df6fd70c9dfa008d3ae3977b184bf8d9180e9`를 한 번 조회했다. 정확한 상태는 `requested`이며 중복 예약하지 않았다. 이 상태는 운영 활성화 증거가 아니다.
- 배포가 `requested`라 이번 확인에서는 runtime_report나 mission status를 추가 호출하지 않았다. 기존 두 mission의 실제 evaluation receipt, pending/evaluated/engine/account 변화는 미확인이다. 신규 실제 구독 호출 0건이므로 계정 소진 대기와 reset 시각도 발생/관측되지 않았다. 개인 문서의 회사 pool 제외 경계는 바꾸지 않았다.
- 후속 담당자는 예약 상태가 실제 활성 generation/draining으로 바뀐 뒤에만 기존 두 큐의 receipt와 counts/engine/account를 조회한다. 취소/재시작/새 mission 없이 기존 허용 범위만 확인한다.

## L2 임시 순서 교정 후속 (2026-11-07)

- `git fetch origin main`의 `FETCH_HEAD`는 `adba28d2e0aca7d886e7c345f12178793d17c727`였다. 이 SHA를 기준으로 한 격리 변경은 `7df97f11`(이 문서의 시각·검증 증거 정정과 독립 검토 문서 보존), `a48062f1`(precise L2에서 문서 pool override ID를 `profiles.choose_async(..., priority_ids=...)`로 전달) 순서다. `0b66498b`는 이 기준의 조상이 아니며, 그 정정 내용은 `7df97f11`에서 충돌 없이 재적용했다.
- busy 지정 계정 반례는 실제 chooser 경로를 사용한다. 이전 어댑터 `7df97f11`에서 같은 테스트를 실행하면 지정 계정이 이미 사용 중일 때 `idle-second`가 선택되어 실패했고, 교정본 `a48062f1`에서는 `temporary-first`가 선택되어 통과했다.
- 교정본 격리 clone 검증: `env -u TTAKKARI_TEST_DB UV_CACHE_DIR=/private/tmp/uv-cache-l2-final uv run --frozen python -m pytest -q unit_tests/test_collection_precise_subscription.py unit_tests/test_collection_precise_eval.py` → `27 passed in 0.36s`; `UV_CACHE_DIR=/private/tmp/uv-cache-l2-final uv run --frozen ruff check src/ttakkari/kb_pipeline/l2_adapters.py unit_tests/test_collection_precise_subscription.py` → `All checks passed!`; `git diff --check adba28d2..HEAD` → 출력 없음.
- 통합 회귀 묶음은 `env -u TTAKKARI_TEST_DB UV_CACHE_DIR=/private/tmp/uv-cache-l2-final uv run --frozen python -m pytest -q unit_tests/test_collection_precise_subscription.py tests/test_collector_budget.py unit_tests/test_collection_precise_eval.py tests/test_kb_v3_subscription_l2.py tests/test_kb_v3_visibility_and_l2_adapters.py tests/test_kb_v3_subscription_cli_contracts.py tests/test_kb_v3_l2_budget_boundaries.py tests/test_collection_missions.py::test_precise_result_survives_worker_crash_after_budget_settlement tests/test_collection_missions.py::test_unknown_precise_call_is_unresolved_without_refund_or_retry tests/test_collection_missions.py::test_precise_day_budget_denial_leaves_item_pending_until_reset` 실행 중 `tests/conftest.py`의 임시 PostgreSQL `initdb`가 종료 코드 1로 실패해 수집 단계에서 중단됐다. 이 격리 command environment에서 통합 묶음은 미검증이다.
- 작업 지시가 제공한 01:47 KST 운영 스냅샷: 공식 runtime active `5f5cbd997addcb4ea084ab908790f8f6eec54122`, 누적 배포 `c28426ad-4b9e-490b-a79a-00067920bd1e`의 `adba28d2` revision은 `requested`. 이 값은 이번 작업에서 재조회하지 않았다. 대기 폴링·새 mission·실제 구독 CLI 호출·push·배포는 하지 않았다. 다음 독립 검증은 새 컨텍스트에서 진행한다.
