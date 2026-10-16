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

## 엔진 폴백 (2026-10-16 후속)

사용자 지시: 날씨는 Luna 고정일 필요 없다. Codex 한도가 차면 Claude나 Antigravity로 넘어간다. 위 "Luna low 고정" 결정은 이 절로 대체한다.

- **경로 선택** (weather/service.py `choose_route`): `profiles.availability("auto")`에 한도 아닌 Claude·Codex 계정이 있으면 engine=auto 세션(model gpt-6-luna, effort low)으로 Run을 넣는다. 계정 선택과 Claude 전환은 매니저가 하고, 모델은 `model_counterparts`에 따라 Claude에서는 sonnet이 된다. 둘 다 한도이면 Antigravity gemini-3.8-flash-medium / medium, 그것도 안 되면 즉시 데이터 기반(degraded, analysis_status=failed) 발행이다.
- **Antigravity effort**: 입력 JSON만 요약하는 단발 작업이라 high는 과하다. 다만 위험 조언 금지와 길이 제한 규칙이 많아 low보다 medium을 택했다. 마지막 폴백이라 속도보다 규칙 준수를 우선한 판단이며 실측 비교는 아직 없다.
- **정책 범위**: Antigravity는 앱 정책상 auto 후보에서 제외된다. 이 폴백은 날씨 모듈만의 명시 경로이고 `profiles.availability`의 auto 정의, 회의록, 데스크, 오케스트레이터는 건드리지 않았다.
- **한도 대기 처리**: auto Run이 시작 뒤 모든 계정 한도로 run.waiting(queued)에 들어가면 매니저는 초기화까지 몇 시간 기다린다. 날씨는 `advance`의 generating 단계에서 이를 감지해 그 Run을 취소하고 Antigravity 새 세션으로 한 번 넘긴다. Antigravity Run이 대기에 들어가면 데이터 기반으로 발행한다. 일반 실패, 형식 오류, 안전 검사 실패는 전환하지 않고 기존처럼 degraded다.
- **추적**: weather_briefings에 analysis_engine, analysis_model, analysis_route(JSON 이력: dispatch/completed와 사유)를 추가했다. 마이그레이션은 0033_weather_analysis_engine(nullable 컬럼 3개)이고 deploy/generation-contract.json은 0033, online from 0032로 갱신했다. 완료 시 analysis_engine/analysis_model은 Run이 실제로 쓴 엔진과 `effective_model`(매니저 `_launch_model` 규칙)이다. GET /api/weather/* 응답에도 포함된다.
- **JSON 추출**: 첫 "{"~마지막 "}" 방식은 머리말·꼬리말에 중괄호가 있으면 깨진다. 모든 "{" 위치에서 raw_decode로 객체를 읽어 스키마에 맞는 첫 객체를 쓰도록 바꿨다. summary가 4문장을 넘으면 앞 4개만 쓴다. 코드 블록, 머리말, 꼬리말, 중괄호 섞인 설명 문장 케이스를 테스트했다.
- **기존 결함 수정**: 실패한 Run에 error가 없을 때 `run.status.value`가 str 컬럼이라 AttributeError였다. `str(run.status)`로 바꿨다.
- **검증**: tests/test_weather_engine.py 신규(Codex 한도→Claude 유지, 둘 다 한도→Antigravity, 전부 불가→degraded, 대기 전환, 완료 기록, JSON 추출). 전체 pytest -q는 886 passed, 2 skipped, 2 xfailed. `ruff check src tests alembic` 통과(저장소 전체의 기존 ruff 오류 21건은 docs/experiments 스크립트라 손대지 않음). 공유 테스트 DB가 다른 작업과 충돌해 TTAKKARI_TEST_DB로 전용 DB를 썼다.
- **미확인**: 실제 Antigravity·Claude Run으로 날씨 JSON을 받아 본 적은 없다. 이 환경의 CLI 제약은 위 기록과 같다.

## 운영 Luna 한도 대기 사고와 수정 (2026-10-16 오후)

증상: Codex 계정 둘이 모두 usage_cap(내 codex 11:43 KST, Daddy codex 13:09 KST 리셋)인데 날씨 브리핑이 계속 대기. Claude 계정은 여유.

**증거 (운영 DB 조회, 읽기 전용)**
- 대기 중인 Run은 2개. 둘 다 queued + run.waiting(until 11:43 KST).
  - weather_briefings 2026-10-16 행의 Run: 세션 engine=codex 고정, 10:33 KST에 구버전(cd0c78e)이 수동 생성. 행은 generating, analysis_engine/analysis_route가 비어 있고 updated_at이 10:33 그대로다. 새 코드가 한 번도 손대지 않았다는 뜻이다.
  - "Luna high 30문항×3 측정" worker 세션: engine=codex 명시 고정. 의도된 엔진 잠금이라 건드리지 않는다.
- engine=auto 세션은 정상이다. 이 수정 작업 자체가 auto 세션이고 claude/sonnet으로 돌았다. 일반 Run 매니저는 원인이 아니다.
- 운영 DB를 스크래치 DB로 복제해 weather.service.advance()를 돌리면 폴백이 동작했다. 로직이 아니라 호출되지 않는 것이 문제였다.

**원인 1 (근본): 운영 주기 작업에 날씨 scan이 없었다.** 운영은 runtime_generation 모드라 main._reminder_loop가 아니라 generation_service.lifespan의 periodic(...) 목록이 주기 작업을 돈다. 날씨 scan()은 _reminder_loop에만 붙어 있었다. 결과적으로 매일 06:30 자동 실행, generating 행의 완료 수집, 한도 대기 폴백이 운영에서 한 번도 돌지 않았다. 수동 POST가 호출한 advance() 한 번이 전부였다. 오늘 06:30 자동 브리핑이 없었던 것도 같은 이유로 보인다(행이 manual, 10:33 생성).

**원인 2: 구버전 고정 엔진 Run의 폴백 경로.** _fail_over는 대기 Run을 취소하고 바로 Antigravity로 넘겼다. Claude가 여유 있어도 구버전 codex 고정 Run은 Antigravity로 가게 된다.

**수정**
- generation_service.maintenance_jobs()로 주기 작업 등록을 한곳에 모으고 ("weather", _weather, reminder_scan_seconds)를 추가했다. 새 주기 작업이 운영 경로에서 빠지지 않도록 이 목록을 테스트한다.
- _fail_over: 대기 Run의 engine이 auto가 아니면(구버전 고정 Run) 취소 뒤 choose_route를 다시 보고, auto가 가능하면 새 auto 세션으로 재시작한다. Claude가 model_counterparts로 이어 받고, 이력 사유는 locked_engine_waiting이다. 불가하면 기존대로 Antigravity, 그다음 degraded.
- 명시 고정 엔진 세션 정책은 그대로다. 날씨 모듈이 만든 세션과 Run만 정리한다.

**회귀 테스트**: test_stale_codex_locked_run_restarts_on_auto_while_claude_is_free, test_stale_codex_locked_run_goes_to_antigravity_when_claude_is_limited_too, test_generation_runtime_schedules_the_weather_scan. 수정 전에 첫 테스트가 `['antigravity'] == ['auto']`로 실패하는 것을 확인했다.

## 지난 날짜 브리핑 보기 (2026-10-16 프론트, 커밋 5d675d5)

요청: 오늘뿐 아니라 다른 날짜에 한 브리핑도 `/weather`에서 확인.

**백엔드 확인 (변경 없음)**
- `GET /api/weather/briefings`는 day 내림차순, limit 1~100(기본 30), 페이지네이션 없음. `GET /briefings/{day}`는 없으면 404. 지난 날짜 열람에는 이 둘로 충분해서 백엔드는 건드리지 않았고 배포 예약도 하지 않았다.
- 알림 링크는 이미 `/weather?date=YYYY-MM-DD`(service.py)라서 URL 키를 `date`로 유지했다.
- 미해결: 목록 응답이 행마다 raw_forecasts·forecast 전체를 싣는다. 30건이면 크기가 상당할 수 있는데 실측은 못 했다. 그래서 목록 30초 폴링은 없앴고(상세만 진행 중일 때 5초 폴링), 포그라운드 복귀와 생성 성공 때만 무효화한다. 필요하면 날짜·상태만 주는 경량 목록 API를 따로 만든다.

**프론트 변경**
- 주소의 `date`가 화면 상태의 유일한 출처다. 없으면 KST 오늘, 형식이나 달력상 잘못된 값(2026-02-30)은 오늘로 대체하고 안내 문구를 띄운다. 이동은 `replace`라서 뒤로가기가 날짜 순회가 되지 않는다.
- 날짜 이동은 ChecklistsPage 관례를 따랐다: 이전/다음(달력 ±1일), 날짜 입력(max=오늘), 오늘 버튼. 미래로는 이동 불가. 기존 이력 칩은 유지하고 상태 라벨을 완료/간이/실패/진행 중으로 구분했다.
- 상태 구분: 진행 중(수집/생성), degraded는 "간이 브리핑" 경고, 실패는 사유 표시, 브리핑 없는 날(404)은 별도 빈 상태. 오늘이 비어 있으면 "지금 만들기", 지난 날짜면 새로 만들 수 없다는 안내와 "가장 최근 브리핑 보기".
- 생성 API는 항상 오늘 행을 만들기 때문에 지난 날짜에서는 생성·다시 시도 버튼을 렌더하지 않고 핸들러도 오늘일 때만 호출한다. 대신 "오늘 브리핑으로" 버튼을 둔다.
- 문구: 지난 날짜는 "오늘/현재" 대신 "이날/브리핑 당시". 올해가 아닌 날짜는 제목에 연도를 붙인다. "Luna가 정리" 문구는 엔진이 바뀔 수 있어 중립 문구로 바꿨다.
- 푸터에 `analysis_engine`/`analysis_model`을 작게 표시한다(EngineBadge 재사용). 타입에 두 필드를 추가했다.

**검증 (front)**
- Vitest: WeatherPage 7건 + weatherData 5건 신규·갱신. 전체 75 files passed, 1 skipped; 492 tests passed, 1 skipped.
- ESLint, `tsc -b`, `vite build` 모두 exit 0.
- main push 04dd5f5→5d675d5, Vercel 커밋 상태 success("Deployment has completed").
- 미확인: 로그인 세션이 없어 실제 인증 화면에서 모바일 레이아웃(날짜 이동 줄, 칩 스크롤)을 눈으로 확인하지 못했다. 테스트는 jsdom 기준이다. 공유 worktree에 있던 AccountsSection 미커밋 변경은 이번 커밋에 넣지 않았다.
