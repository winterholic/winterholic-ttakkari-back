# Research Run 재실행·취소 경계 교정

2026-10-26 리드 Codex가 기존 영속목표 revision70을 복원했다. 전체 목표는 active다.
기존 A/B/C/D 인수와 운영575필드 보존은 재실행하지 않았다.

## 배포 관측

이번 Run에서 `backend_deploy_status(0d1dfeb0-3f8c-465c-b0ac-a452620b8886)`를
한 번 조회했다. 후보32ec23736bb43ba55dd3f0a4a36331477aa8048a는 testing,
skip_tests=false, error=null이었다. `ops_deploy_log(summary)`는21줄/1479bytes,
23% 진행, 오류 section없음이었다. 이후 상태를 반복 조회하지 않았다.
`curl -fsS http://127.0.0.1:8787/api/version`은 활성
fcd3dd161406a12e32ed9a9c9a65fb5e58c207b0/PID87194를 반환했다.
설치exit127의 새 상세로그는 아직 얻지 못했으며 원인 해결을 주장하지 않는다.
새 parser 활성 전이므로 host GPU status나 A 벤치는 재실행하지 않았다.

## PMS7.3에서 확인한 실제 결함

원문 완료 조건은 owner crash·generation drain·취소·복구와 실측이다.
현재 `generation.enter_accepted_work('research')`는 프로세스 내 추적이며
에이전트 RuntimeLease와 다르다. 이 교정은 그 전체 조건의 일부다.

실제 PostgreSQL 테스트에서 아래를 재현했다. 어댑터는 명시적인 합성
ProbeAdapter이며 실제 GPU/TTS 실행이나 성능 측정이 아니다.

- cancelled/failed/succeeded Run을 다시 넘기면 어댑터를 재호출하고 원본을 덮었다.
- 실행 중 같은 Run을 다시 호출하면 두 번째 어댑터 호출이 생기고 취소 Event가 바뀌었다.
- 배치 첫 Run 취소 후 나머지 Run은 queued로 남았다.
- 취소 요청 후 어댑터가 늦게 succeeded를 반환하면 저장된 취소가 succeeded로 바뀌었다.

테스트 준비 중 Experiment의 잘못된 planned/draft 상태는 DB check constraint가
거부했다. fixture를 허용된 queued로 고친 뒤 아래 기능 반례를 확인했다.
취소 결과 fixture의 필수 environment_snapshot 누락도 준비 오류로 분리해 고쳤다.
이 준비 실패를 제품 결함 재현으로 계산하지 않는다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py -q --timeout=20 --tb=short
실제 기능 RED: terminal3건 덮어쓰기/duplicate wait timeout (2.88s 실행의 나머지1건은 fixture 오류)
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py::test_cancelled_batch_closes_unstarted_runs -q --timeout=20 --tb=short
1 failed in 0.60s: ['cancelled', 'queued'] != ['cancelled', 'cancelled']
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py::test_cancel_request_survives_late_success_with_raw_evidence tests/test_research_runner_lifecycle.py::test_simultaneous_claims_execute_adapter_once -q --timeout=20 --tb=short
1 failed, 1 passed in 0.74s: succeeded != cancelled
```

## 최소 변경

Run 행을 PostgreSQL row lock으로 읽고 queued만 실행권을 얻는다. 다른 호출은
저장된 상태를 반환하므로 원본과 살아 있는 소유자의 Event를 바꾸지 않는다.
취소 요청도 같은 행 lock으로 직렬화한다. 어댑터 반환 후 DB 취소 상태를 다시
읽어 원본 raw/env와 취소 사유를 함께 보존한다. 아티팩트 복사 중 받아들인 취소는
등록 트랜잭션을 rollback하고 이번 복사만 보상 정리한다. 먼저 저장한 측정은 남는다.
배치가 취소로 멈추면 해당 배치·실험의 queued Run만 cancelled로 닫는다.

schema/API/실행기 명령/예산/세대 배포 정책은 바꾸지 않았다. 기존 동일한
Configuration으로 새 Run을 만드는 정상 반복·재실행은 유지한다. 이번에는
과거 Run ID 자체를 재실행하지 않도록 제한했다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py tests/test_scenario_b_artifacts.py tests/test_research_v2_core.py tests/test_research_v2_api_contract.py tests/test_research_v2_combination_acceptance.py -q --timeout=30 --tb=short
98 passed in 7.01s
$ .venv/bin/ruff check src/ttakkari/research/runner.py tests/test_research_runner_lifecycle.py
All checks passed!
$ git diff --check
exit0
```

새 테스트8건은 각 seed에서 current_database/current_user를 주입 URL의 DB·명시user와
대조하고 운영DB ttakkari를 거부한다. 네 동시 호출의 단일 어댑터 실행,
살아 있는 Event 보존, 취소 후 raw/env 보존·fresh DB조회, 복사 중 취소의
측정 보존/신규 복사 제거/원본 파일 유지도 확인했다. 기존 artifact 예외·취소와
API/조합/반복 검사를 묶어98건을 실행했다. 별도 독립 검증자·운영 실행 증거는 아니다.

## 미완료와 다음 실행

설치exit127 상세원인, 새 누적 전체배포gate, 동일 A launch의 Metal layer placement,
운영 인증 UI·원문UX10, legacy92 연구, 장시간 owner crash/drain/checkpoint 및
PMS7.x의 기존 원조건은 남아 있다. R4/7/12~16이나 PMS7.3을 done으로 바꾸지 않는다.
이번 교정과 기존32ec/5e/151을 누적한 clean SHA를 main에 FFpush하고 기존 배포
컨트롤러에 예약한다. 예약 결과는 활성화 성공과 구분한다. 다음 Run은 그 예약을
한 번 확인해 FAIL 상세로그 교정 또는 활성version→동일 A launch 배치 인수로 이어간다.
