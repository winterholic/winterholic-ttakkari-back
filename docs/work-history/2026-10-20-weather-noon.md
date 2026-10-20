# 날씨 브리핑 낮 12시 변경

- 사용자 요청: 날씨 브리핑을 12시로 변경. 매일 12:00 Asia/Seoul로 적용한다. 2026-10-16 구현 기록의 06:30 결정은 이 요청으로 대체된다.
- `weather/service.py`: 자동 시작 임계시각과 `scheduled_at` 저장값을 공통 `BRIEFING_TIME = time(12, 0)`으로 변경. 수동 생성, 날짜 중복 방지와 진행 중 브리핑 회수는 기존 동작을 유지한다.
- 회귀 검사: 06:30·11:59에는 시작하지 않고 12:00·12:01에는 시작, 예정 시각 UTC 변환, 같은 날짜 중복 방지.
- 검증: `.venv/bin/pytest -q tests/test_weather.py tests/test_weather_engine.py tests/test_weather_api.py` → 33 passed. 변경 전 새 검사 3 failed, 2 passed로 이전 시각을 검출했다. 변경 파일 Ruff와 `git diff --check` 통과.
- 프론트 별도 브랜치 `fix/weather-noon-20261020`: 매일 낮 12:00 안내, 예약 시각은 행의 `scheduled_at`로 표시해 과거 브리핑 시각을 보존한다. 날씨 Vitest 12 passed, TypeScript·Vite build·변경 파일 ESLint 통과.
- 배포: clean commit/push 후 generation 예약. 예약 접수는 운영 전환 성공과 구분한다. 실제 다음 정오 자동 발송은 아직 미검증.
