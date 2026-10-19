# 게임 스튜디오 실제 에이전트 E2E 실행 기록 스크립트 (TASK-G14)

로컬 격리 따까리에서 실제 worker 로 12단계를 돌릴 때 쓴 스크립트다. 경로·포트가 실행 당시 값으로 박혀 있어 그대로 재사용하기보다 절차의 기록으로 둔다.
자동으로 재현되는 부분은 `tests/test_e2e_game_studio.py`(기록된 worker 소스 재생)다.

격리 환경(운영 설정·`.env` 를 읽지 않는다):

- 별도 DB 하나, 별도 `TTAKKARI_HOME`, `TTAKKARI_NO_ENV_FILE=1`, `TTAKKARI_ALLOWED_ROOTS`/`TTAKKARI_GAMES_ROOT` 를 임시 디렉터리로
- `TTAKKARI_GAME_ORIGIN_HOST=127.0.0.1:18795`, `TTAKKARI_GAME_ORIGIN_URL=http://127.0.0.1:18795`, `TTAKKARI_GAME_FRAME_ANCESTORS=["http://127.0.0.1:5410"]`
- `uv run ttakkari migrate` 후 `ttakkari serve --port 18790`, `ttakkari serve-games --port 18795`, front 는 `VITE_API_BASE=http://127.0.0.1:18790 pnpm exec vite --port 5410`

순서: `step1.py`(스택 선정 기록·프로젝트 scaffold·등록·잠금 승인) → `step4.py`(develop 요청) → `lib.py build v1` → `lib.py rules v1`(실제 브라우저 규칙 검증)
→ `tests/game_smoke/e2e_cockpit.mjs`(관제탑 UI: observe → feedback) → 수정 요청 Run 완료 후 `lib.py build v2`, `lib.py rules v2` → 롤백 API 호출 → 잠금 위반 확인.
