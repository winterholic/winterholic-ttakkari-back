# AI Research Lab 실용화 E2E 실행·재시도·배선 결함 수정

- 날짜: 2026-11-06 (Asia/Seoul)
- 대상 저장소: `back-lab-practical-20261106` (worktree of winterholic-ttakkari)
- 브랜치: `fix/lab-practical-20261106`
- 기준 리비전: `origin/main` (`521f469aeb27a28a897bcac55fa969b1a5c2323e`)
- 운영 배포 리비전: `c396370f` (`ops_list_deploys` 성공 확인)
- 기존 dirty main HEAD `4ea068af`는 보존하고 격리된 전용 worktree에서 작업 진행.

## 주요 수정 내용

1. **`_finish_experiment_if_terminal` 재시도(retry) 성공 후 Experiment 터미널 상태 복구 결함 수정 (P0)**:
   - Run 실패 후 `retry_run`으로 재시도하여 새 Run이 `succeeded`되었음에도, 이전 실패한 Run이 `statuses`에 남아있어 Experiment가 영원히 `failed`로 유지되던 결함을 수정.
   - `retry_of_run_id`로 재시도된 과거 Run은 유효 판정에서 제외하고 최신 trial 기준으로 Experiment 상태(`completed` 등)를 결정하도록 보정.

2. **`manage_research` 및 `/api/agent/research` 라우터 에이전트 연산 미배선 해결 (P1)**:
   - 에이전트/오케스트레이터가 `manage_research`를 통해 실험 실행을 시작(`run_start`), 실패한 실행을 재시도(`run_retry`), 외부 평가 결과를 등록(`external_report`)할 수 있도록 `AgentIn` 스키마 및 디스패치 핸들러를 배선.
   - `manage_research` MCP 도구의 Literal 및 docstring에 해당 연산 명시.

3. **`snapshot` (연구 재개 `resume`) 원자료·결과·실행 상태 누락 보완 (P1)**:
   - 연구 재개 시 과거 실행 이력(`runs`), 최적 채택 결과(`best_results`, `best_result`)를 함께 반환하여 재개 에이전트가 실패 영수증과 최적 설정을 파악하고 다음 단계를 수립할 수 있도록 보강. 기존 4개 컬렉션(`nodes`, `relations`, `experiments`, `timeline`)의 pagination totals 계약은 엄격히 유지.

4. **`export_study_report` 리포트 지표 및 최적 결과물 연계 강화 (P1)**:
   - 마크다운 리포트에 `## 채택된 최적 결과물` 섹션 및 Run별 주요 지표 측정값 열을 추가.

## 검증 증거

- 수정 전 최소 재현 실패(RED) 3건 확인:
  - `test_finish_experiment_recovers_to_completed_when_retry_succeeds`: `AssertionError: Expected experiment to be completed, but got failed`
  - `test_agent_in_allows_run_retry_and_run_start_and_external_report`: `pydantic_core._pydantic_core.ValidationError`
  - `test_snapshot_includes_best_result_and_terminal_runs`: `AssertionError: snapshot missing best_result`
- 수정 후 좁은 단위 테스트 및 회귀 테스트 69개 전원 통과(GREEN, 1.10s):
  `UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest unit_tests/test_research_terminal_and_dispatch.py unit_tests/test_research_rules.py unit_tests/test_research_benchmark.py unit_tests/test_research_owner_receipt.py -v`
- Ruff 린트 통과 (`All checks passed!`).
- 상세 증거: `docs/research-lab/evidence/research-lab-practical-recovery-20261106.md`.

## 미실행 / 확인 필요
- 미실행: real PostgreSQL asyncpg loopback 연결 검증 (샌드박스 정책 `network_loopback` 차단으로 인한 `PermissionError: [Errno 1] Operation not permitted`).
- 미실행: `~/bin/ttk get /api/research/studies` (`로그인 실패 400: Direct IP access is not allowed`).
- 미실행: main push 및 운영 배포 (지침에 따라 이번 단계에서 제외).

## 2026-11-07 보존 worktree 후속 교정
- 시작 상태: HEAD `ca1fa23419017ed63857114a61c2646478bfd47f`, branch 기준 `521f469aeb27a28a897bcac55fa969b1a5c2323e`. 다른 트리에는 쓰지 않음.
- 안전 중단·원인 receipt, snapshot runs/best_results/configurations 경계, AgentIn/MCP dispatch·scope/session 거부, idempotency replay/conflict/fresh batch 및 retry provenance, finite measured best-result, LLM preview completeness 반례를 이어서 검증했다.
- Configuration total을 현재 experiment page 범위로 맞췄다. 빈 experiment page는 configurations total 0을 반환하며, 실제 페이지 구성의 bounded list/total/has_more가 같은 scope를 사용한다.
- 격리 PostgreSQL E2E 및 선택 회귀: `85 passed in 5.15s`; 변경 파일 ruff: `All checks passed!`. 정확한 명령·출력은 `docs/research-lab/evidence/research-lab-practical-recovery-20261106.md`에 기록.
- 구현/회귀 커밋: `82a81dd6d91c5b59ca949d4e086614cccb9dcee7`.
- 실제 GPU/운영 DB/live API/push/deploy 미실행. 현재 account profile 미확인.
- 안전 명세 assertion 후속: frozen ca1fa234의 memory-guard+failed 및 unknown-cancelled 형제 실행은 unsafe baseline 관측으로 README에 명시했다. 교정 회귀는 guard 시 cancelled 주상태+adapter 원 오류 receipt, unknown/memory cancellation 뒤 후속 Run 미실행, adapter-local TimeoutError/deadline 구분, preview source/completeness를 단언한다. 선택 회귀 86 passed, focused safety 8 passed, Ruff 통과.
