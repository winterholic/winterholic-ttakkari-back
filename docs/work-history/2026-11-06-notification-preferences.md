# 유형별 알림 수신 설정 — 2026-11-06

요청: 중요한 소식만 받고 하위 에이전트의 개별 작업 알림은 줄이며 날씨·일정 등을 따로 선택한다.
현재 상태: 구현·로컬 검증. 운영 배포 적용 확인 전이다.

- 기준: back origin/main 521f469a, front origin/main e2d89e6. 기존 main의 다른 세션 미커밋 변경은 건드리지 않고 `*-notification-preferences-20261106` 작업트리를 사용했다.
- 활성 알림 12종을 카탈로그로 제공하고 엄격한 소유자 GET/PATCH 설정 API를 추가했다. 기존 app_settings 개별 행 upsert를 사용하며 새 마이그레이션은 없다.
- 날씨·일정·할 일·메인 직접 알림·메인 실패·회의록·계정 로그인은 기본 켜짐, 메인 대기·하위 직접/실패/대기·배포는 기본 꺼짐이다. 명시적 선택은 저장하고 모든 기기에 적용한다.
- 서버가 source session.kind와 parent.kind로 메인/하위를 구분한다. 접수 아래 기획은 메인이고 worker 및 중첩 기획은 하위다. notify 공통 진입점에서 꺼진 유형의 새 push와 inbox 적재를 생략한다. 기존 기록은 보존한다.
- 날씨 opt-out을 배송 실패로 오인해 반복 재시도하지 않도록 별도 결과를 처리한다. 브리핑 생성은 수신 여부와 독립이다.
- 프론트: 기존 알림 설정 다이얼로그에 그룹별 토글·설명·즉시 저장·실패 복구, 기기 구독과 유형 설정 분리, 목록의 세부 kind 라벨을 추가했다. 기기 푸시가 지원되지 않아도 유형 설정은 조작할 수 있다.

## 실행 증거

- 신규 테스트 RED: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_notification_preferences.py` → 4 failed. 기존에는 API 404와 worker 직접 알림 발송으로 실패했다.
- GREEN·회귀: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_notification_preferences.py tests/test_notifications.py tests/test_push.py tests/test_weather.py tests/test_weather_api.py tests/test_weather_engine.py --timeout=90` → 70 passed, 격리 PostgreSQL.
- 전체 계약: `PYTHONPATH=src .venv/bin/python -m pytest -q -m contract --timeout=90` → 85 passed, 6686 deselected.
- 프론트 신규·관련: `TTAKKARI_NO_ENV_FILE=1 node_modules/.bin/vitest run src/features/push src/pages/NotificationsPage.test.tsx src/api/notifications.test.ts` → 최종 32 passed. GET/PATCH 인증·단일 유형 전송·저장 성공·저장 실패 시 기존 선택·조회 재시도 검증.
- 프론트 전체(최종 스타일·토스트 보강 전): vitest → 1594 passed, 10 skipped. 이후 변경 범위 재검증 32 passed. tsc/build 및 변경 파일 ESLint 통과. 기존 canvas 미구현 안내 및 큰 번들 경고는 별도다.
- 지정 ttakkari_browser에서 실제 컴포넌트+샘플 fetch로 320×740, 390×844, 1280×900, light/dark를 확인했다. 토글→새로고침 유지, 하위 토글 독립 선택, 내부 스크롤로 마지막 항목 조작을 확인했다. 실제 사용자 API·휴대폰 OS push는 미검증이다.
- ui-refine collectPage를 샘플 페이지에서 실행했다. 320px에서 dialog x=12,width=296, 유형 행 12개 폭254px/높이74px 이상, css-vars 스케일. 내부 스크롤 때문에 최초 화면 측정 커버리지는 40.7%이며 전체 화면 감사로 주장하지 않는다. 하단은 실제 스크롤·클릭으로 확인했다. 새 제목 행간 지적은 기존 heading 토큰으로 수정했다. 기존 vendor 버튼 비율·focus 탐지 신호는 이번 범위 밖이다.

## 다음 단계

변경 파일만 커밋하고 최신 원격 main에 통합한 뒤 사전 배포 게이트·push·request_backend_deploy 예약을 수행한다. 백엔드의 설정 API 적용을 확인한 다음 프론트 main을 배포한다. 예약 수락을 배포 성공으로 표현하지 않는다.
