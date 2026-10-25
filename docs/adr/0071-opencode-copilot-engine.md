# ADR 0071: OpenCode 엔진 및 GitHub Copilot 계정 인증 연동

- 상태: 구현 완료 / 운영 검증 대기
- 날짜: 2026-10-25

## 배경

Ttakkari(Mac Studio 무인 운영 비서)에 새로운 실행기 엔진으로 OpenCode(sst/opencode CLI v1.19.4, https://opencode.ai)를 추가하고, 사용자의 GitHub Copilot 구독 계정 인증을 통해 GPT-5.6 Sol, GPT-5.4 Nano, GPT-6.1 Sol, Claude Sonnet 5.5, Gemini 3.8 Flash 등의 모델을 작업 세션 및 에이전트 실행에 활용할 수 있도록 한다.

계정 관리는 기존 Claude, Codex, Antigravity, Muse 엔진과 동일한 수준으로 지원해야 한다:
- 계정 등록·삭제·이름변경
- 개인/회사 pool 분류, 활성화/비활성화, 우선순위 및 임시 순서 override
- 인증 상태 확인 및 격리 홈 보관
- 세션 유지 및 대화 재개, 요청 대화 불일치 시 fail-closed
- 한도 도달 감지, 쿨다운, 다음 계정 폴백
- 토큰 만료 감지, 재인증 안내

## 결정 사항

### 1. CLI 실행 방식 및 프로토콜 정규화

- 바이너리: 공식 배포 바이너리(`~/.opencode/bin/opencode`)를 사용하며, 기본 경로는 `config.opencode_bin`으로 제어한다.
- 헤드리스 실행 명령:
  `opencode run --format json --pure --auto --dir <workspace> --model github-copilot/<model>`
  - `--format json`: 라인 단위 NDJSON(JSON stream) 이벤트 출력.
  - `--pure`: 외부 임의 플러그인 로드 방지.
  - `--auto`: 무인 환경에서 파일 수정 및 도구 실행 자동 승인.
  - `--session <id>`: 기존 세션 재개.
- stdin 처리: OpenCode CLI는 TTY가 아닐 때 stdin EOF를 대기하므로, 백엔드 subprocess 실행 시 stdin을 `/dev/null`로 연결하여 블로킹을 방지한다.
- 이벤트 정규화:
  - `step_start`: `sessionID` 추출 및 `agent.session` 이벤트 발행. 요청 세션 ID와 일치하지 않으면 `resume_mismatch=True`로 즉시 중단(fail-closed).
  - `text`: 증분 텍스트를 `message` 이벤트로 발행하고 최종 결과 텍스트 누적.
  - `reasoning`: 모델 사고 과정을 생각 블록으로 정규화.
  - `tool_use`: `tool.call` 및 `tool.result` 이벤트로 변환. 파일 쓰기 도구(`write`, `edit` 등) 감지 시 `touched_files` 추적, `[ttakkari-guard]` 차단 감지 시 `guard.blocked` 이벤트 발행.
  - `step_finish`: `tokens` 및 `cost`를 파싱하여 `usage` 이벤트 발행.
  - `error`: 에러 이벤트 발행 및 사용량 한도 휴리스틱 적용.

### 2. 계정 격리 및 자격증명 보안

- 계정별 독립 데이터 홈:
  `~/.ttakkari/accounts/<account_id>/opencode/`
  - `data/` -> `XDG_DATA_HOME` (`data/opencode/auth.json` 보관)
  - `config/` -> `XDG_CONFIG_HOME` (`config/opencode/opencode.json` 보관)
  - `state/` -> `XDG_STATE_HOME`
  - `cache/` -> `XDG_CACHE_HOME`
- 보안 불변 조건 준수:
  - 자격증명 파일(`auth.json`) 내부의 OAuth 토큰, refresh token 등 민감 정보를 읽거나 출력·로깅하지 않는다.
  - 인증 상태 확인은 `opencode auth list` 공식 CLI 실행 출력 또는 파일 존재/mtime 수준으로만 판정한다.
  - Seatbelt 정책의 `SECRET_DIRS`에 `.opencode`를 추가하여 샌드박스 내부 프로세스가 호스트 전역 설정을 건드리지 못하게 차단한다.

### 3. GitHub Copilot 로그인 및 재인증 UX

- GitHub Copilot은 Device Code OAuth 플로우를 사용한다.
- 따까리 웹 UI(설정 > 계정)에서 계정 추가 시 격리된 전용 환경변수가 포함된 로그인 명령을 제공:
  `TMPDIR=... XDG_DATA_HOME=... XDG_CONFIG_HOME=... opencode auth login -p github-copilot`
- 사용자가 Mac Studio 터미널에서 이 명령을 실행하면 User Code가 발급되며, 브라우저(`github.com/login/device`)에서 코드를 입력해 승인한다.
- 웹 UI의 '인증 확인' 버튼을 누르면 백엔드가 `opencode auth list`를 실행하여 `authenticated` 상태를 확인하고 준비 완료 상태로 전환한다.
- 실행 중 401 Unauthorized 또는 token expired 감지 시 `record_run`에서 계정 상태를 `auth_expired`로 전환하고 다음 계정으로 폴백하며, UI에 재인증 안내를 표시한다.

### 4. 모델 카탈로그 및 라우팅 정책

- 제공 모델:
  `github-copilot/gpt-5.6-sol`, `github-copilot/gpt-5.4-nano`, `github-copilot/gpt-6.1-sol`, `github-copilot/claude-sonnet-5.5`, `github-copilot/claude-opus-4.7`, `github-copilot/gemini-3.8-flash`, `github-copilot/gemini-4-argon`, `github-copilot/mai-code-1.1-flash`
- 기본 모델: `github-copilot/gpt-5.6-sol`
- 기본 모델 라우팅(`worker_routing.py`의 auto 단계표)에는 opencode를 자동 편입하지 않는다(불변 조건).
- 명시적 `engine="opencode"` 선택 시에만 배정하며, auto 편입은 운영 수용 검증 후 후속 결정으로 추진한다.
- `spawn_worker`의 engine 선택지에 `opencode`를 지원한다.

### 5. MCP(Model Context Protocol) 도구 연동

- 오케스트레이터 Run 실행 시 세션 임시 디렉토리의 `opencode.json`에 `mcp` 항목을 로컬 stdio 서버 형식으로 주입한다:
  ```json
  {
    "mcp": {
      "ttakkari": {
        "type": "local",
        "command": ["python3", "-m", "..."],
        "environment": { ... }
      }
    }
  }
  ```
- Run 종료 시 임시 설정을 정리하여 프로세스 외부 파일 유출을 방지한다.

## 검증 범위 및 실측 근거

- **검증 완료**:
  - OpenCode CLI v1.19.4 설치 및 도움말, 모델 목록, auth 명령어 동작 확인.
  - 계정 격리 홈(`data`, `config`, `state`, `cache`) 생성 및 XDG 환경변수 분리 검증.
  - 단위 테스트(`unit_tests/test_opencode_accounts.py` 8개, `unit_tests/test_opencode_adapter.py` 9개, 전체 17개) 통과.
  - 프론트엔드 `AccountsSection.test.tsx` 14개 테스트 통과, `tsc --noEmit` 통과, vite production build 통과.
  - Ruff lint 검사 통과.
- **미검증 항목 (확인 필요)**:
  - 실제 사용자 브라우저를 통한 GitHub Copilot OAuth Device Code 최종 승인 단계(무인 자동화 불가능, 사용자 직접 승인 필요).
  - 로그인된 GitHub Copilot 유효 세션에서의 실제 장시간 대화 응답 및 토큰 차감 실측.
