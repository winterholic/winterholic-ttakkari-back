# 러너 배치 완료·취소 예외 보존 및 배포 차단 테스트 교정

2026-10-26. 기획 세션의 Codex Run10cf179a가 `Selected model is at capacity`
오류로 중단됐다. 영속목표 revision73은 이 엔진 오류를 blocker로 기록했다.
재개 시 HEADf7dc370c와 추가 lifecycle 테스트108행만 미커밋 상태임을 확인했다.
runner 제품 변경은 없었다. revision74에서 active로 복원하고 남은 교정부터
이어갔다. 사용자의 전체 목표나 작업 완료로 해석하지 않았다.

## 확인한 결함과 범위

이전 Run의 실제 PostgreSQL 반례는7건 중6FAIL/1PASS, 2.86초였다.
같은 RED를 다시 돌리지 않고 남은 코드를 교정했다. 어댑터는 합성 ProbeAdapter다.
DB는 각 seed에서 주입URL의 DB/명시user와 실제 current_database/current_user를
대조하고 운영DB ttakkari를 거부했다.

- 취소를 저장한 후 어댑터가 예외를 던지면 취소가 failed로 덮였다.
- 같은 배치를 다시 호출하면 기존 owner의 running Run을 넘어 다음 queued Run을 실행했다.
- 다른 Run이 queued/running인데 부분 배치를 마치면 Experiment가 completed로 표시됐다.
- 완료 배치를 재생하면 완료시각·분석시각을 다시 썼다. generation gate 거절 시에는
  과거 completed를 cancelled로 바꾸고 receipt도 덮었다.

현재 교정은 취소 예외에서 저장된 행을 row lock과 populate_existing으로 다시 읽는다.
단순 get/with_for_update만으로는 이미 로드된 ORM identity map 값이 남아, 첫
교정 실행은1FAIL/6PASS0.76초, row lock만 보강한 실행도1FAIL/6PASS0.83초였다.
실제 실패 출력의 failed!=cancelled를 보고 명시적 재로딩을 추가했다.
취소 사유와 후속 어댑터 예외 문구를 함께 보존하고 일반 예외는 failed로 남긴다.
시스템 CancelledError 처리도 같은 DB 재로딩을 사용한다.

배치에서는 새 queued 작업이 없는 재생을 시작하지 않고, 다른 owner가 running인
Run을 만나면 후속 작업을 가져가지 않는다. 전체 자식 Run이 terminal일 때만
완료시각과 자동 분석을 게시한다. pending이 있으면 기존 running/queued 상태와
미완료 표시를 유지한다. gate 거절은 요청된 실험·배치의 queued 행만 취소하며,
변경할 행이 없는 terminal 재생은 과거 receipt·시간·분석을 보존한다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py -k 'adapter_exception or duplicate_batch or partial_batch or completed_batch_replay' -q --timeout=15 --tb=short
7 passed, 8 deselected in 0.78s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_runner_lifecycle.py tests/test_scenario_b_artifacts.py tests/test_research_v2_core.py tests/test_research_v2_api_contract.py tests/test_research_v2_combination_acceptance.py -q --timeout=30 --tb=short
105 passed in 7.35s
$ .venv/bin/ruff check src/ttakkari/research/runner.py tests/test_research_runner_lifecycle.py unit_tests/test_opencode_adapter.py
All checks passed!
$ git diff --check
exit0
```

105건은 현재 runner 변경 뒤 실행한 통합 묶음이다. 이전 f7의98건에 새7개를
포함한다. 파일등록 실패·취소/측정 보존·API·실제CPU 조합 저장복원도 함께 확인했다.
별도 독립 검증자, 실GPU, 운영 UI, 장시간 owner-crash/checkpoint 인수가 아니다.

## 누적 배포 실패: OpenCode 시간 fixture

재개 후 최신 edcdc2e8-be3d-4029-84fc-9e0425469f84/f7dc370c 예약을1회 조회했다.
상태는failed였다. ops_deploy_log summary와74~95행을 직접 읽었다.

```text
FAILED unit_tests/test_opencode_adapter.py::OpencodeAdapterTests::test_error_event_and_usage_limit_detection
assertIsNotNone(self.state.limit_resets_at) → unexpectedly None
1 failed, 4863 passed, 13 skipped, 2 xfailed, 4961 subtests passed in 1199.32s
```

이 테스트는 `Oct 26th, 2026 3:00 PM`을 고정하면서 현재 시각을 사용했다.
profiles.reset_epoch_from은 현지 초기화 시각이 현재보다 미래이고 허용기간 안에
있을 때만 반환한다. 해당 현지 시각이 지난 뒤 None을 반환하는 운영 계약은
유지한다. 테스트만 현지14시/16시로 고정해 미래시각 정확값과 만료 None을 대조했다.
OpenCode 실제 CLI/계정 조회나 모델 응답을 실행한 것이 아니라 합성 error 이벤트
정규화 단위검사다. 운영 파서·쿼터·모델 라우팅은 변경하지 않았다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest --noconftest unit_tests/test_opencode_adapter.py::OpencodeAdapterTests::test_error_event_and_usage_limit_detection -q --tb=short
교정 전: 1 failed in 0.20s (unexpectedly None)
$ PYTHONPATH=src .venv/bin/python -m pytest --noconftest unit_tests/test_opencode_adapter.py -q --tb=short
교정 후: 9 passed, 2 subtests passed in 0.20s
```

동일한 absolute-reset fixture를 tests/unit_tests에서 검색했다. 같은 고정시각에
미래 reset을 단언하는 곳은 이 한 곳이었다. qwen_emergency의 과거 문구는
is_usage_limit 분류만 검증하므로 바꾸지 않았다.

## 운영과 남은 목표

이번 curl GET /api/version은 fcd3dd161406a12e32ed9a9c9a65fb5e58c207b0,
PID87194였다. 새 parser 미활성이므로 host_gpu_status/A 벤치와 기존 A/B/C/D,
575필드 보존·유효 UI 검사를 반복하지 않았다.

중단 직전 조회한0d1d/32ec는testing59%·오류구간없음이었고,
그때 edcdc2e8/f7는requested였다. 이 두 관측은 과거 상태로 보존한다.
현재 edcdc2e8의 최종실패는 위 OpenCode 테스트다. 이 gate에서 설치exit127이
관찰되지 않은 사실을 설치오류의 원인 해결로 확대하지 않는다.

R4/7/12~16과 PMS7.3 전체, 장기 소유권/복구·실메모리 예산, 같은 A launch의
Metal layer placement, 인증 운영 화면·원문UX10, legacy92 및 PMS7.x 원조건은
미완료다. schema/API/실행기 명령/배포 통과 기준/재시도 정책은 바꾸지 않았다.
깨끗한 누적 커밋을 main/research-v2에 반영하고 기존 배포 컨트롤러에 예약한다.
예약 후 폴링 대기 없이 다음 실행 소유자를 영속목표와 PMS에 남긴다.
