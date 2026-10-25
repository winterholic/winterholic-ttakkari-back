# 2026-10-25 GitHub Copilot CLI (`copilot`) 실행기 엔진 추가 및 계정 인프라 연동

## 작업 목적
- ADR 0071(OpenCode)에 이어 공식 GitHub Copilot CLI(`copilot` v1.0.103)를 별도 실행기 `engine="copilot"`으로 추가.
- 호스트의 기존 시스템 인증(`~/.copilot`) 및 격리된 `COPILOT_HOME`을 지원하여 다중 계정 풀과 헤드리스 실행 스트림을 구축.

## 구현 상세
1. **계정 격리 및 자격증명 관리 (`src/ttakkari/copilot_accounts.py`)**:
   - `allocate`, `load`, `status`, `is_ready`, `execution_env`, `login_command`, `check_auth`, `record_run` 구현.
   - 시스템 기본 계정(`is_system=True`)과 격리 계정(`COPILOT_HOME` 지정) 지원.
2. **에이전트 어댑터 (`src/ttakkari/agents/copilot.py`)**:
   - `CopilotAdapter` 구현: `-p <prompt> --output-format json --allow-all-tools --no-ask-user`.
   - `--session-id <UUID>` 세션 재개 및 불일치 감지.
   - NDJSON 스트림 정규화: 메시지 델타, 도구 시작/완료, 세션/사용량, 오류 파싱.
3. **카탈로그 및 프로필 연동**:
   - `model_catalog.py`: `COPILOT_MODELS`, `COPILOT_DEFAULT = "auto"`.
   - `models.py`: `Engine.copilot`.
   - `schemas.py`: `EngineName`, `SessionEngine`에 `copilot` 추가.
   - `config.py`: `copilot_bin`, `worker_default_models["copilot"]`, `engine_models["copilot"]`.
   - `profiles.py`: `ENGINES`에 `copilot` 추가, `Profile.env`, `execution_ready`, `identity`, `model_engine`.
   - `manager.py`: `ADAPTERS["copilot"]`, 실행 기록, 프로세스 감시, 모델 런치.
   - `api/accounts.py`: 계정 등록 시 시스템/격리 홈 처리, `POST /{account_id}/copilot/check` 엔드포인트.
   - `seatbelt.py`: `.copilot` 디렉터리 보호 목록 추가.
   - `gate.py`: 사전 검증에 `unit_tests/test_copilot_accounts.py` 추가.
4. **단위 테스트**:
   - `unit_tests/test_copilot_accounts.py`: 9개 테스트 통과.
   - `unit_tests/test_copilot_adapter.py`: 7개 테스트 통과.

5. **독립 검증 결함 교정 (OpenCode & Copilot 공통)**:
   - 권한 경계: OpenCode `--auto` 위험 플래그 제거, `seatbelt.py`의 `WRAPPABLE_ENGINES`에 `opencode` 및 `copilot` 등록하여 `sandbox-exec` 커널 격리 강제.
   - MCP 로드: `OPENCODE_CONFIG` 환경변수를 통해 임시 `opencode.json`(`type: "local"`, `enabled: true`)을 프로젝트 루트 외부에 격리 주입.
   - 인증 실패 폴백: `runs/manager.py`에 `auth_failed` 감지 및 재시도 대상 제외, 남은 계정 핸드오프·폴백 연동.
   - 에러 파서: 중첩된 `error.data.message`, `cause.body.message`를 누락 없이 추출하도록 `_extract_error_message` 구현.
   - 한도 문구: `USAGE_LIMIT_RE`에 Copilot premium requests 초과 및 월간 한도 문구 정규식 추가.
   - 단위 테스트 7개 추가: `unit_tests/test_opencode_defects.py` (6개), `unit_tests/test_opencode_seatbelt.py` (3개), `unit_tests/test_account_fallback.py` (1개) 전원 통과.
