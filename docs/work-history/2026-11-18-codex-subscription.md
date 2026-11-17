# Codex 구독 관리 구현 후보 — 2026-11-18

## 현재 상태

구현과 기획 자체 회귀 검증을 마쳤고 독립 검토·통합·배포가 남아 있다. 실제 주간 초기화와 다음 주기 변경 효과는 미검증이다. 원래 요구/미완료는 기획 세션 `manage_work` 계약 R1~R6에 보존한다.

- backend 작업 공간: `back-codex-weekly-20261118`, branch `feat/codex-weekly-activation-20261118`.
- frontend 작업 공간: `../front-codex-weekly-20261118`, 같은 branch 이름(별도 저장소).
- 기본 `back/`, `front/` 체크아웃의 타 세션 미커밋 변경은 손대지 않았다.
- backend는 초기 `65ca56f9`에서 작업하고 upstream 날씨 수정 2건(`be9e3de7`, `7caf0528`) 위로 충돌 없이 rebase했다. 날씨 회귀도 아래 검증에 포함했다.

## 구현

- 설계·제약: `docs/adr/0074-codex-weekly-activation.md`, 기존 ADR 0006·API 계약 갱신.
- 공식 조사: `docs/research/codex-subscription-20261118.md` (조사 작업자 Claude Sonnet, 기획이 공식 문서/CLI help/생성 스키마 핵심 대조).
- `runs/codex_subscription.py`: 읽기 RPC allowlist, API 자격 없는 구독 자식 환경, 임시 exec, 인증/한도/불명 결과 구분.
- `runs/subscription_policy.py`: 시간 주입, 12시간 조회, resetsAt+10초 예약, 제한된 확인 재시도.
- `runs/subscription_monitor.py`: 계정 행 잠금, 영속 상태/예약 교체, dispatch 전 커밋, 사용자 선사용/불명 요청 생략, 전후 기록과 알림.
- Collector의 Codex 고빈도 조회 제외. 기존 큐 복구를 DB로 점검하고 실패한 작업의 무한 즉시 조회를 막는다.
- 계정 관리 API·경로 분류표·알림 수신 유형 추가. 정상 12시간 조회 간격은 가짜 조회 실패로 표시하지 않는다.
- 프론트의 일반 Codex 새로고침도 새 예약 경로로 연결해 resetsAt 변경을 반영한다. 닫힌 대화상자의 상태 조회는 멈춘다.

## 실행 증거

대상은 위 backend rebase 후 작업 트리와 frontend 작업 트리다. 코드 변경 뒤 아래 대표 회귀를 실행했고 문서 기록만 추가했다.

```
PYTHONPATH=src ../back/.venv/bin/pytest -q tests/test_subscription_monitor.py tests/test_usage_collector.py tests/test_scheduled_actions.py tests/test_accounts.py tests/test_notifications.py tests/test_notification_preferences.py tests/test_push.py tests/test_route_inventory.py unit_tests/test_subscription_policy.py unit_tests/test_codex_subscription_client.py unit_tests/test_usage_failure_visibility.py unit_tests/test_muse_usage.py tests/test_weather.py tests/test_weather_api.py tests/test_weather_engine.py
243 passed, 1 warning in 13.81s
```

경고는 기존 Antigravity 테스트가 사용하는 Starlette HTTP 422 이름의 deprecation 1건이다. 변경 파일 Ruff 검사 통과.

```
vitest run src/features/accounts/SubscriptionRow.test.tsx src/features/accounts/UsageDialog.test.tsx src/features/accounts/UsageDialog.legacy.test.tsx src/api/accountUsageFallback.test.ts
48 passed (4 files)
```

해당 프론트 파일 ESLint, `npm run build`, SubscriptionRow design-lint 통과. 브라우저 합성 데이터 검증은 프론트 작업 기록과 저장소 내 PNG에 있다. CLI 실제 모델 요청은 실행하지 않았다.

새 테스트가 구현 전 실패하는 것을 확인한 뒤 구현했다. 초기화 예약·변경·재시작·동시 실행·dispatch 직후 장애·수동 재시도·원래 사용자 실행의 주간 경계 통과·구독 자격 거절·캐시 전후 반영·예상 밖 스키마를 검증했다.

## 미검증·제약

- 실제 등록 계정 `read_usage` 실행은 기획의 중첩 Codex 샌드박스에서 App Server initialize 전에 `sandbox_denied (exit 1)`로 끝났다. 인증 파일을 직접 읽거나 복사하지 않았고 같은 대상 우회를 하지 않았다. 호스트/운영 실패 주장이 아니다.
- 배포 백엔드의 공식 RPC 응답, 최소 CLI 모델 접근성과 실제 토큰 소모, 실제 초기화 시각 일치, 첫 사용/조회가 주기에 미치는 영향은 남아 있다.
- 미래 새 창의 0%는 외부 미세 사용을 구분할 수 없어 생략한다. 조회와 요청 사이 외부 사용을 원자적으로 배제할 방법도 확인되지 않았다.
- 예약의 10초는 목표 시각이며 기존 스캐너·OS·네트워크 지연을 포함한 실시간 보장이 아니다.
- 최초 UI 미리보기(127.0.0.1:4638, exec session 97587)는 수정 전 CSS를 제공했다. 종료 명령 `kill -TERM 34029`는 중첩 Codex 샌드박스에서 `operation not permitted`로 거절됐다. 종료는 미검증이다. 다른 명령으로 종료를 재시도하지 않았다. 최신 UI는 자동 종료되는 4639/4640 미리보기에서 확인했고 브라우저는 닫았다.

## 다음 동작

독립 검토는 코드·요구·공식 인터페이스의 정합성과 테스트 사각을 확인한다. 지적은 재현/교정 후 좁게 재검증한다. 검토를 마친 후보를 원격 main에 통합하고 backend full SHA 배포를 예약한다. 배포 접수와 성공을 구분한다. 운영 리비전을 확인한 뒤 지원 API로 계정 상태를 한 번 읽고 실제 resetsAt에 맞춰 관측 후속을 예약한다. 실제 주간 경계를 기다리려고 활성 Run을 유지하거나 미리 폴링하지 않는다.
