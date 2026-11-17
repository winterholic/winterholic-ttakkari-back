# Codex 구독 예약 구현 독립 검증 — 2026-11-18

## 판정

**배포 전 차단 결함 1건(P1)을 수정해야 한다.** 정상 경로 테스트는 통과했지만, 자동 활성화 선사용 방어가 Codex 계정의 변경 가능한 이름에 묶여 있다. 이름을 바꾼 뒤 경계 이후에 완료된 따까리 Run을 선사용으로 인식하지 못하는 반례를 격리 PostgreSQL에서 재현했다. 그 반례의 quota 응답이 `reset_at`을 그대로 반환하고 사용률 0%·`ordinaryUsageAllowed=true`인 경우 정책은 자동 요청을 보낼 수 있다. 해당 API의 이 조합이 실제로 발생하는지는 관측하지 못했으나, 이 후보가 보호하려는 stale/지연 관측에서 로컬 선사용 fence가 무너지는 것은 코드·재현으로 확인했다.

- 최소 수정 방향: 선사용 조회 키를 이름 대신 `EngineAccount.id`에 영속 결합한다. 과거 Run 처리까지 정해야 하므로 이름 변경 이력/backfill 또는 stable account ID 저장 방식을 선택하고, 이름 변경 후 완료 Run 반례를 GREEN 회귀로 고정한다.
- 이 검증 Run에서는 제품 코드를 변경하지 않았다. 결함 수정, 통합 및 배포는 남아 있다.

## 검증 대상과 범위

- Backend: `9099ca129ad119dd268665075d28ed61baa027e6` (`feat/codex-weekly-activation-20261118`)
- Frontend: `f587dd4854eb51c134487529bd9a594dbce9d238` (`feat/codex-weekly-activation-20261118`)
- 두 작업 트리의 `HEAD`가 위 SHA와 같고 시작 시 worktree가 깨끗함을 `git rev-parse HEAD`, `git status --short --branch`로 확인했다.
- 기준: `docs/adr/0074-codex-weekly-activation.md`, backend/frontend `docs/work-history/2026-11-18-codex-subscription.md`, `docs/research/codex-subscription-20261118.md`, backend 구현·테스트 및 frontend 컴포넌트·테스트.
- 실제 Codex 계정 인증/RPC, 실제 모델 요청, 운영 API와 운영 DB는 사용하지 않았다. 실제 주간 경계는 아직 오지 않았으며 주기 효과 관측은 미검증이다.

## 요구사항 대조

| 요구·불변식 | 구현·검증 | 판정 |
|---|---|---|
| 평소 조회 간격 12시간, 알려진 `resetsAt`+10초에 정확 예약 | `subscription_policy.py:9-12, 22-27, 89-94`; 격리 단위 테스트의 bootstrap/단일 예약/시각 변경 사례 | 통과. 10초는 목표 시각이며 scheduler tick·OS·네트워크 지연은 보장하지 않음 |
| 확인 실패 시 30·60·120·240·450초 재확인, 15분/횟수 상한 | `subscription_policy.py:11-12, 69-80`; `test_reconfirmation_backoff_is_bounded_by_count_and_elapsed_time` | 통과. 최초 확인 포함 6회 뒤 종료, 다음 주기 예약으로 복귀 |
| 취소·예약 변경·재시작 복구·중복 방지 | `subscription_monitor.py:56-89, 134-209`; 기존 테스트는 stale leased action supersede, 동시 check, dispatch marker 전 commit, 실패 예약 복구 확인 | 통과. 계정 잠금 및 canonical schedule ID로 동시 실행 fence 적용 |
| 취소/변경된 예약 payload는 실행하지 않음 | `_reserve`는 이전 pending/leased row를 취소하고, `check`는 전달된 ID와 state의 `schedule_id`가 다르면 `superseded` 반환 | 통과 |
| 미사용 사용자만 실행, 이미 사용/불명 결과/주기 중복은 생략 | 정책은 신선한 API 관측·주간 0%·사용 허용 확인을 요구하고, 미래 reset + 0%는 불명으로 생략. dispatch 주기 표식은 외부 호출 전에 commit | P1 수정: 변경 전 이름 별칭을 구독 AppSetting에 보존하고 현재 이름과 함께 조회. 격리 PostgreSQL 회귀 통과 |
| 구독 전용 실행·API 과금 방지 경계 | `codex_subscription.py:39-61, 132-157, 184-213, 224-269`: RPC allowlist는 initialize/account reads뿐, child env allowlist, ephemeral/read-only 임시 cwd, ChatGPT login 강제 설정, pre-spawn OSError만 제한 재시도, spawn 뒤 timeout은 unknown | 코드/모의 테스트 정합. 실제 CLI 인증 선택·모델 접근·최소 토큰 소비는 미검증 |
| auth·전송·한도 오류와 불명 결과 구분, 전후 관측 | RPC 오류는 범주만 저장하며 원문 stderr는 보존하지 않음; dispatch 결과가 불명일 때 재전송하지 않음; before/after usage snapshot과 토큰 결과 저장 | 코드/모의 테스트 정합. 외부 서비스의 실제 오류 응답과 사용량 반영은 미검증 |
| 대시보드 상태·시간·자동 설정·수동 작업, 닫힌 화면의 provider 조회 중단 | `SubscriptionRow.tsx:38-97`; `UsageDialog.tsx:116-169, 359-361`; 컴포넌트 테스트의 toggle/manual refresh/activate/closed-query 사례; 합성 UI 캡처는 frontend docs/work-history/evidence 아래 desktop/mobile PNG | 테스트·기존 캡처 확인. 화면 메타 조회는 열려 있을 때만 polling하고, provider 확인은 사용자가 누르거나 backend 예약이 실행될 때만 함 |
| 소유자 인증 | `api/accounts.py:638-672`의 각 route가 `User` dependency 사용; `security/auth.py:58-74`는 `aud=api`, `sub=owner` 확인. subscription 테스트는 미인증 401, 비 Codex 400, 없는 계정 404 확인 | 통과. 현재 앱의 단일 owner 모델 범위이며 계정별 다중 테넌트 권한은 대상 아님 |

## 배포 차단 결함 P1

**수정 완료 — 이름 변경 이력을 구독 상태에서 보존한다.** 아래는 발견 당시의 재현과 조치 내역이다.

**완료된 사용자 사용이 계정 이름 변경 뒤 선사용 조회에서 사라질 수 있다.**

- Backend [subscription_monitor.py](../../src/ttakkari/runs/subscription_monitor.py#L106): `Run.profile == account.name`으로 이름을 비교한다. `Run.profile`은 `models.py:386-387`에서 문자열이며 Run에는 account ID 필드가 없다. 반면 실행 중인 사용은 `RuntimeLease.account_id`로 별도 확인한다(`subscription_monitor.py:110-114`).
- 최소 재현 순서: `codex-old` 계정으로 주간 경계 뒤 완료 Run(`engine=codex`, `profile=codex-old`, `started_at >= reset_at`)을 기록 → 계정 이름을 `codex-new`로 변경 → `_user_used(account, reset_at)` 호출. 완료 Run은 active lease가 아니며 `Run.profile == account.name` 조건도 불일치해 결과가 `False`다.
- 격리 재현은 임시 `tests/test_tmp_subscription_rename_repro.py`로 실행하고 파일을 제거했다. assert는 현재 코드가 해당 사용을 못 찾음(False)을 확인했다. 테스트 출력의 `1 passed`는 결함 없는 의미가 아니라 이 false-negative 반례 재현 성공이다.
- 피해 조건: API read가 이미 경계 후 갱신된 시각을 보고하지만 아직 reset 시각/0%를 stale하게 유지하고 사용 허용을 true로 반환하는 경우 정책의 `new_reset == old_reset` 분기가 활성화할 수 있다. 그 조합은 공식 보장도 실제 계정에서 검증되지 않아 발생 빈도는 미확인이다. 외부 사용은 현재 저장 데이터만으로 원자 검출할 수 없다는 ADR의 기존 제약과 별개로, 이름 변경으로 따까리 자체 Run 사용 흔적까지 잃는 것은 피할 수 있는 내부 false-negative다.

### 격리 재현 명령과 출력

```text
PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_tmp_subscription_rename_repro.py
.                                                                        [100%]
db reset: 1 selective (avg 3.0/138 tables), 0 forced full, 0 fell back to full
1 passed in 0.68s
```

관측 환경은 feature backend worktree의 pytest/격리 PostgreSQL fixture다. 실제 계정/운영 DB는 관여하지 않았다. 임시 테스트 파일 제거 뒤 backend `git status --short`는 비어 있음을 확인했다.

## 기존 회귀 검증 실행

### 이름 변경 수정 후 backend 재검증

`patch_account`는 Codex 이름 변경 시 subscription monitor의 기존 계정 행 잠금을 사용하고, 이전 이름을 계정 구독 AppSetting의 `user_used_profile_aliases` 배열에 누적한다. `_user_used`는 현재 이름과 누적 별칭 모두를 비교한다. Run 행과 DB 스키마는 변경하지 않았다.

회귀 케이스는 `codex-old`에서 경계 이후 완료 Run을 저장하고 계정을 `codex-new`, 이어 `codex-latest`로 변경한다. `_user_used`가 True인지, 별칭이 누적되는지, Run.profile 원본이 유지되는지 격리 PostgreSQL에서 확인한다. 구현 전 테스트는 이 반례에서 `assert False is True`로 실패했다.

```text
PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_subscription_monitor.py tests/test_usage_collector.py tests/test_scheduled_actions.py tests/test_accounts.py tests/test_notifications.py tests/test_notification_preferences.py tests/test_push.py tests/test_route_inventory.py unit_tests/test_subscription_policy.py unit_tests/test_codex_subscription_client.py unit_tests/test_usage_failure_visibility.py unit_tests/test_muse_usage.py tests/test_weather.py tests/test_weather_api.py tests/test_weather_engine.py
244 passed, 1 warning in 12.41s

../back/.venv/bin/ruff check src/ttakkari/runs/subscription_monitor.py src/ttakkari/api/accounts.py tests/test_subscription_monitor.py
All checks passed!
```

경고는 기존 Starlette `HTTP_422_UNPROCESSABLE_ENTITY` deprecation이다. 실행은 feature backend worktree의 격리 PostgreSQL fixture에서 이뤄졌으며 실제 Codex 계정/RPC·모델 호출·인증 파일은 사용하지 않았다.

### Backend

```text
PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_subscription_monitor.py tests/test_usage_collector.py tests/test_scheduled_actions.py tests/test_accounts.py tests/test_notifications.py tests/test_notification_preferences.py tests/test_push.py tests/test_route_inventory.py unit_tests/test_subscription_policy.py unit_tests/test_codex_subscription_client.py unit_tests/test_usage_failure_visibility.py unit_tests/test_muse_usage.py
205 passed, 1 warning in 11.98s
```

경고는 기존 `HTTP_422_UNPROCESSABLE_ENTITY` deprecation이다. 이 지정 명령은 work-history의 243건과 달리 날씨 관련 테스트 3개 파일을 포함하지 않는다.

### Frontend

```text
./node_modules/.bin/vitest run src/features/accounts/SubscriptionRow.test.tsx src/features/accounts/UsageDialog.test.tsx src/features/accounts/UsageDialog.legacy.test.tsx src/api/accountUsageFallback.test.ts
Test Files  4 passed (4)
Tests       48 passed (48)
```

관측 환경은 각 feature worktree의 로컬 pytest/Vitest다. UI 캡처는 허용 저장소의 기존 합성 데이터 PNG만 확인했다. 이 독립 검증에서는 브라우저를 새로 실행하지 않았고, 기존 4638 프로세스에는 접근·종료 시도를 하지 않았다.

## 공식 인터페이스 제약과 미검증 범위

- 공식 조사 `docs/research/codex-subscription-20261118.md`는 설치 CLI `codex-cli 0.166.0` 생성 스키마와 공식 App Server 문서에서 `account/read`, `account/rateLimits/read`, `windowDurationMins`, `usedPercent`, `resetsAt` 구조를 확인했다. 코드 파싱은 300/10080분만 5시간/주간으로 인정하고, unknown/missing duration은 버린다. 이번 독립 Run은 `codex --version`이나 schema generation을 재실행하지 않았다.
- 공식 문서가 보장하지 않는 것: 주간 주기가 첫 모델 사용 때 시작되는지, `account/rateLimits/read`가 주기를 시작/비시작하는지, `resetsAt`의 실제 초기화 시각 일치, 모델 요청이 다음 주기를 앞당기는지. 이는 구현 결함으로 단정하지 않으며 실계정 확인 전 운영 효과 주장은 금지한다.
- App Server 프로토콜은 조사 당시 experimental로 기록됐다. 인증/CLI 경로는 문서상 로컬 비공개 신뢰 러너와 직렬 사용 제한을 따르며, `auth.json`을 직접 읽거나 복제하지 않는다. CLI가 내부 상태와 인증을 쓸 수 있어 파일시스템 무변경 보장은 없다.
- 이 feature 코드에 대한 실계정 RPC는 하지 않았다. 앞선 기획 실행의 중첩 Codex 샌드박스 initialize 전 `sandbox_denied (exit 1)`는 그 환경에 한정된 결과이며 호스트/운영 장애의 증거가 아니다. 본 검증은 이를 재시도하거나 우회하지 않았다.
- 모델 접근성, 실제 최소 토큰 소모, 실제 사용자의 주기 내 미세 사용 감지, 외부 요청과 조회 사이 원자적 선사용 차단, 알림 전달 성공은 미검증이다. 알림 실패는 dispatch marker를 되돌리거나 요청을 재전송하지 않는 구조다.
- 브라우저 재검증, lint/build 재실행, 주간 경계 대기/관측은 하지 않았다. 사유: 범위는 독립 코드 검증이며 기존 브라우저 인스턴스 접근·셸 브라우저 실행은 금지됨; 프론트 빌드/브라우저 근거는 선행 work-history 및 PNG에 기록됐으나 이 Run의 재측정 증거는 아니다. 실제 주간 경계는 외부 시간·실계정 필요.

## 최종 결정

P1 이름 변경 반례는 별칭 저장과 격리 PostgreSQL 회귀로 수정했다. 이번 범위에서 main 병합·배포는 하지 않는다. 그 외 지정 회귀 명령은 통과했고 요구된 예약 상한, 취소·복구·dispatch 중복 fence, UI/API auth 경계는 코드·테스트상 정합하다. 정책의 핵심 외부 가설과 실계정 동작은 별도로 관측해야 하며, 이를 이미 입증된 사실로 취급하지 않는다.
