# ADR 0002. 에이전트 엔진은 CLI 헤드리스를 지시 1회당 프로세스 1개로 실행

- 상태: 채택 (2026-10-09)
- 결정자: 사용자(Claude·Codex 둘 다, Orca 참고), 방식은 에이전트 제안

## 결정
- Claude: `claude -p <prompt> --output-format stream-json --verbose [--resume <session_id>]`
- Codex: `codex exec --json ... [-C dir] -- <prompt>`, 후속은 `codex exec resume ... <thread_id> -- <prompt>`
- 두 엔진 출력을 `agents/` 어댑터에서 공통 이벤트(`docs/api-contract.md`)로 바꾼다.
- stdin 은 닫는다(`codex exec` 가 stdin 을 기다리는 문제 실측).
- 로그인된 구독 인증을 그대로 쓴다. API 키가 필요 없다.

## 대안
- Orca 방식(Claude Agent SDK + codex app-server JSON-RPC 상주 프로세스): 중간 승인·스트리밍 부분 메시지 등 표현력이 높다. 대신 프로토콜 상태 관리가 크고 Python 에는 Node SDK 가 없다. 승인 흐름이 필요해지면(Phase 2) 이 방향을 다시 본다.
- PTY 로 대화형 CLI 를 띄우고 화면을 긁기: 모바일에서 구조화된 로그·Artifact 연결이 어렵다.

## 결과와 주의
- 사용자 전역 설정(CLAUDE.md, 스킬, hook)이 headless 실행에도 적용된다. 전역 Stop hook 이 있으면 Run 마다 추가 턴이 생긴다. `TTAKKARI_CLAUDE_SETTING_SOURCES=project,local` 로 사용자 설정을 뺄 수 있지만 그러면 전역 CLAUDE.md·스킬도 빠진다(실측). 자식 프로세스에는 `TTAKKARI_RUN_ID` 가 들어가므로 사용자 hook 에서 이 값으로 건너뛰게 하는 쪽이 낫다.
- Codex 는 파일을 셸로 고치기도 해서 도구 이벤트만으로 변경 파일을 다 못 잡는다. git 워크스페이스에서는 체크포인트 대비 diff 로 변경 파일을 모은다.
