# Muse 리드 세션 도구 탐색 차단 해제

## 범위와 판정

사용자는 Muse 에이전트 사용 시 뜨는 해괴한 오류들을 고치라고 요청했다.
이 변경은 **PreToolUse 가드가 읽기 전용 탐색 도구를 막던 것**을 푼다.
MCP 바인딩 인자 손실(빈 객체) 자체의 수정이 아니다.

## 관측

- Muse 리드 세션에서 `tool_search` 호출이
  `[ttakkari-guard] Muse tool is not yet admitted: tool_search` 로 거부됐다.
  지연 MCP 도구(spawn_worker 등)가 목록에 없어 탐색 자체가 불가했다.
- `read_skill`도 같은 이유로 거부됐다. `write_todos`는 거부되지 않았다(하네스 내장).
- 리드 세션은 TTK_ROLE=orchestrator 로 MCP 서버를 받는다
  (runs/manager.py → mcp_server_spec). 발견만 되면 위임 호출은 기존 분기가 허용한다.

## 변경

- `src/ttakkari/security/muse_hook.py`: 허용 집합에
  `tool_search`, `read_skill` 추가. 둘 다 읽기 전용이며 호스트 영향이 없다.
  subagent/monitor/cron/goal/memory 등 나머지는 계속 fail-closed.
- `unit_tests/test_muse_hook_admission.py`: 허용 2종, 미허용 유지,
  spawn_worker 오케스트레이터 전용, 비정상 입력 차단.

## 검증

- ruff check 2파일 통과(트리 루트에서 실행).
- pytest unit_tests/test_muse_hook_admission.py +
  unit_tests/test_muse_mcp_binding.py: 14 passed.
- muse_hook.main() 직접 호출 5건 전수 일치(허용/거부·역할별).
- tests/test_guard.py 등은 conftest가 임시 PostgreSQL을 띄우는데
  이 Muse 샌드박스에서 PermissionError 로 막혀 미실행.
  guard.py·claude_hook.py는 손대지 않았다.

## 남은 일

- 운영 배포 후 Muse 리드 세션에서 tool_search →
  mcp__ttakkari__spawn_worker 실제 호출 확인.
- Codex 작업 세션 투입은 이 Muse 세션에 작업자 도구가 없어 미실행.
  다음 네이티브 턴에서 배포 예약 후 확인한다.
