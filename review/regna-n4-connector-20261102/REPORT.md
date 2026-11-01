# Regna N4 r2 고정 first-six CPU connector 작업 기록

## 결과

고정 kind `regna_n4_combo_first6_cpu`를 기존 host operation으로 연결했습니다. 코드와 검증 evidence는 implementation commit `20764ae3ad38305d9ea85dcd92e883653a015df8`에 있습니다. base는 요청된 read-only SHA `748c35c666d3bf1b221d15fa193ccbe44627e7b4`이고, 작업 branch는 `codex/regna-n4-connector-20261102`입니다.

이 결과는 **backend source와 fake transport 경로 검증**입니다. 실제 N4 host operation, child, model, asset load, lease acquire, push, deploy는 실행하지 않았습니다. 따라서 실제 N4 연구 실행이나 품질 측정 완료로 해석하지 않습니다. 기존 final-isolated acceptance gate도 재실행하지 않았습니다.

## 구현 범위

- `src/ttakkari/research/regna_n4_combo_first6.py` — plan·pins를 고정 경로에서 읽고 같은 bytes를 hash/parse, 15개 pin 검증, batch-01 6셀·Qwen3-1.7B Q4_K_M·generation mode·입력 identity·6/120/900/30 예산 고정. 임의 payload/argv/path는 거부합니다.
- 실제 이전 attempt ledger에서 baseline HTTP request body의 **직렬화 bytes SHA-256**를 비교합니다. ledger는 스캔 중 파일 식별자와 크기·시각을 대조하고, 스캔 직후 전체 digest를 다시 확인합니다. 변동이나 손상은 fail-closed하며, 정확한 match는 경로·request hash·record hash와 함께 private refusal evidence로 남기고 grant 발급을 거부합니다.
- grant는 owner session, job, 공용 resource lease ID/generation, raw/canonical plan identity, source pins, assets, generation mode, 여섯 입력 identity와 prior-attempt review에 결속합니다. grant 만료·변조·중복 claim은 거부하고 첫-six batch claim은 새 grant ID로도 재사용할 수 없습니다.
- 기존 operation kind/API/MCP allowlist와 runner에 연결했습니다. work session만 시작하고 같은 owner만 상태·취소할 수 있으며, foreign/unknown owner 충돌 응답은 상세를 감춥니다. host GPU의 personal-only 권한은 바꾸지 않습니다.
- dispatch는 승인된 N4 `dispatch_batch()`에서 `MeasuredOwnedServer`와 기존 `LlamaHTTPAdapter.complete()`까지 이어집니다. private server-owned evidence에 raw request/SSE/usage/output 및 실패·정리 receipt를 기록합니다. 직접 child reap이 확인되지 않으면 lease를 보존합니다. connector summary는 attempt elapsed 합과 outer monotonic 시간을 함께 표시합니다.
- fake success/failure dispatch는 가짜 HTTP·child·placeholder asset으로 실행했습니다. 6셀 성공, SSE 완료 누락, direct-child reap, owner 경계, 변조·중복 claim, 원장 재사용·변동, 불확실 cleanup을 시험했습니다.

변경 경로:

- `src/ttakkari/research/regna_n4_combo_first6.py`
- `src/ttakkari/operations/{plans,jobs,runner}.py`
- `src/ttakkari/api/operations.py`
- `src/ttakkari/orchestrator/mcp_server.py`
- `tests/test_regna_n4_execution.py`
- `tests/test_host_operations.py`
- `tests/test_route_inventory.py`
- `docs/api-contract.md`
- `docs/adr/0037-execution-capabilities-and-host-gpu.md`

## TDD 및 source pin evidence

- 초기 RED: `evidence/initial-red.txt` — 구현 전 `tests/test_regna_n4_execution.py` 수집이 새 connector 모듈 import 불가로 exit 2였습니다.
- 최종 GREEN: `evidence/01-regna-n4.stdout.txt` — fake dispatch 포함 11 passed, exit 0.
- N4 final-isolated plan SHA-256 `4b0888456b1aa079b0cb23764fa9a8cd14ad3c7c7e90381e0ed5415a36dc6a19`, source-pins bytes SHA-256 `765c36b5e0323bdf9bd4f06f009c96e231b4bf54224e135aa433a8907ce3c82d`입니다. **15/15** pinned source hash·크기가 before/after 모두 일치합니다.
- 기존 N3 manifest SHA-256 `370ab02657803d134e2ca147f6ce78404c3c70d52b636ec4baaf8bed04cffe0c`; **31/31** manifest source가 before/after 모두 일치합니다.
- 상세 rows는 `evidence/pin-sha-before.json`, `evidence/pin-sha-after.json`에 있습니다. 비교 결과 `identical_to_before: true`입니다. 이 검사는 pin identity 확인이며 과거 source/mock gate의 재실행은 아닙니다.

## 검증 receipt

각 명령의 전체 stdout·stderr·exit code는 `evidence/`의 대응 파일과 `check-results.json`에 보존했습니다. 지정된 DB 검증은 순차 실행했습니다.

1. 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_n4_execution.py`

   stdout:
   ```text
   ...........                                                              [100%]
   db reset: 11 selective (avg 1.2/120 tables), 0 forced full, 0 fell back to full
   11 passed in 0.67s
   ```

   stderr: 비어 있음 · exit: `0`

2. 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py`

   stdout:
   ```text
   .....................................                                    [100%]
   db reset: 37 selective (avg 1.8/120 tables), 0 forced full, 0 fell back to full
   37 passed in 2.97s
   ```

   stderr: 비어 있음 · exit: `0`

3. 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_host_operations.py -k 'regna or conflict or owner'`

   stdout:
   ```text
   .........                                                                [100%]
   db reset: 9 selective (avg 8.2/120 tables), 0 forced full, 0 fell back to full
   9 passed, 71 deselected in 1.05s
   ```

   stderr: 비어 있음 · exit: `0`

4. 좁은 MCP allowlist 검증 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_route_inventory.py -k 'mcp_host_operation_start'`

   stdout:
   ```text
   .                                                                        [100%]
   db reset: 1 selective (avg 3.0/120 tables), 0 forced full, 0 fell back to full
   1 passed, 7 deselected in 0.68s
   ```

   stderr: 비어 있음 · exit: `0`

5. 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff check src/ttakkari/research/regna_n4_combo_first6.py src/ttakkari/operations/plans.py src/ttakkari/operations/jobs.py src/ttakkari/operations/runner.py src/ttakkari/api/operations.py src/ttakkari/orchestrator/mcp_server.py tests/test_regna_n4_execution.py tests/test_host_operations.py tests/test_route_inventory.py`

   stdout: `All checks passed!` · stderr: 비어 있음 · exit: `0`

6. 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run python /Users/winterholic/.claude/dev-experts-skills/dev-python/scripts/pitfall_scan.py src/ttakkari/research/regna_n4_combo_first6.py`

   stdout: `total: 0 finding(s)` · stderr: 비어 있음 · exit: `0`

7. 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff format --check src/ttakkari/research/regna_n4_combo_first6.py tests/test_regna_n4_execution.py`

   stdout: `2 files already formatted` · stderr: 비어 있음 · exit: `0`

8. 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run python -m py_compile src/ttakkari/research/regna_n4_combo_first6.py`

   stdout/stderr: 비어 있음 · exit: `0`

9. 명령: `UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run mypy src/ttakkari/research/regna_n4_combo_first6.py`. 환경에 실행 파일이 없어 완료하지 못했습니다.

   stderr:
   ```text
   error: Failed to spawn: `mypy`
     cause: No such file or directory (os error 2)
   ```

   stdout: 비어 있음 · exit: `2`

`git diff --check`도 통과했습니다. 넓은 test suite와 실제 model 측정은 실행하지 않았습니다.

## 작업 라우팅

요청된 `engine=auto`, `task_type=general`로 read-only 독립 검토 worker를 지정했습니다. worker routing receipt의 선택 모델은 **`gpt-6-luna` (engine `codex`)**이며 `verification_required=false`였습니다. 최초 생성 응답의 모델 문자열은 `claude-sonnet-5-5-high`였지만 routing receipt에서 해당 모델이 계정 model 목록에 없어 제외되고, `gpt-6-luna`가 선택됐습니다. 직접 `account_usage` 도구는 이 Run에 노출되지 않아 `get_worker.routing.selected`에서 실제 선택 모델을 기록했습니다 (`evidence/routing-receipt-summary.json`).

독립 검토에서 발견된 원장 scan/hash TOCTOU는 같은 scan 중 digest 계산, file identity 대조, scan 후 digest 재확인으로 보완했고, 변동을 주입하는 회귀 테스트를 추가했습니다. 이는 소스 검토와 fake input 검증이며 원장에 대한 host operation을 실행한 것은 아닙니다.

## 미실행 및 다음 독립 확인

- 미실행: 실제 host operation/child/model/asset load/lease acquire — 이 source connector 작업 단위에서 실행하지 말라는 범위 조건.
- 미실행: push/deploy — 별도 integration worker의 N3 push/deploy 및 기존 main/index는 건드리지 않았습니다.
- 다음 독립 검증자는 이 worktree의 commit `20764ae3ad38305d9ea85dcd92e883653a015df8`, 본 보고서, `evidence/`를 확인하면 됩니다. 이번 증거만으로 N4의 실제 모델 결과나 연구 완료를 주장하지 않습니다.
