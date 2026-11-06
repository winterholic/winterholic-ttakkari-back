# Research Lab 백엔드 통합·배포 기록 (2026-11-07)

## 반영 대상

- 기준 원격 `main`: `adba28d2e0aca7d886e7c345f12178793d17c727` (통합 시작 시 `git ls-remote` 및 fetch 결과).
- 통합 대상 최종 상태: `8fdeab4f0a85c8d3bf1eb9b254f43d98ff11b630`.
- 공통 기준: `521f469aeb27a28a897bcac55fa969b1a5c2323e`.
- 통합 및 배포 대상 코드 커밋: `4d09c79f748c9915db7cc3a4b54c2deadce0d834` (`feat(research): integrate verified lab backend`).
- GitHub 계정: 지원 계정 목록의 `winterholic` (활성). `gh auth switch`를 사용하지 않았다.
- 새 release worktree: `/Users/winterholic/development/projects/winterholic-ttakkari/back-lab-release-20261107`, branch `release/lab-20261107`.
- 기존 `back`과 `back-lab-practical-20261106` worktree는 읽기만 했다. 기존 dirty 변경을 reset하거나 편집하지 않았다. 독립 검증 보고서 두 건도 읽기 전용으로 참고했으며, practical tree의 untracked 독립 보고서는 stage하지 않았다.

## 통합 및 MCP 대조

공통 기준 이후 대상의 Lab 변경은 최신 `main`에 이미 반영된 커밋이 없어 최종 diff를 새 clean worktree에서 단일 스쿼시 통합 커밋으로 만들었다. 검증되지 않은 중간 상태를 별도 게시하지 않았다. 원격 `main`의 다른 변경은 그대로 보존했다.

양쪽에서 바뀐 `src/ttakkari/orchestrator/mcp_server.py`를 대조했다. 최신 `main`의 `notify_user` 사용자 설정 설명과 Lab의 `run`, `run_start`, `run_retry`, `external_report` operation 및 인자 설명이 자동 병합 결과에 함께 남았다. 양쪽 변경은 파일 안의 서로 다른 구간에 있었다. 통합 MCP operation/API 계약 회귀에서 각 operation 등록과 API 배선을 확인했다.

## 통합 검증

- MCP·Lab API·선택 Run 보고서 연결·artifact 회귀와 대표 guard/prompt 회귀:

  ```text
  UV_CACHE_DIR=/private/tmp/research-lab-release-uv-cache uv run pytest -q tests/test_research_lab.py tests/test_research_report_selected_run.py tests/test_research_v2_api_contract.py::test_export_report_returns_artifact_metadata unit_tests/test_research_terminal_and_dispatch.py::test_mcp_manage_research_operation_names_match_agent_api unit_tests/test_research_terminal_and_dispatch.py::test_agent_in_allows_run_retry_and_run_start_and_external_report unit_tests/test_research_regna_improvements.py::test_memory_guard_stops_failed_adapter_and_preserves_adapter_receipt unit_tests/test_research_regna_improvements.py::test_llm_inference_preserves_prompt_and_trajectories unit_tests/test_research_regna_improvements.py::test_llm_preview_fallback_is_marked_incomplete_and_preserves_raw_run
  23 passed in 2.36s
  ```

- 통합된 변경 Python 12개 Ruff:

  ```text
  UV_CACHE_DIR=/private/tmp/research-lab-release-uv-cache uv run ruff check src/ttakkari/api/research.py src/ttakkari/host_gpu.py src/ttakkari/orchestrator/mcp_server.py src/ttakkari/research/adapters/llm_inference.py src/ttakkari/research/runner.py src/ttakkari/research/service.py tests/test_research_lab.py tests/test_research_report_selected_run.py tests/test_research_runner_lifecycle.py tests/test_research_v2_core.py unit_tests/test_research_regna_improvements.py unit_tests/test_research_terminal_and_dispatch.py
  All checks passed!
  ```

- push preflight gate (통합 SHA `4d09c79f748c9915db7cc3a4b54c2deadce0d834`): Ruff on changed Python 13개 통과, contract `85 passed, 6755 deselected in 17.73s`, changed test files `89 passed in 7.58s`. 이 게이트 출력은 전체 CI 통과를 뜻하지 않는다.
- 기존 독립 증거에서 이번 MCP 병합에 영향받지 않은 실행 안전·오류 격리·재전송/retry·채택 gate·pagination 회귀 85개와 경계 회귀 9개를 재사용했다. 해당 전체 묶음은 다시 돌리지 않았다. 보고서 연결 수정에 직접 영향받는 새 보고서·artifact 회귀는 위 통합 명령으로 새로 실행했다.
- Push preflight 최초 시도는 기본 uv cache 접근 거부(`Operation not permitted`)로 중단됐다. 허용된 Run-local `/private/tmp` cache로 재실행했다. 그때 증거용 `ca1fa234-counterexamples.py`의 Ruff F401 1건 및 RUF012 2건이 발견되어 import/class-attribute annotation만 정리한 뒤 게이트를 다시 통과했다.

## 원격 반영과 배포 예약

```text
$ git ls-remote origin refs/heads/main
adba28d2e0aca7d886e7c345f12178793d17c727  refs/heads/main

$ UV_CACHE_DIR=/private/tmp/research-lab-release-uv-cache GH_AS_USER=winterholic GH_AS_TOKEN="$(gh auth token --hostname github.com --user winterholic)" git -c credential.helper= -c 'credential.helper=!f() { echo username=$GH_AS_USER; echo password=$GH_AS_TOKEN; }; f' push origin HEAD:refs/heads/main
[pre-push] 통과: Ruff, contract 85 passed, 변경 테스트 89 passed
adba28d2..4d09c79f  HEAD -> main

$ git ls-remote origin refs/heads/main
4d09c79f748c9915db7cc3a4b54c2deadce0d834  refs/heads/main
```

`request_backend_deploy` 영수증:

- 예약 ID: `6d84e591-e69d-412d-b585-806040b5f5e0`.
- 요청 revision: `4d09c79f748c9915db7cc3a4b54c2deadce0d834`.
- 요청 직후 실제 상태: `requested` (`skip_tests=false`, strategy `generations`).
- 이후 `waiting_slot`, `draining`, `succeeded` 여부는 조회하지 않았다. 여기서 기다리거나 polling하지 않는다.
- 후속 담당자는 Ttakkari MCP `backend_deploy_status(job_id="6d84e591-e69d-412d-b585-806040b5f5e0")`로 상태를 확인한다. `requested`/`waiting_slot`은 대기, `draining`은 새 세대 활성화 및 이전 세대 정리 진행, `succeeded`는 이전 세대 retire까지 확인한 상태로 구분한다. `recovery_pending`이면 로그/예약을 조사한다.

## 실행하지 않은 항목과 후속

- GPU 및 실제 REGNA 실행, 별도 REGNA 작업, 운영 DB/API 쓰기, 브라우저 UI 여정 및 실제 MCP transport: 실행하지 않았다. 보고서에 적힌 격리 계약 검증 범위로 한정한다.
- 실제 routing profile: 이름을 기록하지 않았다. 이번 세션에는 profile 이름을 뒷받침할 routing 영수증이 없다.
- 운영 CPU 연구 여정: 배포 반영 뒤 상위 작업에서 별도로 실행할 항목이라 이번에는 실행하지 않았다.
- 배포 예약 이후 상태 조회: 대기 및 polling 금지에 따라 실행하지 않았다.
