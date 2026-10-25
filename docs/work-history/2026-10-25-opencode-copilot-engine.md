# 2026-10-25 OpenCode 엔진 및 GitHub Copilot 계정 인증 연동

## 목적
따까리에 신규 실행기 엔진 `opencode`(sst/opencode CLI v1.19.4)를 추가하고, GitHub Copilot 구독 계정 인증으로 모델(GPT-5.6 Sol, GPT-5.4 Nano, GPT-6.1 Sol, Claude Sonnet 5.5, Gemini 3.8 Flash 등)을 사용할 수 있도록 계정 관리 및 어댑터 인프라를 구축한다.

## 작업 내용

1. **OpenCode CLI 설치 및 헤드리스 환경 검증**:
   - `~/.opencode/bin/opencode` (v1.19.4 darwin-arm64) 설치 확인.
   - macOS 샌드박스 환경에서 쓰기 가능한 `TMPDIR=/Users/winterholic/development/.tmp` 설정.
   - `opencode run --format json`의 NDJSON 스트림 이벤트 구조(`step_start`, `text`, `reasoning`, `tool_use`, `step_finish`, `error`) 및 `--session`, `--model`, `--auto` 플래그 분석.
   - stdin이 TTY가 아닐 때 EOF를 대기하는 CLI 특성에 맞춰 subprocess stdin을 `/dev/null`로 연결.

2. **계정 격리 및 자격증명 모듈 구현 (`src/ttakkari/opencode_accounts.py`)**:
   - 계정별 격리 홈: `~/.ttakkari/accounts/<account_id>/opencode/`
   - `XDG_DATA_HOME` (`data/opencode/auth.json`), `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME` 분리 주입.
   - 자격증명 토큰·auth.json 내용을 절대 읽거나 노출하지 않는 보안 불변 조건 준수.
   - `check_auth`를 통해 공식 `opencode auth list` 출력 기반 `authenticated` / `setup_required` 판정.
   - 실행 중 인증 오류(401, token expired 등) 발생 시 `record_run`에서 `auth_expired`로 표시하고 다음 계정으로 폴백.
   - Mac Studio 터미널에서 실행할 디바이스 로그인 명령어(`login_command`) 제공.

3. **엔진 어댑터 구현 (`src/ttakkari/agents/opencode.py`)**:
   - `OpencodeAdapter` (AgentAdapter 프로토콜 구현).
   - `build_command`: 헤드리스 JSON 스트림 실행, 세션 ID 재개, Copilot 모델 접두사(`github-copilot/...`), `--variant` 매핑.
   - `parse_line`: JSON 스트림을 따까리 표준 이벤트(`agent.session`, `message`, `tool.call`, `tool.result`, `guard.blocked`, `usage`, `error`)로 정규화.
   - 세션 재개 불일치 시 fail-closed 검증.
   - 툴 사용 시 파일 수정(`touched_files`) 추적 및 `[ttakkari-guard]` 차단 감지.
   - 사용량 한도 오류 감지 시 쿨다운 트리거.

4. **모델 카탈로그 및 런타임/오케스트레이터 연동**:
   - `src/ttakkari/model_catalog.py`: `OPENCODE_COPILOT_MODELS` 및 기본 모델 `github-copilot/gpt-5.6-sol` 정의.
   - `src/ttakkari/models.py`, `schemas.py`: `Engine`, `EngineName`, `SessionEngine` enum에 `opencode` 추가.
   - `src/ttakkari/config.py`: `opencode_bin`, `worker_default_models["opencode"]`, `engine_models["opencode"]` 등록.
   - `src/ttakkari/runs/profiles.py`: `ENGINES`에 `opencode` 추가, 계정별 XDG 환경변수 반환, `identity`, `execution_ready`, `model_engine` 연동.
   - `src/ttakkari/runs/manager.py`: `ADAPTERS["opencode"]` 등록, 프로세스 종료 시 `record_run` 연동, 프로세스 모니터링 연동.
   - `src/ttakkari/api/accounts.py`: 계정 등록 시 격리 홈 자동 생성, 로그인 명령 제공, `POST /{account_id}/opencode/check` 인증 상태 확인 엔드포인트 제공.
   - `src/ttakkari/orchestrator/mcp_server.py`: `spawn_worker`의 `engine` 옵션에 `opencode` 추가.
   - `src/ttakkari/security/seatbelt.py`: `SECRET_DIRS`에 `.opencode` 추가.

5. **프론트엔드 연동**:
   - `src/api/types.ts`, `src/api/client.ts`: `opencode` 엔진 타입 및 `checkOpencodeAccount` API 추가.
   - `src/ui/EngineBadge.tsx`, `engineBadge.css`: OpenCode 뱃지 및 테마 스타일 추가.
   - `src/features/accounts/AccountsSection.tsx`: 계정 추가 모달에 OpenCode 옵션 추가, 로그인 명령 복사 및 터미널 디바이스 인증 후 '인증 확인' 버튼 제공, 인증 상태 배지 연동.
   - `src/features/accounts/OverrideDialog.tsx`: 엔진 라벨 매핑 추가.

6. **검증 결과**:
   - 백엔드 단위 테스트 17개 전원 통과:
     `unit_tests/test_opencode_accounts.py` (8/8 passed)
     `unit_tests/test_opencode_adapter.py` (9/9 passed)
   - 백엔드 Ruff lint 전원 통과 (`All checks passed!`).
   - 프론트엔드 Vitest 14개 테스트 통과 (`AccountsSection.test.tsx` 14/14 passed).
   - 프론트엔드 TypeScript 타입 검사 통과 (`tsc --noEmit` exit 0).
   - 프론트엔드 Vite 프로덕션 빌드 통과 (`vite build` exit 0).
   - 실제 CLI 스모크 테스트: 임시 계정 홈에서 `opencode auth list` 정상 실행 및 초기 `setup_required` 판정 확인.
