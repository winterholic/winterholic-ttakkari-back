# ADR 0029: Antigravity CLI 작업 엔진

- 상태: 구현 중
- 날짜: 2026-10-15

## 2026-10-16 모델 배정 방침 변경

사용자 요청으로 아래 초기 Gemini-only·Claude 기본 배정 제외 결정을 대체한다. `agy models` 1.3.6에서 확인한 Sonnet/Opus 5.5, Gemini 4 Argon, Flash 3.8 low/medium/high와 GPT-OSS medium을 허용한다. 일반 작업의 Antigravity 기본값은 Sonnet high, 단순 범용 작업은 Flash high, 어려운 설계·추론·독립 대안은 사유를 명시한 Argon/Opus다. 메인 모델 방침은 유지한다.

Claude/GPT, Gemini, 별도로 노출되는 Argon quota를 선택 모델에 따라 확인하며, 한 그룹의 소진·실행 거절이 다른 그룹을 막지 않도록 한다. AI 크레딧은 baseline allowance와 별개이며, 이 변경은 유료 overage를 켜지 않는다. 모델 목록·단발 응답과 MCP/대화 재개 성공은 별도 검증한다. 진행 중인 연결·설정 수명 수정의 완료를 이 모델 배정 변경으로 대신 주장하지 않는다.

공식 근거: [모델](https://www.antigravity.google/docs/models), [baseline과 overages](https://www.antigravity.google/docs/plans). 실제 계정의 2026-10-16 `/usage`에는 Argon의 별도 주간 창이 있었으며, 이 계정 상태를 모든 요금제에 일반화하지 않는다.

## 초기 결정 (2026-10-15, 모델 제한은 위 방침으로 대체)

Ttakkari는 유료 SDK 대신 사용자가 로그인한 공식 단일 `agy` CLI를 headless NDJSON 어댑터로 실행한다. 초기 지원 모델은 계정의 `agy models` 출력으로 확인된 Gemini 3.8 Flash low/medium/high이며, 기본 worker 배정은 `gemini-3.8-flash-high` / `high`다. Claude·Codex 계정의 자동 순환에는 넣지 않는다. 명시적인 `engine="antigravity"` 선택만 허용한다. 모델 quota와 AI 크레딧은 `/usage`·`/credits`의 정상 CLI 응답에서 관측 가능한 별도 단위로 수집한다. 조회 실패·stale·unknown은 0% 또는 무한 여유로 해석하지 않는다.

기존 모델 정책과 상위 worker 설정은 Antigravity Claude/GPT 모델을 선택하지 않는다. 엔진 계정은 로그인된 단일 CLI를 나타내며 `config_dir` 기반 추가 계정은 지원하지 않는다.

## 실행 프로토콜

- 실제 `stream-json` NDJSON의 `init`, `step_update`, `result`, 도구 입력/출력, 상태, token usage를 Ttakkari Run 이벤트로 정규화한다.
- 재개 시 전역 최신 대화 옵션을 쓰지 않고 `--conversation <engine_session_id>`를 전달한다.
- `--sandbox`와 `--mode accept-edits`를 사용하며 `--dangerously-skip-permissions`는 사용하지 않는다. timeout·취소에서 프로세스 그룹과 임시 파일을 정리한다.
- MCP 설정은 세션 전용 임시 프로젝트의 `.agents/mcp_config.json`에 0600으로 만들고 실행 시도 종료 시 제거한다. `init` 뒤에도 CLI가 설정을 읽고 MCP를 연결하므로 `init`에서는 삭제하지 않는다. 프로세스 종료 처리 중 오류가 나도 설정 정리는 수행하며, 종료 오류는 실패로 전파한다. 토큰은 이벤트·로그로 기록하지 않는다.

## 2026-10-16 MCP 설정 수명 수정

실제 CLI 대조 검사에서 `init` 시 설정 삭제를 생략하면 인증된 MCP 호출이 이루어졌고, 제품의 `RunManager._pump`가 `init`에서 삭제하던 경로를 수정했다. 성공·오류·시간 초과·취소 때의 설정 정리를 자식 프로세스 기반 회귀 검사로 검증한다. 프로세스 그룹 신호가 거부된 경우에도 설정 파일을 남기지 않도록 종료 정리를 `finally`로 감쌌다. 신호 거부를 무시하거나 다른 신호 경로로 우회하지 않으며, 파일 정리가 프로세스 종료의 증거는 아니다.

수정본의 실제 CLI + 격리 DB/TCP backend 검사에서는 Flash 단발 응답, Flash·Argon의 인증된 `list_sessions` 무작위 표식 회수, 취소한 MCP 자식 PID 소멸·heartbeat 중단이 관측됐다. Sonnet은 MCP 호출 이벤트 뒤 backend 종료 처리의 `os.killpg(..., SIGTERM)`에서 `PermissionError`가 났으므로 해당 검사는 실패다. 명령 sandbox의 증거이며 운영 Sonnet 장애로 일반화하지 않는다. 실제 운영 프로세스의 모델 응답·MCP·대화 재개 및 장시간 안정성은 별도 검증이 필요하다. 이전 날짜의 미검증 기록은 당시 범위이며 이 수정의 성공을 의미하지 않는다.

## 공식 CLI 사용량 수집 (2026-10-15)

- 설치된 `agy 1.3.5`에서 `agy --print /usage --output-format json`은 성공 상태와 `command.data.groups[].buckets[]`를 반환했다. 각 버킷의 `window`, `remaining_fraction`, `reset_time`을 사용해 **모델 그룹별** 5시간·주간 quota를 표시한다. 현재 CLI 응답은 절대 요청 수/토큰 수가 아니라 잔여 비율이며, 그룹이 공유하는 quota를 개별 모델 값처럼 쪼개지 않는다.
- `agy --print /credits --output-format json`은 `command.data.remaining_credits`를 반환했다. 화면에는 이 잔액만 AI 크레딧 단위로 따로 표시한다. 이번 실제 응답에는 청구주기 사용량 필드가 없어 그 수치는 표시하지 않는다.
- `/usage` JSON 구조는 공식 안정 스키마로 문서화되지 않았다. [공개 CLI 저장소 이슈 #1045](https://github.com/google-antigravity/antigravity-cli/issues/1045)에서 현재 `command.data.groups[].buckets[]` 구조와 필드를 확인했으며, CLI 버전 변경 시 parser 회귀를 확인해야 한다.
- `GET /api/accounts/usage`와 `account_usage` MCP는 quota `windows`와 별도 `credits_balance`를 전달한다. `windows[].percent`는 사용률 정수, `windows[].remaining_percent`는 원천 잔여 백분율이다. 전체 Antigravity 계정 상한 퍼센트는 만들지 않고, 이 그룹 quota를 기존 auto 계정 headroom 계산에 넣지 않는다.
- 조회에는 로그인된 일반 사용자 프로필에서 공식 `agy` 실행만 사용한다. timeout·종료 코드·JSON 모양 오류는 조회 실패로 반환하고, 성공 응답 시각을 공통 stale 판정에 사용한다. 결과가 비어 있거나 오류일 때 0/100 기본값을 만들지 않는다. 계정 토큰·키체인·쿠키는 읽거나 복사하지 않는다.
- 2026-10-15 실측: CLI 1.3.5에서 Gemini 그룹 5시간 잔여 98.37%, 주간 잔여 99.16%, Claude/GPT 그룹은 각 100%, AI 크레딧 잔액 0. 별도 직접 CLI 실행과 Ttakkari parser 경로 모두 성공했다. 과거 Run sandbox의 로그인 오류와는 달리, 현재 정상 CLI 프로세스에서 같은 설치본으로 조회가 성공했다. 이는 계정 전체 로그아웃이 아니며, 과거 실패의 세부 argv/env 전체는 당시 로그에 남지 않아 더 정밀한 차이는 확정하지 않는다.
- 실행 차이 추가 확인: 같은 사용자 프로필·CLI 1.3.5·`CODEX_SANDBOX=seatbelt`에서 `agy models`, `/usage`, `/credits`의 직접 CLI 조회는 성공했다. `agy models` 시작 로그에 일시적인 “not logged in”과 app log/crash 파일 쓰기 `operation not permitted`가 먼저 나타났지만, 이후 시스템 keyring의 로그인 세션을 불러와 모델을 반환했다. 따라서 그 초기 문구만으로 로그아웃이라고 판단하면 안 된다. CLI의 quota 조회는 성공해도 에이전트 대화 실행이 성공한다는 뜻은 아니다. 실패했던 Run들의 argv·cwd·환경 전체 기록은 없어 status 명령과 model-turn 실행 차이 외의 원인을 단정하지 않는다.
- macOS GUI의 접근성·스크린샷 권한이 허용되지 않아 모바일 브라우저 렌더 확인은 미실행이다. 컴포넌트 테스트와 production build는 실행했다.

## 검증 한계와 배포 조건

2026-10-15 후속 검증에서는 설치된 CLI가 1.3.5였고, 정상 `agy models` 호출이 Gemini 3.8 Flash low/medium/high를 반환했다. 이전 1.2.10 CLI 실측(high 호출·파일 작성·workspace 밖 차단)은 그 날짜의 별도 증거로 유지한다. 현재 실행 환경에서 실제 headless 요청은 CLI가 `~/.gemini/antigravity-cli` 아래 conversation DB·brain·cache를 만들려다 `operation not permitted`를 기록했다. `HOME`을 임시 경로로 바꾸면 이 쓰기는 가능했지만 기존 로그인이 없어 모델 조회가 `Please sign in`으로 끝났다. 인증 자료를 복사하지 않았다. 따라서 독립 CLI 프로세스의 세션 저장과 연속성은 이 실행 환경에서 재검증하지 못했다.

이전 실제 `--conversation` 시도는 요청 대화를 찾지 못했다는 경고 뒤 새 ID를 반환했다. 후속 보강은 이미 저장한 `engine_session_id`와 CLI `init.conversation_id`가 다르면 새 세션으로 바꾸지 않고 오류로 중단한다. 응답 ID 불일치 회귀 테스트는 실패 후 수정되어 통과했지만, 무작위 표식의 독립 프로세스 회수는 아직 검증되지 않았다.

공식 문서는 workspace MCP 설정 위치를 활성 프로젝트의 `.agents/mcp_config.json`으로 지정한다. 실제 CLI 실행 로그는 해당 임시 프로젝트의 `safe_fixture` 서버를 연결하려 했음을 보였고, `init.tools`에는 MCP별 이름 대신 일반 `call_mcp_tool` dispatcher가 있었다. fixture는 `server/discover`를 처리하지 못해 1분 연결 대기 후 취소되었으므로 CLI를 통한 MCP 도구 호출 성공으로 세지 않는다. 한편 제품의 `mcp` 2.3.0 stdio 서버에 직접 modern `server/discover`와 `tools/list`를 보낸 검증은 성공해 도구 43개(작업 세션용 `share_file`, `notify_user` 포함)를 반환했다. 읽기 도구 호출도 서버 응답까지 도달했으나, 의도적으로 닫힌 loopback API 주소와 fixture token을 사용해 `isError=true`를 받았다. 이는 실제 로그인 세션의 API 접근·보고 성공 증거가 아니다.

`--sandbox`와 `--mode accept-edits`는 유지했고 `--dangerously-skip-permissions`는 사용하지 않았다. OS 실행 격리가 Antigravity 앱 저장소 쓰기를 막아 실제 Run→worker→MCP 접근/보고, 취소 후 자식 정리, 다중 허용 root/민감 경로 전수 fixture를 완료하지 못했다. 코드에는 요청 대화 ID 불일치 시 fail-closed 처리가 추가됐다. 추가로 CLI가 stdout에 결과 없이 exit 0을 내면 Run을 실패로 닫고 stderr 진단을 보존한다. 이는 성공 오판을 막지만, CLI가 모델 응답 전에 앱 저장소 쓰기에서 막히는 실행 경계를 해결하지 않는다. 이 경계 검증 전에는 넓은 프로젝트에 worker를 배정하거나 이 ADR을 승인 상태로 전환하지 않는다.

공식 경로 조사(2026-10-15): 설치된 `agy 1.3.5 --help`에는 상태 root override가 없다. `--log-file`은 로그만, `--project`는 project ID/name을 받는다. 공식 문서는 설정을 `~/.gemini/antigravity-cli/settings.json`, `--continue` 캐시를 `~/.gemini/antigravity-cli/cache/last_conversations.json`에 둔다고 명시한다. 저장 위치 override는 공개 저장소의 아직 열린 [AGY_HOME 요청 #155](https://github.com/google-antigravity/antigravity-cli/issues/155)에서도 기능 요청 상태다. `HOME` 변경·인증 복사·symlink·sandbox 완화는 허용된 경로가 아니며 수행하지 않았다. 따라서 현 실행 주체에서 `~/.gemini/antigravity-cli`에 필요한 기록 권한을 받을 수 있는 별도 공식 지원 경로는 확인되지 않았다.

2026-10-15 실행 경계 후속 판정: Codex 작업 도구에서 직접 실행한 `agy`는 도구 실행 sandbox의 자식이므로 그곳에서 관측한 `operation not permitted`는 해당 실행 주체의 증거다. Ttakkari `RunManager._attempt`는 `asyncio.create_subprocess_exec(*cmd, cwd=..., env=...)`로 adapter 명령을 직접 실행하며 추가 OS sandbox wrapper는 없다. 운영 설치 스크립트도 launchd `ProgramArguments`로 `uv run ttakkari serve`를 지정하고 사용자 `HOME`/`PATH`를 전달하며 sandbox wrapper를 설정하지 않는다. 따라서 도구 sandbox의 EPERM을 운영 backend의 실패 원인으로 일반화할 근거는 없다. 이것만으로 운영 backend가 Antigravity 앱 저장소에 쓸 수 있다고 입증되는 것도 아니다. 실행 중인 backend의 `/api/health`는 응답했지만 기존 인증이 필요한 `/api/system/info`는 401을 반환했고, 이 세션에는 기존 사용자 인증을 쓸 수 있는 Ttakkari Run 호출 경로가 없었다. 분리 review checkout은 운영 backend에 배포되지 않았으므로 실제 backend `agy` 호출·대화 재개·MCP tool 호출은 미실행이다. 이 검증은 **인증된 기존 backend Run 경로 부재로 blocked**이며 sandbox 원인이 재현됐다는 판정이 아니다.

첫 단발 worker의 성공 및 worker→보고 경로에는 재개된 엔진 대화가 논리상 필요하지 않다. `RunManager`가 worker Run 종료 뒤 parent의 `[작업 보고]` Run을 직접 생성하므로 보고도 Antigravity MCP 호출에 의존하지 않는다. 반면 각 Antigravity Run에는 현재 MCP 서버 설정이 전달되며, MCP 기능을 실제로 쓰는 작업은 도구 discovery·호출을 별도 검증해야 한다. 현재 계약에는 재개를 끄는 one-shot/capability 필드가 없고 후속 메시지는 저장된 대화 ID로 재개를 시도하므로, 단발 배정은 “한 번만 실행하고 후속 메시지를 보내지 않는 작업 지침”으로만 성립한다. 재개 불일치 때 새 대화로 조용히 폴백하지 않는 동작은 유지한다. 그러므로 첫 **단발·MCP 비의존** smoke task의 선행 조건으로 실제 대화 재개를 요구할 필요는 없지만, 일반 재사용 세션·MCP 의존 설계 작업을 안전하게 배정하기 위한 운영 검증 게이트는 여전히 남는다.

배포 전에는 해당 앱 저장소 쓰기 권한이 이미 허용된 정상 실행 환경에서만 무작위 표식 기반 대화 재개, 실제 Ttakkari MCP tool 호출·결과와 worker 보고, stop/자식 프로세스 정리, 허용/금지 파일 fixture 및 오류 cleanup을 확인해야 한다. 이 권한 없이는 실제 CLI 검증은 미실행으로 유지한다.

## 통합 상태 (2026-10-15)

- Antigravity adapter·API·MCP 계약 변경은 back `main`에 통합됐다. 기준 커밋 `f89709a93c008a447d19c9410a7ee155ab182073`의 Qwen router feedback export는 보존했다.
- Adapter·policy·spawn cleanup 집중 테스트 15개, Ruff와 diff check를 통합 결과에서 다시 확인했다. DB 기반 Run API 및 운영 backend Run은 이번 통합 검증에서 실행하지 않았다. 공유 DB fixture의 FK 차단은 재시도하지 않았다.
- 운영 smoke는 **미검증**이다. 인증된 기존 Run 경로 부재 및 미배포 상태라 실제 backend agy 호출, 상태 저장 권한, 대화 재개/표식 회수, CLI를 통한 MCP 호출을 입증하지 못했다.
- 디자인 작업은 **실행하지 않음**. 승인 시 worker 요청은 `engine="antigravity"`, `model="gemini-3.8-flash-high"`, `effort="high"`로 명시하며 `auto`는 사용하지 않는다.
- 다른 세션의 미커밋 변경은 통합 커밋에 포함하지 않았다. 해당 변경은 원래 작업 트리에 보존되어 있다.
