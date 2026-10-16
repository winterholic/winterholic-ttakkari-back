# 2026-10-16 작업자 배정·한도 인계 복구

## 현재 상태 (22:08 KST 이후 검증 기준)

- 중단된 worker `7ec7294c-a04a-4053-afda-dd42959aaa3e`의 코드 `d76f13cb04251f1be60be9e7307dd161d7af9b26`를 보존한 채 이어받았다. 두 기존 Run은 이미 cancelled이며 다시 실행하지 않는다. 기존 provider 대화 ID와 이벤트도 변경하지 않았다.
- 운영 `/api/version`과 읽기 전용 runtime_generations 조회는 `ee700630e8c62456cce0b304c8c689227ae6feae`, PID 42581, active `http://127.0.0.1:8791`을 가리킨다. 해당 release 경로에 구 라우터가 있다. 이 시점에는 새 정책이 운영에 적용되지 않았다.
- 운영 이벤트: 원래 Run은 20:17 시작, 21:06 Argon `Individual quota reached`, 이후 `run.waiting`의 재개 시각이 10/23 13:45 KST로 연장됐다. 구 `_run`은 고정 antigravity 엔진에서만 후보를 찾았다. 신규 사용자 지시도 같은 세션 뒤에 대기했다. 코드 추론과 이 실제 이벤트 관측을 구분한다.
- GPU 이력 `f4fe3bf066ee7cef64b1a36ffe7a5c03e085d893`의 main 커밋을 별도 브랜치에 통합했다. 두 0036 head를 계약 검사에서 재현한 뒤, 미배포 라우팅 migration을 `0037_worker_routing`으로 옮겨 `0035_qwen_token_usage → 0036_system_resource_samples → 0037_worker_routing` 단일 그래프로 정리했다. generation protocol은 1이며 기존 online source들과 0036 source를 허용한다. canonical의 별도 Qwen 접수 수정은 진행 중이어서 건드리지 않는다.

## 이번 수정

1. 새 세대의 coordinator가 queued quota wait를 현재와 같은 실행 정책으로 재평가한다. 다른 적합 후보가 생기면 저장된 주간 reset 전에 같은 Run을 재개한다. active generation의 control 잠금과 Run 행 잠금, lease 부재 검사를 사용한다. cancelled/running/기존 소유권/재시작 backoff는 변경하지 않는다. 전부 막혔거나 strict_engine이면 원래 대기를 유지한다.
2. 요청 모델을 `run.usage.routing_request`에 보존해 실제 마지막 fallback 모델과 분리한다. 구 Run은 원래 세션 모델로 복구한다. Argon reasoning이 재개 과정에서 Sonnet/general로 내려가지 않으며, native Sol high/Opus medium을 유지한다. NULL task_type인 구 auto worker도 원래 모델에서 작업 유형을 추론한다.
3. 모델이 다른데 legacy account-only provider 대화 ID를 재사용하지 않는다. Argon ID는 보존하고 Opus는 별도 `account_id:model` 키로 시작한다. 맞는 모델의 저장된 provider ID는 resume한다. 인계문·기존 checkpoint·진행 이벤트와 부작용 확인 지시는 유지한다.
4. 일반 권한 거부 줄의 파일/도구 이름에 `quota` 또는 `429`가 있어도 공급자 한도로 분류하지 않는다. 다른 줄의 실제 공급자 limit은 계속 감지한다. 권한/샌드박스 실패를 다른 공급자에서 재실행하는 경로를 만들지 않는다.

`d76f13c`의 task별 모델·pool별 quota·strict_engine·Flash 독립 검증 표시는 이어받는다. general은 Sonnet 우선, routine은 범위가 명확한 반복·추출에 Flash를 활용한다. 복잡한 장기 자율 완수·아키텍처 최종 결정·UI 조작·독립 검증을 Flash의 보편적 강점으로 주장하지 않는다. 사용자 제공 외부 벤치마크 수치는 미검증 주장이다. 검증 결과/false_completion은 오케스트레이터가 기록한 관측이며 모델 성능의 보편적 비율이나 자동 판정이 아니다.

## 검증 증거

이번 원시 증거: `/Users/winterholic/.ttakkari/data/outbox/99e9c324-de0e-44cf-bbe7-bc2456219aa0/`.

- d76 baseline: `pytest tests/test_worker_routing.py` → 41 passed (8.78s). 아래 예전 e9/4da의 전체 검사를 d76 검사로 재사용하지 않았다.
- `recovery-red.txt`: 주간 timer 고착과 재개 모델 손실 2 failed, 5 passed. `isolation-red.txt`: 다른 모델의 legacy 대화 ID 재사용과 권한 문구 오분류 5 failed.
- `focused-regression.txt`: worker routing, account selection, quota model routing, 모델 정책, orchestrator API/MCP 계약, generation 소유권·process·배포·bootstrap·graph 241 passed (58.58s). 실제 provider를 호출하는 검사가 아니라 synthetic CLI와 격리 PostgreSQL을 사용했다.
- `final-migration-online.txt`: 격리 DB에서 0035 설치 → 운영 ee70063 ORM으로 running/queued·provider ID·handoff·checkpoint 저장 → online 0036 GPU/0037 routing upgrade → 새 ORM 보존 검사 → 구 ORM read/heartbeat write 성공. 운영 migration 직접 실행 없음. 초기 `migration-online.txt`는 GPU 통합 전 0036 routing 검사 이력이다.
- `routing-production-replay.json`: 저장된 운영 quota/cooldown/lease를 읽기 전용 재생했을 때 Argon·AGY Claude pool 소진, Flash 여유에도 reasoning은 native Codex `gpt-6.1-sol/high` 선택. 이는 운영 새 라우터 실행·실제 모델 응답·MCP·CLI resume 증거가 아니다.
- `ruff check src tests alembic` 및 `git diff --check` 통과. 저장소 전체 `ruff check .`는 변경하지 않은 Qwen experiments에서 기존 72개 오류를 보고했다(`lint.txt`). 이 파일들은 수정하지 않았다.

- 최초 전체 회귀 `full-regression.txt`: 1166 passed, 2 failed, 3 skipped, 2 xfailed, 6 subtests passed (278.42s). 1건은 모델 생략 legacy AGY resume의 정규화 누락으로 재현 후 수정했다(`legacy-default-red/green.txt`, 관련 64 passed). 다른 1건은 fresh-context 검사가 동시 메인 작업 보고를 worker로 잘못 읽은 테스트 경쟁이었다. 정확한 Run outbox로 실행을 찾도록 보완해 원래의 새 대화·과거 문맥 차단 조건은 유지했다. 최종 통합 전체 회귀를 새로 실행한다.

운영 새 정책의 실제 모델 응답·MCP 호출·provider resume는 미실행이다. 배포 예약 접수와 성공을 구분하고, 예약 후 활성 Run 종료를 기다리며 폴링하지 않는다. 최종 전체 회귀·통합 SHA·배포 예약 결과는 후속 항목에 기록한다.

## 최종 통합 검증 (22:17 KST)

- 실행 코드 `3223fd3`(GPU main 통합 및 0037 graph)에서 `PYTHONPATH=src TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_routing_99e9 .venv/bin/python -m pytest -q --tb=short --junitxml=.../final-full-regression.xml` → **1175 passed, 3 skipped, 2 xfailed, 6 subtests passed (263.37s)**. `final-full-regression.txt/xml` 원시 출력 보존.
- `ruff check src tests alembic` → All checks passed. `alembic heads` → 0037_worker_routing 단일 head. `git diff --check` 통과.
- 코드 검사에만 필요한 임시 `.venv` 링크는 제거한다. 기존 canonical의 실제 `.venv` 및 다른 작업자의 파일은 그대로 둔다.
- 이 검증 시점 운영은 여전히 ee70063이고, canonical에는 별도 Qwen 접수 수정이 staging되어 있다. 후속 통합과 배포 예약은 source/cleanliness를 다시 확인한 뒤 수행한다. 예약의 성공 상태는 독립 controller가 정한다.

## 앞선 작업의 이력 (현재 상태로 해석하지 않음)

### 초기 auto 라우팅 구현·배포 보류 기록

사용자는 네이티브 Codex/Claude 쏠림과 Antigravity 저활용 진단 후 꼼꼼한 개선·검증을 요청했다. 원인은 worker auto의 Antigravity 제외, 도구 안내와 최신 역할 정책의 충돌, 모델/작업 성격보다 계정 여유를 먼저 비교하는 기본 경로였다. 메인·접수는 사용자 기존 결정대로 native 정책을 유지한다.

## 변경 계약

- `runs/worker_routing.py`를 단일 정책 원본으로 두고 신규 auto 작업자에 적용한다. general=Antigravity Sonnet→Flash→native Sonnet/Luna, routine=Flash→Luna→Antigravity Sonnet→native Sonnet, design=Antigravity Sonnet→native Sonnet, verification=Luna→Antigravity Sonnet→Flash→native Sonnet, reasoning=Argon→Antigravity Opus→native Sol/Opus다. 같은 단계의 계정은 기존 Run 당 여유로 분산한다. reasoning 및 peer 모델은 사유를 요구하며 native peer effort도 기존 Sol high/Opus medium으로 맞춘다. 이 표는 사용자 작업 선호를 실행하는 정책이며 보편적인 모델 성능 순위나 새 품질 벤치마크가 아니다.
- `task_type`이 없으면 general이다(peer 요청은 reasoning으로 분류해 사유를 요구). 예전 Luna 모델 선호를 검증 작업으로 간주해 Codex에 고정하지 않는다. 지정 모델이 작업 정책에 없으면 조용히 무시하지 않고 400으로 명시 엔진 선택을 안내한다. 명시 엔진은 사용자/필요 도구에 따른 실제 잠금으로 보존한다.
- fresh 모델그룹 창의 정확한 remaining_percent를 headroom에 반영한다. Claude/GPT·Flash·Argon pool, zero credits, stale 소진, 초기화 후/미측정 unknown을 구분한다. unknown보다 알려진 적합 후보, 그 안에서 모델 단계와 계정 부하를 비교한다. 회사 범위·켜짐·지원 모델·cooldown을 지킨다.
- `account_usage.worker_routing`, `model_capacity`, 역할 지침의 단계 목록이 같은 원본에서 생성된다. 최신 정책이 낡은 역할 지침 뒤에 붙고 MCP 설명도 일치한다. `worker.routing` 이벤트와 WorkerOut.routing에 선택·실제 모델·quota group/창·측정시각·잔여·점수·제외·트리거를 기록한다.
- 모델별 provider 대화를 `account_id:model`로 분리한다. Sonnet→Flash→Sonnet 재개 시 같은 계정이라도 Flash의 진행을 `_handoff_context`로 전달한다. 모델을 바꿔 같은 실행파일 부재를 반복하지 않으며 일반 실패/권한 거부를 quota처럼 다른 엔진에서 재실행하지 않는다.
- `message_worker(reroute=True, task_type=...)`는 idle 작업자를 새 auto 정책으로 전환하고 기존 대화를 보존한다. running/queued이면 409로 거부하고 새 지시·context reset도 적용하지 않는다. legacy Run이 요청 모델명을 그대로 보존한 native fallback 이력도 counterpart를 복원해 맞는 provider 대화를 재개한다. 부작용 확인/완료·취소 이후 전환하며 완료 작업을 자동 재실행하지 않는다.
- 0035는 sessions.worker_task_type nullable 컬럼만 추가한다. 기존 세션/진행 중 Run을 자동 변경하지 않는다. generation protocol/소유권/슬롯/drain 조건을 유지하고 온라인 호환 계약은 0035로 갱신한다. 별도 회의·날씨 라우팅은 변경하지 않는다.

## 검증 및 증거 범위

증거 디렉터리: `/Users/winterholic/.ttakkari/data/outbox/3f5bf6da-ce10-4829-be72-e36a013c259a/`.

- RED: 기본 auto의 native 선택, AGY headroom 미반영, reasoning 사유/잘못된 task_type, 모델 preference 무시, native peer effort, 같은 계정 모델 간 진행 손실, 실행파일 중복 시도, legacy counterpart resume 누락을 각각 재현하고 수정했다. `*-red.txt` 원시 결과를 보존했다.
- task·모델별 소진·런타임 quota·all-capped wait·회사 범위·unknown/stale/reset·지원 모델·명시 잠금·idle/active reroute·일반 실패·모델별 재개/진행 인계와 실제 generation ownership 경로를 synthetic CLI/격리 DB에서 검사했다.
- 초기 전체 검사: 1095 passed, 3 skipped, 2 xfailed, 5 failed. 4개는 분리 checkout의 `.venv/bin/alembic` 경로 누락으로 테스트 환경을 연결했다. 1개는 native auto 재개 시 어느 계정이 먼저 풀리는지에 따라 Claude/Codex 둘 다 가능한데 Claude만 가정한 검사였다. 실제 선택 엔진과 맞는 argv를 읽고 동일 Run·진행/체크포인트·단일 알림·resume 이벤트를 검사하도록 보완했다. 보완 뒤 관련 76개 통과. 원본 실패 로그는 `full-regression.txt`에 남긴다.
- migration-check.txt: 별도 DB에 0034를 실제 설치하고 구 배포 ORM으로 running legacy Run/대화 ID를 쓴 뒤 online 0035 upgrade를 실행했다. status/engine/대화 ID 보존 및 nullable task_type을 SQL로 확인했고, 구 ORM에서도 동일 데이터를 조회했다. 운영 DB에 migration을 직접 실행하지 않았다.
- routing-production-snapshot.json: 저장된 운영 quota/cooldown/실행 lease를 읽기 전용 트랜잭션에서 재생해 개인 general/design=AGY Sonnet, routine=Flash, verification=Luna, reasoning=Argon 선택을 확인했다. 회사 범위는 개인 계정을 빌리지 않는다. 이 파일은 모델 호출·운영 새 라우터 실행·MCP·CLI 재개의 증거가 아니다.
- GPU 모니터링 main 변경을 통합한 최종 전체 회귀 결과와 커밋/배포 상태는 아래에 기록한다.

최종 검증: GPU main 통합 코드 `4da9b32e1448b4d51bc21ceb915ba2509f6dca8e`에서 단일 전체 실행 `python -m pytest -q --tb=short` → **1118 passed, 3 skipped, 2 xfailed, 6 subtests passed (222.04s)**. `final-full-regression.xml/txt`가 원시 증거다. Ruff 전체 및 diff check도 통과했다. 최종 문서 기록 이후에는 실행 코드를 바꾸지 않았다.

배포 상태: 이 기록 시점에는 **미적용**이다. canonical back에는 다른 세션의 Qwen 구현·0035_qwen_token_usage 변경이 미커밋으로 남아 있어 변경을 섞거나 stash하지 않았다. 이 브랜치는 GPU main을 포함한 별도 clean commit으로 보존한다. 예약 시도의 원시 응답은 outbox/deployment-response.json에 남긴다. 이후 Qwen 변경과 합칠 때 양쪽의 0035 migration이 단일 그래프가 되도록 정리하고 schema 계약/마이그레이션/관련 회귀를 다시 확인해야 한다. 운영 새 라우터의 모델 응답·MCP·CLI 재개는 아직 미검증이다.
