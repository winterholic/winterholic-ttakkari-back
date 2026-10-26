# Research 배치 시간 예산과 취소 정리

2026-10-26, 리드 Codex 직접 실행. 원목표는 active이며 PMS7.3 일부다.
시작 제품은 `1b23d566a4b8d15813a9e1425adbbe705aa5e8a4`다.
기존105건/시각 fixture9건과 A~D/운영575필드 증거를 그대로 재사용했다.

## 배포와 범위

기존 누적예약 `f257d939-3aea-4a2d-b4be-0dff66e642be`를 이번 Run에서
1회 상태 확인하고 `ops_deploy_log(summary)`를 읽었다. 후보1b23d566은
`testing`, error=null, 오류 section0이었다. 로그는9줄/533bytes로 당시7%였다.
이후 대기·추가 폴링을 하지 않았다. testing을 활성화 성공으로 보지 않는다.

계약 `refactor-v2.md:424`는 max_total_seconds 기본300/최대3600을 요구한다.
planner는 초과 상한만 검사했고 runner는 그 값을 읽지 않았다. 배포를 기다리는
동안 이 독립적인 PMS7.3 실행 예산 공백부터 교정했다.

## 반례와 변경

- 배치 전체가 하나의 monotonic deadline을 공유한다. 후속 queued Run은 남은
  예산이 없으면 어댑터를 호출하지 않고 budget_exceeded로 취소한다.
- 비동기 실행에 deadline을 전달한다. 어댑터가 CancelledError를 처리한 뒤
  success를 반환해도 예산 초과는 cancelled로 저장한다. 반환된 raw/env/정리
  로그는 남기며 성공 측정을 발행하지 않는다. 먼저 끝난 Run의 증거는 바꾸지 않는다.
- native/blocking 코드가 이벤트 루프를 막으면 asyncio가 선점할 수 없다.
  반환 후 monotonic 시각도 대조해 늦은 결과를 성공으로 인정하지 않는다.
- 명시적인 사용자 취소를 먼저 저장한 경우 그 상태·사유를 우선한다.
- 0/음수/bool/문자열/null/3600초 초과/비유한 숫자는 공통 파서에서 거부한다.
  검증 API는 valid=false, 직접 저장된 잘못된 예산의 배치는 budget_invalid로
  queued를 닫는다. NaN/Infinity는 PostgreSQL JSON에 저장하지 않고 파서 경계에서 검사한다.
- GPU start의 to_thread는 coroutine 취소로 스레드를 중단할 수 없다. start와
  launch identity 읽기를 shield하고, 취소를 받은 부모도 그 결과를 관찰한 다음
  기존 launch 검증 경로로 자신이 소유한 자원만 정리한다. 정리 결과를 반환한다.
- TTS는 취소한 provider/STT 자식의 teardown을 await한다. 합성 전/중 취소는
  임시 디렉터리를 지우고 실행 lock을 반환한다. 이미 만든 오디오가 있으면
  삭제하지 않고 취소 결과 raw/env에 참조를 남긴다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py -k 'budget' -q --timeout=5 --tb=short
교정 전: 3 failed, 15 deselected in 6.77s
교정 후: 3 passed, 15 deselected in 0.90s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_adapters.py -k 'task_cancel_during_start or cancel_awaits_synthesis' -q --timeout=8 --tb=short
교정 전: 2 failed, 49 deselected in 0.19s
교정 후: 2 passed, 49 deselected in 0.10s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py -k 'blocking_adapter_late' -q --timeout=5 --tb=short
추가 반례: 1 failed, 26 deselected in 0.84s (succeeded != cancelled)
```

첫 RED의 세 번째 사례는 watchdog cancel을 어댑터가 삼켜 두 번째 실행도
기다리면서 pytest5초 timeout이 났다. 이후 fixture는 두 번째 호출을 즉시
반환하도록 제한했다. 기능 실패와 watchdog 정리를 구분한다. blocking 반례의
sleep은 선점 불가 입력을 의도적으로 만들며 ASYNC251 예외 사유를 해당 줄에 적었다.

## 실행 검증

모든 DB seed는 주입된 TTAKKARI_TEST_DB의 DB/user와 current_database/current_user를
대조하며 운영DB ttakkari를 거부한다. 합성 ProbeAdapter/host autospec/provider
fixture다. 실제 GPU/TTS 벤치를 실행한 검증으로 확대하지 않는다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py tests/test_research_adapters.py -q --timeout=15 --tb=short
79 passed in 2.51s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_scenario_b_artifacts.py tests/test_research_v2_core.py tests/test_research_v2_api_contract.py tests/test_research_v2_combination_acceptance.py -q --timeout=30 --tb=short
90 passed in 6.91s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py -k 'plan_validation_rejects' tests/test_research_adapters.py -k 'plan_validation_rejects or cancel_during_synthesis_stops' -q --timeout=15 --tb=short
8 passed, 77 deselected in 1.15s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py tests/test_research_adapters.py -k 'budget or task_cancel_during_start or cancel_awaits_synthesis or cancel_during_synthesis_stops' -q --timeout=15 --tb=short
23 passed, 63 deselected in 2.02s
$ git diff --check
exit0
```

79/90/8/23은 실행 묶음별 값이며 겹치는 검사를 더하지 않는다. 마지막23건은
API7개(거대 정수 포함), 잘못된 저장 예산8개, deadline4개, 정리 관련4개다.
마지막 수정의 wall_seconds는 예산 초과 시 정리까지 포함한 실제 elapsed를 쓴다.

ruff 전체는 기존 진단이 있다. HEAD 원문과 현재 파일의 code/message별 Counter를
대조해 새 진단0을 확인했다. baseline/current는 runner0/0, planner3/3,
llm13/13, tts11/11, lifecycle0/0, adapters-test5/5다. 미사용 import를 주입한
부정 대조에서는 실제 추가 진단을 검출했다. 전체 lint PASS로 표기하지 않는다.

### 필수 push 검사에서 확인한 lint 차단과 후속

위 수치 대조만으로는 필수 gate를 대체할 수 없었다. 첫 커밋2a0e5bce의
실제 pre-push는 ruff32건으로 push를 거절했다. 당시 계약84PASS13.23초,
변경파일86PASS2.77초는 통과했다. 우회 옵션은 사용하지 않았다.

해당6개 파일 안에서 미사용 import/정렬8건, 공유 클래스 목록의 ClassVar 표시,
grid 인덱스의 enumerate 전환, 예외 경계의 traceback 로깅을 정리했다.
기존 실패·fallback 반환값은 유지하고 조용한 pass는 관측 로그로 바꿨다.
전역 lint 규칙 변경이나 파일 단위 ignore는 추가하지 않았다.

```text
$ .venv/bin/ruff check src/ttakkari/research/runner.py src/ttakkari/research/planner.py src/ttakkari/research/adapters/llm_inference.py src/ttakkari/research/adapters/tts_synthesis.py tests/test_research_runner_lifecycle.py tests/test_research_adapters.py
All checks passed!
```

최종 source hash는 동명JSON에 다시 기록한다. 최초32건대조는 위 과거 상태이며,
후속 lint정리 후의 통과 결과와 구분한다.

## 통합과 남은 조건

fresh fetch에서 main이 독립 계정쿼터 커밋dd974867 하나 앞선 것을 확인했다.
연구 파일과 겹치지 않아 research-v2에서 origin/main을 FF했다. 연구 변경을
main 체크아웃에서 직접 편집하지 않았다. 누적 source/검증 기록을 커밋하고
필수 pre-push 검사를 유지한 뒤 main/research-v2를 atomic FFpush한다.

시간 제한은 취소와 늦은 결과 판정이며 OS의 강제 선점이 아니다. 이미 시작한
host start/안전 정리는 시간 한도 뒤에도 끝날 때까지 기다린다. 비협조적인
native 작업의 강제 종료, 실제 메모리 예산, owner crash/checkpoint의 장기복구,
연구별 durable ownership, 운영 Metal layer/동일A launch, 인증 운영 UI·원문UX10,
legacy92·PMS7.x 전체 조건 및 누적 배포 최종 gate는 아직 미완료다.
