# 하네스 조회 API (ADR 0069, 2026-10-23)

## 결과
- 요청: 사용자의 전체 하네스(Claude Code·Codex·Antigravity·따까리 주입 지침)를 한 곳에서 읽기 전용으로 조회하고, 세션 종류·엔진별로 실제 합쳐지는 지침 레이어를 순서대로 보여 준다.
- 작업 위치: 공유 checkout(main)에 미커밋 WIP(스테이지된 agent_control 삭제, config·collector 변경 등)와 origin/main과의 분기가 있어 건드리지 않고 `origin/main` 기준 별도 worktree `back-harness-view`(브랜치 feat/harness-view-20261023)에서 작업했다. 프런트도 같은 방식(front-harness-view).
- 구현 커밋: 9db5276(API·시험·ADR), 이 문서는 후속 docs 커밋. origin/main(79ec8f1) 위로 리베이스했다. ADR 번호는 0067(agent-global-stop)·0068(creative-engine)이 다른 브랜치에서 쓰여 0069다.
- 구성: `src/ttakkari/harness_view/{safe,model,sources,ttakkari_side,layers,service}.py`, `api/harness.py`. 새 MCP 도구·DB 스키마·세대 프로토콜 변경 없음.

## 설계 결정
- 주입 지점 정확성: `runs/manager._system_note` 를 `_system_note_parts`(레이어 id, 본문 목록)로 나누고 기존 함수는 이를 `\n\n` 으로 잇기만 한다. 화면의 레이어는 같은 목록을 쓴다. 엔진별 전달 방식(Claude `--append-system-prompt`·`--setting-sources`, Codex `developer_instructions`, Antigravity 래퍼, Muse spec, Qwen 짧은 계약, 접수 Qwen → codex/claude 폴백)은 어댑터 코드를 읽어 정리했고 `evidence=code` 로 표시한다. 엔진 CLI 가 스스로 읽는 파일은 `evidence=external`(확인 필요)로 구분한다.
- 읽기 안전: 루트·상대 패턴을 코드에 고정(`*` 는 폴더 경계 불가). 심볼릭 링크는 실제 경로가 허용 안일 때만. `.credentials.json`·`auth.json`·`history.jsonl`·`antigravity-oauth-token`·`.env` 이름 차단. `.claude.json` 은 mcpServers 키만. 따까리 계정 폴더는 `projects/*/memory/*.md` 만. 안티그래비티 `mcp/` 세션 폴더(토큰 가능)는 읽지 않는다. 깨진 참조 검사의 존재 확인은 probe 루트 안에서만, 홈 루트는 `.claude.json` 읽기 전용(probe=False).
- 마스킹: 구조는 `mask_config`(env·headers 값 전체, 비밀 키 값, `--api-key X` 인자, 명령은 엄격 규칙). 문서 본문은 신규 `oplog.scrub.scrub_prose`. 기존 `scrub` 로 문서를 가렸더니 `Basic Auth`·`dev-auth 토큰` 같은 산문 55곳이 `[REDACTED]` 로 바뀌어 읽을 수 없어서 문서용을 분리했다(실데이터 55곳 → 3곳). 한계: 형식을 모르는 임의 문자열 비밀은 본문에서 못 잡는다.
- 실데이터 시험에서 잡은 결함: MCP 항목 설명(description)이 `--api-key X` 를 가리지 못해 검색 인덱스로 새어 나갔다. 카나리 시험이 잡았고 구조 마스킹된 값으로 설명을 만들도록 고쳤다.

## 검증
- 대상 시험(리베이스 후, 단일 프로세스): `pytest tests/test_harness_view_safe.py tests/test_harness_view_api.py tests/test_route_inventory.py tests/test_security_gate*.py unit_tests/test_oplog_scrub.py tests/test_runs_events_masking.py tests/test_oplog.py tests/test_execution_capabilities.py tests/test_api_runs.py tests/test_antigravity_model_routing.py` → `233 passed`.
- 신규 시험 37개: 경로 화이트리스트·폴더 경계·비밀 이름·심볼릭 링크 탈출(루트 밖·루트 안 비밀·루트 안 허용)·크기 절단·`.claude.json` 부분 읽기·마스킹(env·headers·인자·본문·멱등)·frontmatter 파싱, 소유자 401·에이전트 토큰 401/403(4개 경로), 12종 카나리(env·헤더·인자·MCP env·자격증명·히스토리·계정·TOML·codex auth·OAuth·.env·본문 토큰)가 요약·목록 전 페이지·상세 전 항목·레이어 20조합 응답에 한 번도 나오지 않음과 카나리 검색 0건, 요약 개수·경고 종류, 검색·필터·상세·구간 이동, 레이어와 `_system_note_parts` 일치(기획·메인·작업자), 없는 지침 레이어의 미적용 표시, MCP 도구 항목이 `server.list_tools()` 와 같음, DB 계정·계정 메모리.
- 전체 pytest(리베이스 전 4f14f38 + 변경): `9 failed, 4273 passed, 14 skipped, 2 xfailed, 5020 subtests passed in 940.18s`. 실패 9건은 모두 alembic 마이그레이션 시험(test_game_migration 3, test_google_account 2, test_knowledge_migration 2, test_meeting_lineage_migration 1, test_retire_clovanote_migration 1)이고, **순수 origin/main(79ec8f1) worktree 에서 같은 9건이 같은 방식(`alembic upgrade 0025` 실패)으로 실패**했다. 변경과 무관한 기존 환경 문제로 본다(원인 미조사, 확인 필요).
- 첫 전체 실행은 무효였다: 이 Run 은 전용 `TTAKKARI_TEST_DB` 를 받아 conftest 가 명시 DB 모드로 도는데, 같은 DB 에 다른 pytest 를 동시에 돌려 deadlock·게임 시험 실패가 났다. 단독 재실행으로 위 결과를 얻었다. 같은 Run 안에서 pytest 를 동시에 돌리지 말 것.
- 실데이터 스냅샷(홈의 ~/.claude·~/.codex·~/.gemini·~/.agents, 따까리 홈은 임시 폴더): 항목 581개, 빌드 1.5~3.7초, 경고 20건(깨진 참조 5: stock-momentum-runner·stock-trend·harness-engineering 스킬 참조, access-map 메모리의 context-work-history 경로 등, 중복 이름 12 = claude.ai 동기화 스킬과 로컬 스킬 이름 충돌, 너무 긴 문서 2). 따까리 홈(~/.ttakkari) 파일은 이 작업에서 읽지 않았다(가드 보호 영역). 화면 스크린샷의 비서 지침·고정 기억은 시험용 예시 문구다.

## 미검증
- 운영 서버에서의 실제 응답과 로그인 UI(소유자 인증은 시험에서 토큰으로만 확인). 따까리 홈의 실제 harness 파일·계정 폴더·capability 측정 파일은 읽어 보지 못했다.
- 엔진 CLI 가 `CLAUDE.md`·`AGENTS.md`·`GEMINI.md`·`.agents/rules` 를 실제로 읽는 시점과 순서, Antigravity 의 GEMINI.md 로드 여부(external 레이어는 코드 밖 동작이라 확인 필요).
- 전체 pytest 는 리베이스 전 트리에서 돌렸다. 리베이스로 들어온 변경(creative 계획·qwen 시험 9파일)은 대상 시험만 재실행했다.
- 기존 실패 9건의 원인.
- `request_backend_deploy` 예약 수락은 배포 성공이 아니다. 예약 ID와 상태는 최종 보고에 있다.
