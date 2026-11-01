# Regna 고정 CPU operation 통합 기록

- 날짜: 2026-11-02 (Asia/Seoul)
- 통합 branch: `integ/regna-cpu-integration-20261102`
- base: `83302612c8edaaa40844a423e7223d2ee346e382` (`origin/main`, fetch 뒤 확인)
- feature source: `c3211d1fd77077342615484d0697cbd0d4bd4b54`
- 통합 방식: feature의 committed 10개 커밋을 순서대로 cherry-pick. conflict 없음.
- 비교: base 대비 12개 경로 및 binary diff SHA-256 `5ccf92dc5f97e7f5d359daabdf7c1107b79bc88dad15864524d3094ff59600af`가 feature와 통합 worktree에서 일치.
- 원 backend worktree의 HEAD는 `4ea068afc0ce85ff1e431e0e1315e26ae9dc75f3`이며 다른 세션 dirty 변경이 확인됐다. 이 작업에서는 읽기 전용 status/rev-parse만 수행했고 파일·index·stash를 변경하지 않았다.
- feature worktree에는 독립 검증 기록이 untracked 상태로 존재했다. 그것은 cherry-pick하지 않았고 source worktree를 수정하지 않았다.

## 통합 범위

`docs/adr/0037-execution-capabilities-and-host-gpu.md`, `docs/api-contract.md`, `review/regna-cpu-execution-20261102/REPORT.md`, `src/ttakkari/api/operations.py`, `src/ttakkari/operations/jobs.py`, `src/ttakkari/operations/plans.py`, `src/ttakkari/operations/runner.py`, `src/ttakkari/orchestrator/mcp_server.py`, `src/ttakkari/research/regna_q25_first6.py`, `tests/test_host_operations.py`, `tests/test_regna_q25_execution.py`, `tests/test_research_q25_dispatch.py`.

Frozen Regna 원자료·manifest·scorer·oracle 및 개인 전용 `host_gpu` 권한의 통합 변경은 없다. 실제 Q25 operation 시작은 실행하지 않았다. 기존 broad run의 `49 failed, 241 passed`는 원 로그 부재와 환경 원인 미확인이라는 기존 한계를 그대로 유지했고 재실행하지 않았다.

## 통합 검증

원 명령 및 별도 stdout/stderr/exit 파일은 `evidence/` 아래 보존했다. DB를 사용하는 pytest 명령은 순차 실행했다.

1. `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py` — **exit 1**, `1 failed, 22 passed, 14 errors in 3.02s`. 반복 오류는 고정 외부 자료 `/Users/winterholic/development/hyperez/regna-ios/tools/practical_research/runtime/llama_http.py`가 manifest pin과 불일치한다는 `ProfileError`다. 이 경로/자료는 읽기·수정·복구하지 않았으며 테스트를 재실행하지 않았다. 테스트 명령 실행으로 `.venv`는 새로 생성됐다(ignored 환경 파일).
2. `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_research_v2_api_contract.py tests/test_host_operations.py -k 'test_research_v2_api_contract or arbitrary_input or heavy_kind_outside_allowlist or regna or conflict or owner'` — **exit 0**, `85 passed, 62 deselected in 5.48s`.
3. 변경된 Python 9개 파일 `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff check ...` — **exit 0**, `All checks passed!`.

`evidence/01-*`, `02-*`, `03-*`는 각각 정확한 명령, stdout, stderr, 프로세스 exit code를 담는다. 기존 독립 검증의 base focused/API/manifest 2개 결과는 별도 revision 범위 증거로 재사용했으며 이번 통합 검증으로 합산하지 않는다.

## Push 및 배포 예약

정상 `git push origin HEAD:refs/heads/main`은 pre-push hook을 통해 contract suite와 변경 테스트 파일을 실행한다. 위에서 변경 테스트 중 Q25 고정 자료 검증이 실패했으므로 gate를 우회하지 않고 일반 push 결과를 확인한다. Push가 성공한 경우에만 원격 full SHA를 확인하고 같은 SHA로 `request_backend_deploy`를 호출한다. 예약 접수 상태는 운영 배포 성공으로 표현하지 않는다. 실제 원격 SHA와 예약 ID/state는 완료 시 이 기록의 별도 `reservation.txt` evidence로 추가한다.
