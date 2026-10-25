# ADR 0072: GitHub Copilot CLI (`copilot`) 실행기 엔진 및 시스템/격리 계정 연동

## 상태
구현 교정 / 운영 수용 검증 미완료

## 배경
- 따까리(Ttakkari)에 ADR 0071을 통해 `opencode` 엔진이 도입되었으나, Mac Studio 로컬 환경에는 이미 공식 GitHub Copilot CLI (`/opt/homebrew/bin/copilot` v1.0.103)가 설치되어 있고 시스템 기본 로그인(`~/.copilot`)이 구성되어 있음.
- 사용자는 `engine="opencode"`와는 별개로 공식 GitHub Copilot CLI를 직접 호출하는 `engine="copilot"`을 추가하여 GitHub Copilot 인프라를 상호 보완적으로 활용하고자 함.

## 결정

1. **독립된 엔진 식별자**:
   - `engine="copilot"`으로 `opencode`와 완전히 분리된 엔진 등록.
   - 모델 라우팅(`session_engine="auto"`)에 자동 편입되지 않으며, 명시적 `engine="copilot"` 선택으로만 배정.

2. **계정 격리 및 기본 시스템 계정 지원**:
   - 첫 번째 등록 계정은 `config_dir=None`을 허용하여 호스트의 기본 `~/.copilot` 자격증명을 재사용(`system_default`).
   - 추가 등록 계정은 전용 `COPILOT_HOME`(`~/.ttakkari/accounts/<account_id>/copilot`)을 생성하여 완벽히 격리.
   - 자격증명 원문이나 토큰은 절대 파싱하거나 로깅하지 않으며, 파일 존재나 설정 조회 성공을 인증 증거로 쓰지 않는다. 읽기 전용 도구만 노출한 최대 40초 모델 응답 확인으로 인증 상태를 기록한다. 인증 확인은 구독 요청을 소모할 수 있다.
   - 대화형 로그인 안내 명령은 `copilot login --device-code` (또는 `COPILOT_HOME=... copilot login --device-code`) 제공.

3. **헤드리스 NDJSON 이벤트 스트림 어댑터 (`CopilotAdapter`)**:
   - 실행 명령: `copilot -p <prompt> --output-format json --allow-all-tools --no-ask-user -C <cwd>`
   - Seatbelt 샌드박스: 자체 샌드박스가 없으므로 `sandbox-exec`로 래핑하여 워크스페이스 외부 쓰기 및 보호 디렉터리, DB 포트 접근을 커널 수준에서 차단.
   - 세션 재개: CLI의 `--session-id <UUID>` 플래그를 통한 상태 지속. 세션 ID 불일치 발생 시 즉각 중단(fail-closed).
   - 이벤트 정규화:
     - `assistant.message_delta` -> 스트리밍 텍스트 델타 (`Normalized("message")`)
     - `tool.execution_start` / `tool.execution_complete` -> 도구 호출 및 결과 추적
     - `result` -> 종료 코드, 세션 ID, 토큰 사용량(`usage`) 추출
     - 한도 초과(monthly limit of premium requests, 429 등) 감지 및 자동 쿨다운 연동. 공식 잔여 quota API가 없으므로 오류 이벤트 기반으로 쿨다운 및 계정 회피를 수행.
     - 인증 실패(401, unauthorized) 감지 시 해당 계정 `auth_expired` 설정 및 남은 계정으로 핸드오프·폴백.
   - MCP 서버 연동: `--additional-mcp-config @<path>` 플래그를 통해 세션별 임시 MCP 설정 파일 주입.

4. **프론트엔드 연동**:
   - `AccountEngine` 및 `EngineName`에 `copilot` 추가.
   - 보라색 테마의 Copilot 뱃지(`✦`) 적용.
   - 계정 관리 UI에 Copilot 계정 추가 및 로그인 명령 복사, 원클릭 인증 확인 기능 지원.

## 결과 및 효과
- 공식 GitHub Copilot CLI와 sst/opencode 양쪽 모두를 독립 실행기로 지원하여 GitHub Copilot 구독 모델을 유연하게 활용 가능.
- 기존 로컬 인증 자산(`~/.copilot`)을 손상 없이 활용하면서도 다중 계정 격리 인프라 유지.

## 재검증 후 계정 상태 교정 (2026-10-25)

- 시스템 기본 계정도 `accounts/00000000-0000-0000-0000-000000000000/copilot/manifest.json`에 비밀이 아닌 관측 상태를 보존한다. 실제 호스트 자격증명은 이동하거나 조회하지 않는다.
- `record_run` 성공 또는 유효한 모델 응답 probe만 `authenticated`의 증거다. 기존 파일 존재 기반의 authenticated 표시는 unknown으로 내려 실행을 보류한다.
- 401/만료 실패는 기본 계정에도 auth_expired로 보존하며 다음 계정 폴백을 기존 매니저 통합 테스트에서 검증한다.
- inherited token/BYOK 환경변수가 선택 계정을 바꾸지 못하도록 제거한다. `COPILOT_ALLOW_ALL=true`의 묵시적 폴더 신뢰를 제거하고 명시 도구 승인 옵션만 사용한다.
- `You have exceeded your premium request allowance`를 한도로 분류하고, `quota`가 들어간 일반 안내는 한도로 보지 않는다.
- 운영에서의 인증/실제 모델 응답/대화 재개/MCP 도구 호출은 각각 미검증이다. 중첩 Codex 환경의 EPERM은 호스트 실패의 근거가 아니다.
