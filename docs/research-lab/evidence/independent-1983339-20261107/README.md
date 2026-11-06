# Independent Research Lab evidence carry-forward (2026-11-07)

이 폴더는 1983339 독립 검증의 기존 결과를 재실행 없이 보존하고, 별도 요청으로 받은 보존 SHA `ca1fa23419017ed63857114a61c2646478bfd47f`의 좁은 반례를 추가한다. ca1fa234 이후의 미커밋 working-tree 변경이나 새 수정 SHA는 판정 대상이 아니다.

## A. 1983339 검증 재사용 기록

아래 결과는 이전 독립 검증자의 완료 보고와 이번 작업 카드에서 인계된 숫자에 따른 **과거 실행 결과**다. 이번 세션에서 재실행하지 않았다.

| 확인 | 과거 보고된 명령/출력 | 이번 보존 상태 |
|---|---|---|
| Research unit tests | `UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest unit_tests -k research -q` → `104 passed` (카드에서 유효 결과로 지정) | 재실행하지 않음. 이전 응답의 원 stdout 로그는 제공된 임시 checkout에서 찾지 못해 숫자만 과거 보고로 인용 |
| 격리 DB retry 회귀 | 이전 완료 보고: `50 passed` (격리 PostgreSQL 실행/재시도 회귀) | 재실행하지 않음. 정확한 pytest 인자·stdout 로그는 전달 자료에 없음 |
| 임시 반례 | 이전 완료 보고: `pytest` → `4 passed` (retry 성공/실패, 일부 trial 실패, 중단 계열을 검증했다는 보고) | 원 `.py` 파일은 검색되지 않음. 찾은 `test_review_counterexamples_tmp.cpython-313-pytest-9.1.1.pyc`를 `historical/`에 복사했으나 이는 원문 소스가 아니며 사람이 검토 가능한 소스를 복원했다고 주장하지 않음 |

재사용 시 위 세 결과를 현 SHA의 새 검증 결과와 합산하지 않는다. 원 1983339 독립 보고의 세부 맥락은 대화 인계에만 남아 있고, 명령 인자/출력 전체를 원본 로그로 입증할 수 없는 항목은 위 표처럼 제한을 표시한다.

## B. 과거 findings의 적용 범위 교정

### `raw_output` sentinel

`service.out()`은 ORM row의 모든 컬럼을 반환하고, `run_out()`은 `raw_output`을 제거하지 않는다 (`src/ttakkari/research/service.py:41-63`). `GET /api/research/runs/{run_id}`는 응답 계약에 raw output과 environment snapshot이 포함된다고 명시하며 `User` 의존성을 받는다 (`src/ttakkari/api/research.py:1257-1282`). 인증은 API Bearer JWT의 `sub=owner`를 확인한다 (`src/ttakkari/security/auth.py:58-70`). 따라서 synthetic sentinel이 `raw_output`에 되돌아온 사실은 **인증된 owner에게 원자료를 반환하는 동작의 증거**다. 그 사실만으로 unauthenticated 또는 scope 밖의 실제 비밀 노출이라고 할 수 없다.

Research 사용자 API는 고정 owner 인증 모델이고 personal/work는 데이터 분류다. 읽기 API의 run 상세 handler는 scope별 접근 필터를 적용하지 않는다. Agent API는 `session.pool`로 기본 write/query scope를 정하지만, 해당 코드도 owner 전체 study 접근을 전제로 설명한다 (`src/ttakkari/api/research.py:1643-1648`). 이번에는 별도 사용자나 다른 scope로 접근하는 런타임 negative test를 하지 않았다. 그러므로 **다른 인증 주체로의 노출 여부는 미검증**이며, scope를 tenant 보안 경계라고 단정하지 않는다.

원자료 생산/소비 계약에서 LLM adapter는 실제 입력 prompt 및 bench 응답을 `raw_output`에 보존하려고 한다 (`src/ttakkari/research/adapters/llm_inference.py:451-466`). `execution_owner_receipt()`는 owner metadata를 receipt에 포함하고, token은 실행 소유권 lock identity로 사용된다 (`src/ttakkari/research/ownership.py:42-62`). 이 snapshot/receipt는 인증된 owner가 받는 operational provenance다. 현재 코드 계약에는 임의 adapter 출력이나 snapshot의 secret-like 필드를 redacting하는 단계가 보이지 않는다. 이번 fixture는 synthetic 문자열만 사용했고 운영 데이터/실제 비밀값은 검사하지 않았다. 따라서 판정은 “의도된 owner 원자료 반환; 인증 밖 유출은 입증되지 않음; 민감 원자료의 redaction 보장은 확인되지 않음”이다.

### 보존된 과거 pagination finding과 문서 revision 오기

이전 검증 보고는 snapshot에서 `runs`/`best_results`가 잘리더라도 `has_more` 및 `totals`가 그 collection들을 세지 않는 501-run 반례를 기록했다. ca1fa234의 snapshot 구현도 `has_more` 검사에 nodes/relations/experiments/events만 포함하고 runs/best_results는 잘라 반환한다 (`src/ttakkari/research/service.py:397-412, 450-462`). 이번 좁은 검증에서는 이 반례를 재실행하지 않았다.

기존 recovery 문서의 base SHA `521f469a92440317e040adba15e0646c8e310036`은 앞 8자리 뒤가 `924...`인 값으로 잘못 기록됐다. 기준 문서와 실제 기존 보고 출력에 기록된 SHA는 `521f469aeb27a28a897bcac55fa969b1a5c2323e`다. 이전 검증의 SHA 불일치는 원 문서의 증거 오류로 보존한다.

## C. `ca1fa234` narrow 검증

### 고정 대상과 격리

- Exact SHA: `ca1fa23419017ed63857114a61c2646478bfd47f`
- Detached worktree: `/private/tmp/research-lab-ca1fa234-narrow`
- `git rev-parse HEAD` 출력: `ca1fa23419017ed63857114a61c2646478bfd47f`
- 원 구현 worktree는 시작 시 수정 상태였다. 이 검증은 그 소스 트리를 건드리지 않았고, 반례 코드는 detached tree에만 작성했다.
- GPU / 운영 DB / API 호출 없음. adapter 통합 테스트는 fake `HostGpu`로 바꾸며, 추가 반례도 synthetic row와 mock adapter만 사용한다.

### 실행 결과

성공한 exact-SHA adapter/prompt 검증:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest unit_tests/test_research_regna_improvements.py -k 'snapshot_includes_configurations or preserves_prompt' -q
..                                                                       [100%]
2 passed, 3 deselected in 0.43s
```

첫 전체 파일 실행은 테스트 자체가 사용자 protected runtime lock 경로를 쓰려다 sandbox에 막혔다. 재현 명령과 관측 출력:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest unit_tests/test_research_regna_improvements.py -q
2 failed, 3 passed in 3.49s
PermissionError: [Errno 1] Operation not permitted: '/Users/winterholic/.ttakkari/runtime/research/<uuid>.lock'
```

실패 stage는 pytest DB-free 테스트의 `ExecutionOwner.acquire()`가 테스트 lock 파일을 만들려 한 지점이다. 같은 protected 경로를 재시도하지 않았다. 추가 테스트는 detached tree에서 `ExecutionOwner`를 mock해 해당 경로를 건드리지 않았다.

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest unit_tests/test_ca1fa234_narrow_counterexamples_tmp.py -q
.F.F                                                                     [100%]
2 failed, 2 passed in 0.48s
```

`ca1fa234`의 **unsafe 관측** (이 테스트들이 통과한 것을 안전 통과로 수용하지 않음):

1. `test_adapter_failed_with_memory_guard_error_stays_failed`는 adapter가 `failed/adapter_failed`를 반환하고 memory guard 오류가 발생해도 주상태가 `failed`로 남는 것을 기대한다. 이 기대는 안전하지 않다. guard가 발동하면 배치 중단을 나타내는 `cancelled`가 주상태여야 하며, adapter 실패는 보조 receipt에 보존해야 한다.
2. `test_unknown_cancelled_run_does_not_stop_later_batch_sibling`은 인식되지 않은 cancel code `adapter_internal_abort` 뒤 형제 Run도 실행되는 것을 기대한다. 이 기대는 안전하지 않다. cancellation 사유를 알 수 없어도 후속 Run을 실행하면 안 된다.

현재 교정본에서는 별도 회귀로 안전 명세를 검증했다:

- `unit_tests/test_research_regna_improvements.py::test_memory_guard_stops_failed_adapter_and_preserves_adapter_receipt`: `cancelled/memory_guard_stopped` 주상태 및 adapter 원 오류 receipt 보존.
- `tests/test_research_runner_lifecycle.py::test_any_cancellation_stops_the_rest_of_the_batch`: unknown 및 memory-guard cancellation 모두 첫 Run 뒤 후속 Run 미실행.
- Adapter-local TimeoutError 실패 분류, 실제 deadline 예산 중단, preview fallback의 `response_completeness`/`response_source`도 현재 회귀에서 별도로 확인했다. 상세 명령과 출력은 보존 worktree의 [실용화 recovery evidence](../research-lab-practical-recovery-20261106.md)에 기록한다.

실패 반례:

1. 실험 deadline이 지정되지 않았고 아직 만료되지 않은 상태에서 adapter가 `TimeoutError("synthetic adapter-local timeout")`를 던지면 현재 catch 절은 `cancelled/budget_exceeded`로 분류함. 테스트는 `failed/TimeoutError`를 기대했으며 관측은 `cancelled`였음. 원인 후보는 `runner.py:414-418`의 무조건 `isinstance(e, TimeoutError)` 분기. 이는 해당 adapter 오류와 실제 실험 deadline 만료를 구분하지 못하는 **재현된 결함**임.
2. preview-only response는 `sample_trajectories[0].response`에 들어오지만 `response_completeness`가 없어 전체 생성 결과와 preview fallback을 구분할 수 없음. 해당 필드를 기대한 synthetic 반례는 `None != "preview_fallback"`로 실패함. 원인은 `llm_inference.py:460`에서 `content or output_preview`를 한 필드로 합치면서 출처/완전성을 기록하지 않는 점.

반례 소스는 [`ca1fa234-counterexamples.py`](ca1fa234-counterexamples.py)에 보존했다. 실패 assertion은 target 동작을 기대값과 비교해 발견한 것이며, 소스 코드는 고치지 않았다.

### prompt/full output/sample trajectories 판정

정확한 ca1fa234 소스와 통과한 테스트에서 prompt가 raw output 및 trajectory에 저장되고, `content`가 있을 때 trajectory response에 전체 content가 들어가는 점은 확인됐다. Adapter가 받은 원 `bench_result.runs`도 같은 `raw_output` 안에 보존된다. 하지만 preview-only fallback에 `response_completeness`/`response_source`가 없는 점은 위 실패 반례로 확인했다. 따라서 full content 보존은 조건부 통과, sample trajectory의 완전성 구분은 실패다.

### routing account lookup

제공된 MCP 도구 중 `account_usage` 또는 `worker_routing` 조회는 노출되지 않았다. 확인 가능한 `mcp__ttakkari__execution_capabilities({})`는 실행 환경 정보만 반환하고 `routing.selected.account`를 포함하지 않았다. 따라서 실제 선택 account 이름은 **profile 미확인**으로 남긴다.

## D. 판정 범위

이 기록은 보존 SHA ca1fa234에 대한 좁은 finding이다. 다음 수정 SHA는 판정하지 않았다. 1983339의 과거 104/50 결과를 반복하지 않았으며, 전체 test/lint, PostgreSQL 통합 테스트, UI/API runtime, 운영 scope negative test도 실행하지 않았다.
