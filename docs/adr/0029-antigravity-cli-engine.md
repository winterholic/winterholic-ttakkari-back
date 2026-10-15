# ADR 0029: Antigravity CLI 작업 엔진

- 상태: 구현 중
- 날짜: 2026-10-15

## 결정

Ttakkari는 유료 SDK 대신 사용자가 로그인한 공식 단일 `agy` CLI를 headless NDJSON 어댑터로 실행한다. 초기 지원 모델은 계정의 `agy models` 출력으로 확인된 Gemini 3.8 Flash low/medium/high이며, 기본 worker 배정은 `gemini-3.8-flash-high` / `high`다. Claude·Codex 계정의 자동 순환에는 넣지 않는다. 명시적인 `engine="antigravity"` 선택만 허용한다. 엔진 사용량은 공식 CLI에서 관측할 수 없으므로 unknown으로 유지하고 0% 또는 무한 여유로 해석하지 않는다.

기존 모델 정책과 상위 worker 설정은 Antigravity Claude/GPT 모델을 선택하지 않는다. 엔진 계정은 로그인된 단일 CLI를 나타내며 `config_dir` 기반 추가 계정은 지원하지 않는다.

## 실행 프로토콜

- 실제 `stream-json` NDJSON의 `init`, `step_update`, `result`, 도구 입력/출력, 상태, token usage를 Ttakkari Run 이벤트로 정규화한다.
- 재개 시 전역 최신 대화 옵션을 쓰지 않고 `--conversation <engine_session_id>`를 전달한다.
- `--sandbox`와 `--mode accept-edits`를 사용하며 `--dangerously-skip-permissions`는 사용하지 않는다. timeout·취소에서 프로세스 그룹과 임시 파일을 정리한다.
- MCP 설정은 세션 전용 임시 프로젝트의 `.agents/mcp_config.json`에 0600으로 만들고 프로세스 종료·초기화 후 제거한다. 토큰은 이벤트·로그로 기록하지 않는다.

## 검증 한계와 배포 조건

2026-10-15 후속 검증에서는 설치된 CLI가 1.3.5였고, 정상 `agy models` 호출이 Gemini 3.8 Flash low/medium/high를 반환했다. 이전 1.2.10 CLI 실측(high 호출·파일 작성·workspace 밖 차단)은 그 날짜의 별도 증거로 유지한다. 현재 실행 환경에서 실제 headless 요청은 CLI가 `~/.gemini/antigravity-cli` 아래 conversation DB·brain·cache를 만들려다 `operation not permitted`를 기록했다. `HOME`을 임시 경로로 바꾸면 이 쓰기는 가능했지만 기존 로그인이 없어 모델 조회가 `Please sign in`으로 끝났다. 인증 자료를 복사하지 않았다. 따라서 독립 CLI 프로세스의 세션 저장과 연속성은 이 실행 환경에서 재검증하지 못했다.

이전 실제 `--conversation` 시도는 요청 대화를 찾지 못했다는 경고 뒤 새 ID를 반환했다. 후속 보강은 이미 저장한 `engine_session_id`와 CLI `init.conversation_id`가 다르면 새 세션으로 바꾸지 않고 오류로 중단한다. 응답 ID 불일치 회귀 테스트는 실패 후 수정되어 통과했지만, 무작위 표식의 독립 프로세스 회수는 아직 검증되지 않았다.

공식 문서는 workspace MCP 설정 위치를 활성 프로젝트의 `.agents/mcp_config.json`으로 지정한다. 실제 CLI 실행 로그는 해당 임시 프로젝트의 `safe_fixture` 서버를 연결하려 했음을 보였고, `init.tools`에는 MCP별 이름 대신 일반 `call_mcp_tool` dispatcher가 있었다. fixture는 `server/discover`를 처리하지 못해 1분 연결 대기 후 취소되었으므로 CLI를 통한 MCP 도구 호출 성공으로 세지 않는다. 한편 제품의 `mcp` 2.3.0 stdio 서버에 직접 modern `server/discover`와 `tools/list`를 보낸 검증은 성공해 도구 43개(작업 세션용 `share_file`, `notify_user` 포함)를 반환했다. 읽기 도구 호출도 서버 응답까지 도달했으나, 의도적으로 닫힌 loopback API 주소와 fixture token을 사용해 `isError=true`를 받았다. 이는 실제 로그인 세션의 API 접근·보고 성공 증거가 아니다.

`--sandbox`와 `--mode accept-edits`는 유지했고 `--dangerously-skip-permissions`는 사용하지 않았다. OS 실행 격리가 Antigravity 앱 저장소 쓰기를 막아 실제 Run→worker→MCP 접근/보고, 취소 후 자식 정리, 다중 허용 root/민감 경로 전수 fixture를 완료하지 못했다. 코드에는 요청 대화 ID 불일치 시 fail-closed 처리가 추가됐다. 추가로 CLI가 stdout에 결과 없이 exit 0을 내면 Run을 실패로 닫고 stderr 진단을 보존한다. 이는 성공 오판을 막지만, CLI가 모델 응답 전에 앱 저장소 쓰기에서 막히는 실행 경계를 해결하지 않는다. 이 경계 검증 전에는 넓은 프로젝트에 worker를 배정하거나 이 ADR을 승인 상태로 전환하지 않는다.

공식 경로 조사(2026-10-15): 설치된 `agy 1.3.5 --help`에는 상태 root override가 없다. `--log-file`은 로그만, `--project`는 project ID/name을 받는다. 공식 문서는 설정을 `~/.gemini/antigravity-cli/settings.json`, `--continue` 캐시를 `~/.gemini/antigravity-cli/cache/last_conversations.json`에 둔다고 명시한다. 저장 위치 override는 공개 저장소의 아직 열린 [AGY_HOME 요청 #155](https://github.com/google-antigravity/antigravity-cli/issues/155)에서도 기능 요청 상태다. `HOME` 변경·인증 복사·symlink·sandbox 완화는 허용된 경로가 아니며 수행하지 않았다. 따라서 현 실행 주체에서 `~/.gemini/antigravity-cli`에 필요한 기록 권한을 받을 수 있는 별도 공식 지원 경로는 확인되지 않았다.

배포 전에는 해당 앱 저장소 쓰기 권한이 이미 허용된 정상 실행 환경에서만 무작위 표식 기반 대화 재개, 실제 Ttakkari MCP tool 호출·결과와 worker 보고, stop/자식 프로세스 정리, 허용/금지 파일 fixture 및 오류 cleanup을 확인해야 한다. 이 권한 없이는 실제 CLI 검증은 미실행으로 유지한다.
