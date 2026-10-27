# ADR 0072: GitHub Copilot CLI (`copilot`) 실행기 엔진 및 시스템/격리 계정 연동

## 상태
구현 교정 / 운영 수용 검증 미완료

## 배경
- 따까리(Ttakkari)에 ADR 0071을 통해 `opencode` 엔진이 도입되었으나, Mac Studio 로컬 환경에는 이미 공식 GitHub Copilot CLI (`/opt/homebrew/bin/copilot` v1.0.103)가 설치되어 있고 시스템 기본 로그인(`~/.copilot`)이 구성되어 있음.
- 사용자는 `engine="opencode"`와는 별개로 공식 GitHub Copilot CLI를 직접 호출하는 `engine="copilot"`을 추가하여 GitHub Copilot 인프라를 상호 보완적으로 활용하고자 함.

## 결정

1. **독립된 엔진 식별자**:
   - `engine="copilot"`으로 `opencode`와 완전히 분리된 엔진 등록.
   - 최초(2026-10-25)에는 검증 전이어서 명시 선택만 허용했다. 2026-10-28 교정에서는 인증된 계정의 `auto` 모델을 일반·단순 작업 자동 배정 첫 단계에 포함한다. 월간 잔여량·현재 부하를 기존 엔진과 비교하며, 미확인 잔여량은 여유로 만들지 않는다.
   - 정상 기획자는 네이티브 Opus/Sol 우선이다. 네이티브가 사용 불가할 때 Copilot도 기존 Muse와 함께 비상 기획 후보가 된다. 접수 경로는 이번 변경 범위가 아니다.

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
     - 한도 오류 기반 쿨다운에 더해 2026-10-26부터 공식 SDK `account.getQuota`로 월간 프리미엄 잔여량을 수집한다. 2026-10-28에는 조회 HOME, 월간 한도의 배정·상한·수집 주기·표시 연결을 교정했다.
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

## 2026-10-28 통합 교정

- 위 미검증 문장은 10월 25일 판정이다. 10월 26~27일 대화의 직접 CLI 응답·재개·echo MCP 성공 보고와 따까리 운영 수용 검증을 구분한다. 이번 변경의 현재 증거는 [작업 기록](../work-history/2026-10-28-copilot-parity.md)을 따른다.
- 설치·로그인과 계정 등록은 별개다. 개인 기획용 `activate_copilot_account`가 기존 시스템 로그인을 모델 probe로 확인하고 계정을 등록/재확인·활성화한 뒤 quota를 수집한다. 실패한 계정도 비활성 상태로 보이며, 반복 호출은 기존 계정을 재사용한다. 회사 계정의 범위나 토큰을 바꾸지 않는다.
- 새 quota 조회 프로세스가 반환한 `resetDate`가 관측 시각보다 과거이면 다음 초기화 시각으로 사용하지 않는다. 실측 SDK 1.0.26/CLI 1.0.103에서 이 값은 관측보다 수 초 앞선 시각이었다. [SDK 이슈 #2619](https://github.com/github/copilot-sdk/issues/2619)의 보고와 일치하지만 해당 필드가 모든 버전에서 fetch 시각이라는 보편적 주장은 하지 않는다. 잔여량은 새 관측으로 보존하고 다음 초기화일은 미확인으로 둔다. 소진·상한 도달은 짧은 재조회 대기로 막으며 무료·무제한·다음 달 날짜를 추정하지 않는다.
- Copilot의 공통/역할/기획 계약·MCP·필수 Seatbelt 경계를 유지하고, 컨텍스트 이벤트·환경 probe 등록·하네스 레이어 조회 누락도 연결한다. 공용 스킬은 CLI의 `~/.agents/skills` discovery를 사용한다. Claude 전용 훅을 Copilot에 적용했다고 표시하지 않는다.
- `auto`가 실제 모델을 고르므로 일반·단순 작업 결과는 별도 GPT/Claude 검증을 요구한다. 디자인·독립 검증·고난도 추론 작업의 기본 자동 후보로는 추가하지 않는다.
