# Muse MCP 반복 실패 복구

## 범위와 판정

사용자는 Muse 기획 세션의 새 작업 세션 생성 실패를 고치라고 요청했다.
이 변경은 **반복된 MCP 바인딩 실패에서 빠져나오는 복구 경로**다.
외부 Muse CLI/모델이 인자를 빈 객체로 만든 내부 원인을 고쳤다는 뜻은 아니다.
원시 실패 호출 원문과 운영 Muse의 수정 후 실행은 아직 미검증이다.

기존 보고의 `title/prompt Field required, input_value={}`와 `unknown tool`을
재현 입력으로 사용했다. 이전 보고가 말한 14회 전체의 원문은 이번에 확인하지 못했다.
현재 Codex 기획에서는 인자가 있는 recall/manage_work/read_session 호출이 정상 응답했다.

## 관측

- MCP `host_operation_start(kind="runtime_report")`, receipt
  `ebf16b88-f8f5-4073-9eb3-107b8c8fba43`: 운영 active revision은
  `78c4a2e7cde02536327eefd7184b64826d1726aa`, generation
  `03dfad0a-cb92-4a95-a113-7e64be8b77db`, local API다.
- `muse --version`: `1.4.13-R6996.1`. source SDK pin은 `1.4.10`.
  버전 차이는 관측이며 인자 손실 원인으로 확정하지 않았다. 해당 bridge/adapter는
  운영 revision과 작업 시작 origin/main `d804618a` 사이 변경이 없었다.
- `read_session`의 원 게임 세션 마지막 실패는
  `QueuePool limit of size 5 overflow 3 reached ... timeout 30.00`이고 이후 요청 2개는 cancelled였다.
  취소 카드 재시도 버튼 front `285800f`는 MCP 인자 손실의 해결 증거가 아니다.
- ops_search_logs(query="spawn_worker", since_hours=6)는 0건이었다.
  tool 오류 전체가 없었다는 뜻으로 해석하지 않는다.
- Codex 기획 command 환경에서 기본 localhost production DB의 read-only 연결은
  `InvalidPasswordError`로 인증 단계에서 실패했다. 비밀을 읽거나 다른 경로로 우회하지 않았다.
  원시 RunEvent 조회는 미실행이며, 운영 DB가 고장났다는 증거도 아니다.

## 변경

- Muse의 MCP 도구에서 빈 객체 validation 오류 또는 실제 실패 상태의 unknown tool이
  서로 다른 호출로 연속 2회 나타나면 바인딩 실패로 기록한다. **연속된 MCP 결과**가 기준이다.
  완료 revision 재전달은 중복 집계하지 않는다. 정상 MCP 응답·다른 MCP 오류는 집계를 초기화한다.
  성공한 file/shell 호출은 MCP 바인딩 회복을 증명하지 않으므로 초기화하지 않는다.
  문서 예시·일반 업무 오류는 전환 조건이 아니다.
- stdout pump는 이 시점의 호출/결과/오류를 저장하고 attempt를 종료한다.
  후속 모델의 성공 문구로 실패를 덮지 않는다.
- 기존 `_attempt_handoff`로 변경 파일·도구 결과·원 요청을 보존하고 허용된 다음 provider로 이어간다.
  누락 인자를 임의로 복원하거나 spawn을 직접 재실행하지 않는다.
- 고장 Muse는 해당 Run의 usage에 제외 정보로 보존되어 대기·재시작 후 재선택하지 않는다.
  quota 소진으로 기록하지 않는다. strict engine이면 다른 provider로 바꾸지 않는다.
  네이티브가 한도 대기 중이면 인계를 보존해 기존 초기화 대기로 들어간다.

## 실행 검증

TDD red:

```text
pytest -q unit_tests/test_muse_mcp_recovery.py  # 후속 파일명 test_muse_mcp_binding.py
2 failed, 3 passed
pytest -q tests/test_muse_mcp_recovery.py
1 failed: 기존 Run은 두 MCP 실패 후에도 engine=muse, succeeded
```

수정 후:

```sh
PYTHONPATH=src ../projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q \
  unit_tests/test_muse_mcp_binding.py unit_tests/test_muse_adapter.py \
  unit_tests/test_muse_guard.py unit_tests/test_muse_protocol.py \
  tests/test_muse_mcp_recovery.py tests/test_orchestrator_muse.py tests/test_codex_capacity.py --tb=short
# 50 passed in 5.99s
node --test .muse-runtime/contract.test.mjs .muse-runtime/hook-settings.test.mjs
# 8 passed, 0 failed
ruff check src/ttakkari/agents/muse.py src/ttakkari/runs/manager.py \
  unit_tests/test_muse_mcp_binding.py tests/test_muse_mcp_recovery.py
# All checks passed!
git diff --check
# exit 0
```

통합 검사는 실제 stdout pump·process 종료·provider 전환·DB 저장 경로를 실행하며,
Muse 모델 대신 scripted process와 기존 fake native agent를 쓴다. 격리 PostgreSQL을 자동 생성했다.
운영 모델 응답, 실제 spawn의 부작용 중복 방지, SDK/CLI 내부 바인딩 정상화를 보증하지 않는다.

## 새 세션 접수

현재 Codex 기획의 실제 `spawn_worker` 호출 두 건이 required 인자와 함께 수락됐다.
생성 직후 상태는 queued이며 실제 모델 배정/작업 완료와 구분한다.

- 게임 이어가기: worker `2391716e-ff03-43b7-af4b-ca9ff96c6f90`,
  Run `3374e15f-72ea-4bd6-ba32-58bf64e0d6d1`, auto/general.
- 고장·복구 독립 진단: worker `fac27f5e-0e69-4109-a66e-17683f37218f`,
  Run `fe5edca0-c0cb-4eaa-91ae-727965cbac61`, auto/verification.
  반례·진단을 보고하면 기획이 필요한 교정을 실행한다.

운영 배포 예약의 결과는 MCP 예약 receipt와 manage_work에 기록한다.
예약 수락은 배포 성공이 아니며 원시 Muse 바인딩 원인은 계속 미확정이다.

## 독립 검증 후속

검증자 `fac27f5e`의 두 보고를 대조했다. 일반 파일 읽기 성공 사이에 MCP 오류가 반복돼도
전환하는 동작은 위 정의에 맞는다. 별도 회귀 테스트로 의도를 고정했다.
`unknown tool spawn_worker`처럼 오류에 leaf 이름만 나오는 경우를 놓치는 반례는 채택해 수정했다.
정확한 leaf 또는 전체 이름만 일치시킨다. `spawn_worker_v2` 오류 뒤에 현재 도구명이 언급되는
문장은 전환 조건으로 삼지 않는다. 이 변경의 red는 `1 failed, 6 passed`였다.

통합 검사에 한도 대기 뒤 실제 같은 Run 재개와 네이티브 전환, API 취소 및 manager shutdown
flag가 바인딩 실패 후속 전환보다 우선하는 경계를 추가했다. 실제 서비스 재시작 검사는 아니다.

원인을 더 조사할 지원 경로로 `read_session`의 `turns[].mcp_tool_errors`를 추가했다.
기존 세션 scope 검사를 통과한 Run에 한해 최근 MCP 실패 3건을 보여 준다.
도구명·호출/결과 seq·인자 키(최대20개, 값 제외)·빈 객체 여부·저장된 오류 flag와
마스킹한 오류 발췌(최대1000자)다. 인자 손실의 원인이 무엇인지 해석하지 않는다.
다른 provider가 같은 로컬 call_id를 재사용할 수 있어 가장 가까운 선행 호출과 결합한다.
업무용 세션의 개인 세션 열람 차단과 비-MCP 로그·인자값·비밀값 미노출을 검사했다.
새 필드 누락과 재사용 call_id 오결합을 각각 red로 재현한 뒤 통과시켰다.

후속 선택 회귀 명령은 위 Python 검사 목록에 `tests/test_mcp_error_diagnostics.py`,
`tests/test_agent_scope_auth.py`를 더한다. 작업 트리에서 68 passed in 13.22s를 관측했다.
동시에 다른 작업의 OpenCode/Copilot 변경이 나타나 해당 파일은 수정·stage하지 않았으며,
최종 검증·push는 소유 변경만 커밋한 별도 clean checkout에서 실행한다.

기존 배포 `dd335cf9-9a09-4629-a176-566a219bc265`는 후속 턴의 단일 조회에서 `testing`이었다.
후속 누적 커밋 배포 예약과 원시 운영 호출 진단은 manage_work에서 이어간다.
