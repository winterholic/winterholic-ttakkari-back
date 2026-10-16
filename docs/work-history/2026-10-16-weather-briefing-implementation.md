# 날씨 브리핑 구현 — 2026-10-16

상태: 코드 구현과 검증을 마치고 배포 준비 중. 실 Open-Meteo 수집과 시각 화면 검증은 됐다. 이 실행 환경의 Codex CLI는 OS IPC 제한으로 거부되어 실제 Luna Run·완성 브리핑·푸시 전달·운영 배포·첫 자동 실행은 확인되지 않았다.

## 조사와 결정

- PMS WBS14 원문 및 보관 세션의 사용자 원요청 재확인. 실제 일정은 매일 06:30 Asia/Seoul이며 초기 초안의 06:00은 적용하지 않는다.
- `/Users/winterholic/development/projects/winterholic-ttakkari/back/src/ttakkari/main.py:_reminder_loop`를 사용한다. 날씨 Run은 기존 `sessions.queue_run → runs.manager`를 거쳐 Codex 계정 프로필 라우팅을 재사용하고, 기존 `push.service.notify`로 전달한다.
- Open-Meteo 공식 [Terms](https://open-meteo.com/en/terms)와 [Forecast API](https://open-meteo.com/en/docs)를 확인했다. 개인·비상업 Free API의 한도는 분당 600·시간당 5,000·하루 10,000 미만이며 데이터는 CC BY 4.0 출처 표시가 필요하다. 수원/서울 각각 하루 1회 조회한다.
- 코드 워크트리: `/private/tmp/weather-back`, `/private/tmp/weather-front`. 프론트 공유 worktree의 미커밋 AccountsSection 변경은 보존했고, 진행 중이던 main 변경 두 커밋 위로 기능 브랜치를 rebase했다. 백엔드도 최신 main의 회의록 수정 위로 rebase했다.

## 구현 중인 범위

- Open-Meteo provider 정규화에서 원본 JSON, KST timezone, 단위, 수집/예보 기준시각과 결측을 보존한다. 부분 지역 실패를 유지하고 성공한 지역은 계속 제공한다.
- `weather_briefings` 날짜 유일키, 상태/lease, 수동·자동 공용 날짜 키, 재시작 가능한 Run 상태 저장. `0032_weather_briefings`는 새 테이블과 nullable 컬럼/인덱스를 추가하며 generation contract가 0031→0032 online migration을 허용한다.
- 분석 Run의 엔진·모델·effort를 `codex / gpt-6-luna / low`로 고정한다. 실패/형식 오류/안전 검사 실패는 데이터 기반 결과와 분석 상태를 분리한다.
- 기존 알림함 category에 `weather` 추가, notification dedupe key로 재시작 시 중복 기록을 막는다. 푸시 구독 없음/부분 실패는 브리핑과 분리해 화면에서 알리고 수동 재시도할 수 있도록 한다.
- `/weather` 반응형 브리핑과 기존 더보기 메뉴 링크 구현 중: 오늘 요약/우산·복장, 시간별 변화, 지역 비교, 7일 전망/주말, 결측·출처·갱신·이력·수동 생성·상태.

## 지금까지 검증

- 백엔드 전체 pytest 재실행: 870 passed, 2 xfailed, 6 subtests passed. 그 뒤 대체 브리핑의 주간 강수확률에서 `null`을 0으로 취급하던 결측 처리 오류를 발견했다. 회귀 테스트 RED 확인 뒤 결측 일수를 명시하도록 수정했다. 날씨/API/notification 테스트 26 passed, Ruff와 `git diff --check`도 수정 후 통과했다.
- 새 PostgreSQL DB에서 `alembic upgrade head` 후 `0032_weather_briefings (head)` 확인.
- 프론트 전체: 74 files passed, 1 skipped; 473 tests passed, 1 skipped. Build와 ESLint 통과. 새 경로 추가에 따라 AppShell/Markdown route/알림 category 기존 고정 기대값을 갱신했다.
- 실 Open-Meteo API에서 수원·서울 7일 forecast를 모두 수신하고 정규화했다. 두 지역 모두 수집 오류/결측 없음.
- Codex CLI의 실제 `gpt-6-luna` low 호출은 시도했으나 `failed to initialize in-process app-server client: Operation not permitted (os error 1)`로 실패했다. 권한 우회는 하지 않았다. 따라서 실제 모델 출력 및 정상 app-server 경로의 Run 생성/완료는 입증하지 못했다.
- ttakkari_browser로 격리한 로컬 UI를 확인했다. API fixture는 앞서 실제 수집한 Open-Meteo JSON과 데이터 기반 fallback으로 만들었고, 모바일 390px/데스크톱 1440px, light/dark를 캡처했다. `/weather` 화면의 요약·시간별 그래프·지역 비교·7일 전망·출처가 렌더됨을 확인했다. API 서버 자체의 브라우저 E2E가 아닌 시각 검증이며, 로컬 PWA service worker는 dev server에서 MIME 오류를 냈다.

## 남은 작업

1. 격리된 두 worktree의 변경만 선별해 clean commit/push한다. backend는 full SHA로 generation deploy를 예약하고 예약 상태를 그대로 기록한다. 프론트는 저장소의 기존 방식대로 push 후 Vercel production 상태와 공개 경로를 확인한다.
2. 운영 Luna Run을 이 실행 환경에서 만들 수 없었던 한계를 PMS와 사용자 보고에 남긴다. 운영 모델 검증 전에는 실제 날씨 push 발송을 성공했다고 쓰지 않는다.
3. 배포 후 health/migration/UI/API를 점검한다. 다음 06:30 첫 자동 실행 전이면 WX06을 done으로 표시하지 않는다.
