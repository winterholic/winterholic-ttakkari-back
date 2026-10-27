# Muse MCP 바인딩 3종 수정 (envelope·탐색가드·인자 provenance)

## 범위와 판정

부모 지시의 코드 구현 단계로, 기존 분석을 반복하지 않고 3개 연관 변경만
최신 main(`c70bce7b`, origin/main) 기반 격리 worktree에 구현했다.
게임은 범위 제외. push/배포는 하지 않았으며(부모가 독립검증 후 진행),
기존 worktree와 타 작업 dirty는 건드리지 않았다. 작업 위치:
`/Users/winterholic/development/ttk-muse-bindingfix-20261028` (detached HEAD).

이 기록의 검증 성공을 실제 Muse 응답 검증으로 간주하지 않는다.
원시 MSP→앱 경계 바깥(provider 내부)의 인자 손실 원인은 여전히 미확정이며,
아래 진단은 그 원인을 확정하는 증거가 아니다.

적용 규칙: 해당 repo에 AGENTS.md가 없어(루트·2단계·워크스페이스 확인)
프로젝트 관례와 지시 스킬로 진행했다. `read_skill`은 수정 대상 가드 자체에
막혀(`Muse tool is not yet admitted: read_skill`) SKILL.md 원문을 직접 읽었다.
systematic-debugging(2회차 추측 금지·증거 대조), dev-experts(프로젝트 규칙 우선·좁은 검증),
TDD(red 선행, `git stash`로 integration 경계 red 확인), working-discipline(계약 ripple·소리 없는 실패 경계)을 따랐다.

## 관측 (재현)

- 부모·검증자 증거와 동일한 직접 재현. 수정 전:
  `_mcp_binding_error('mcp__ttakkari.list_workers', {}, 'tool unavailable: unknown tool ...', True)` → `False`.
- `read_session 74ce52cc` 과거 관측(dotted tool ID·`input_keys=[]`·`args={}`)은
  앱 어댑터 결과이며, 최초 유실 경계는 이 변경으로도 미확정이다.

## 변경1: tool-unavailable envelope 좁은 인식 (커밋 `cb12f49b`)

- `src/ttakkari/agents/muse.py` `_mcp_binding_error`에 분기 2줄 추가.
  출력이 정확히 `tool unavailable: unknown tool \`id\`` 로 열리고,
  실패 상태이며, 보고된 id가 호출 full name 또는 exact leaf와 일치할 때만 바인딩 오류로 본다.
- 제외(테스트로 고정): 다른 도구 보고·유사접두어(`spawn_worker_v2`)·구분자 정규화 가정 없음·
  envelope/보고id 대소문자 변형·성공 인용(문서)·일반 도구 오류·문두가 아닌 위치·근사 문구·
  비MCP 도구·호출과 다른 MCP 도구.
- streak 정의 보존: matcher만 건드렸고, 2연속·MCP 성공 리셋·비MCP 성공 무시 로직은 그대로다.
  성공 비MCP 호출을 MCP 회복 증거로 쓰지 않는다.

## 변경2: 읽기 전용 탐색 가드 통합 (cherry-pick `e0992eb9` ← `fe5cc734`)

- 최신 main ancestry를 먼저 확인: `fe5cc734`은 origin/main에 없어 cherry-pick했고,
  재구현하지 않았다. 부모 커밋(`8f883c8a`)→`c70bce7` 사이 해당 3파일 drift 없음.
- 내용은 기존 커밋 그대로: `tool_search`·`read_skill` 2종만 허용 집합에 추가(읽기 전용, 호스트 영향 없음).
  정책 약화·전역 allow·보호경로 우회 없음. 소유자 회귀 주장(14 passed)은 아래에서 직접 재검증했다.

## 변경3: 인자 provenance 최소 메타데이터 (커밋 `65f12341`)

- 경계 손실: `item.get('args') or '{}'` 파싱은 인자 키 누락·명시적 `{}`·빈 문자열을 모두 `input={}`로 만든다.
- `tool.call`에 optional `input_provenance {args_present, args_type, args_parsed}` 1키 추가(프로젝트 `input_*` 관례).
  어댑터에서 파싱하는 MSP item 기준으로 계산하며, 값·raw 문자열·프롬프트·인증정보는 절대 싣지 않는다.
- `input` 계산식은 바이트 동일 유지: 임의 복원·placeholder 인자 실행 없음.
- `read_session` `mcp_tool_errors`가 같은 행에 echo. 접근범위·call_id 상관(최근접 선행 호출)·3건 bound·SQL은 그대로.
  레거시 행(키 없음)은 `None`이라 하위호환된다. `args_type`만 `scrub`·32자 bound.
- 경계 정리: started+completed 쌍은 started item 형태를, started 없는 completed는 completed item 형태를 싣는다.
  completed 미도착은 기존대로 행이 없다. bridge raw-log 추가 없이 어댑터 provenance만 쓴다.
- `docs/api-contract.md`의 `tool.call` 행에 optional 키 1줄 반영(프론트 계약 ripple).

## 실행 검증

단위 red/green (공유 venv, `PYTHONPATH=src`):

```text
# 변경1 red → green
pytest -q unit_tests/test_muse_mcp_binding.py
2 failed, 8 passed → 10 passed
# 변경3 red → green (어댑터 8 + 진단 3)
pytest -q unit_tests/test_muse_args_provenance.py
8 failed(KeyError) → 11 passed
# 변경3 진단 red 확인용: diagnostics.py만 stash 후 collection error, pop 후 green
# 변경2 재검증 (소유자 14 passed 주장을 현 트리에서 재실행)
pytest -q unit_tests/test_muse_hook_admission.py unit_tests/test_muse_mcp_binding.py
17 passed
decision() 직접 호출 5건: tool_search·read_skill 허용, monitor·subagent_spawn 거부, spawn_worker agent 거부
```

좁은 회귀·필수 checks:

```text
pytest -q unit_tests/test_muse_args_provenance.py ..._binding.py ..._admission.py \
  ..._adapter.py ..._guard.py ..._protocol.py
57 passed + 1 failed(아래 환경 실패)
ruff check <변경 7파일>
All checks passed!
git diff --check
exit 0
py_compile tests/test_mcp_error_diagnostics.py unit_tests/test_muse_args_provenance.py
COMPILE-OK
```

환경 실패(코드 실패 아님, 정확히 기록):

- `test_muse_adapter.py::...test_prompt_and_mcp_material_are_private_and_cleaned`:
  `PermissionError: ... '/Users/winterholic/.config/muse/settings.json'` (sandbox stat 거부).
  손대지 않은 기준 트리(`ttakkari-muse-mcp-final-20261028`, c70bce7)에서도 동일 실패로 대조해
  기존 환경 실패임을 확인했다. 기준 트리는 `git status` clean 유지.
- PG 통합(`tests/test_mcp_error_diagnostics.py` 기존+신규, `tests/test_muse_mcp_recovery.py` 등):
  `PermissionError: [Errno 1] Operation not permitted` (sandbox loopback connect 거부).
  1회 probe 후 우회 시도 없이 중단. manager/stdout-pump 회귀와 신규 provenance echo
  통합 테스트(`test_read_session_echoes_args_provenance_and_keeps_legacy_rows_null`)는
  부모 독립검증 환경에서 실행해야 한다. `.muse-runtime` node 검사는 변경 경계 밖이라 생략.

## 남은 일 (부모)

독립검증 → push/배포 → 실제 Muse 응답·MCP 도구 검색·호출·동일 세션 resume 각각 실행검증.
검증 명령 후보:

```sh
PYTHONPATH=src:tests <venv>/bin/python -m pytest -q \
  tests/test_mcp_error_diagnostics.py tests/test_muse_mcp_recovery.py \
  tests/test_orchestrator_muse.py tests/test_agent_scope_auth.py \
  unit_tests/test_muse_args_provenance.py unit_tests/test_muse_mcp_binding.py \
  unit_tests/test_muse_hook_admission.py --tb=short
```
