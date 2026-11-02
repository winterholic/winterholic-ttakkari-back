# Regna N4 F1/F2/F3 독립 교정 인수

**상태: 완료 — 통합 인수: passed.** 판정 대상 correction source는 `68283b6597a5af180bfcd2616fcd554f144cd21d`, correction base는 `a07ca4cff1136eb5825e740c1576e52543addb1c`다. 요청한 target commit은 존재하며, 현재 checkout HEAD `27d706d962ac41d7fdf7592296f93c2449b0a02b`와는 다르다. 다만 `git diff --quiet <target> HEAD -- src tests`가 exit 0이라 target과 현재 `src/`·`tests/` 바이트 내용은 일치한다. 현재 tracked source/tests 변경은 없다.

## 요구 항목 판정

| 항목 | 판정 | 확인 결과 |
|---|---|---|
| F1 — baseline reuse eligibility | 통과 | 같은 유효 baseline request는 cell·receipt/generation·generation mode·asset·완료·raw integrity/durability·usage가 맞을 때만 eligible exact match로 남고, grant는 중복 실행 방지를 위해 거부한다. 실패, foreign asset, cell identity 누락은 `prior_attempts`에 사유와 함께 보존되며 exact reuse로 세지 않아 새 grant를 막지 않는다. |
| F1 — 실제 historical schema/raw 의미 | 통과, 경계 명시 | 실제 Regna N3 r4 receipt와 연결 raw JSON을 읽었다. receipt의 성공·usage·asset·raw 필드 경로가 구현의 recursive context 탐색과 맞았다. 선택한 실제 raw 파일의 크기/SHA, request body/SHA, SSE bytes/SHA, generation completion/usage 및 cell의 최종 generation usage가 서로 일치했다. 독립 eligibility probe는 이 실제 receipt 구조에서 request body만 synthetic expected body로 바꿔 positive 및 incomplete/foreign/missing-identity 분기를 검증했다. 이 probe는 실제 과거 N3 body가 N4 expected request와 일치한다는 주장은 아니다. 구현은 기록된 raw verification flags를 eligibility에 사용하고 매번 원본 raw 파일을 다시 열지는 않는다. 이번에 검사한 실제 원장은 flag의 근거가 raw file과 일치했다. |
| F2 — production inventory | 통과 | production discovery를 사용하는 scan 중 inventory 추가·삭제·교체를 각각 거부한다. 명시적 test ledger의 기존 bytes mutation 거부도 그대로 통과했다. 임의 동시성/무한 TOCTOU 탐색은 하지 않았다. |
| F3 — private path 경계 | 통과 | `research`, operation kind, `runs`, `claims`, job evidence 경로 각각 symlink를 tmp 경로에서 검증했다. 전부 쓰기 경로 진입 전에 거부됐고 외부 target의 mode와 기존 content는 그대로였다. 기존 원본 symlink counterexample/report는 읽기만 했다. |
| fake dispatch/cleanup/lease | 통과 | 결합 suite의 fake dispatch 성공·실패, raw receipt, direct-child reap 및 positive cleanup, unknown cleanup 때 lease 보존 검사가 통과했다. no-batch-receipt recovery의 `child_possible` lease 보존은 기존 independent 증거에서 재사용했다. 보수적 cleanup 동작이며 이 세 correction의 추가 gate로 취급하지 않았다. |
| API owner, work-only, host_gpu, N3, frozen pins | 통과, 기존 증거 재사용 | target delta에 API/host_gpu/N3 구현 변경이 없다. 기존 host-operation owner/API 9개, MCP fixed allowlist 1개, N3 focused 37개, N3 manifest 31개 및 N4 final isolated pins 15개 증거를 재사용했다. pin before/after identity는 기존 producer receipt에서 동일하다. 과거 전체 gate는 반복하지 않았다. |

## 실행 및 원본 근거

요청된 순차 실행(동일 `UV_CACHE_DIR`)과 독립 반례 실행 결과:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_n4_corrections.py tests/test_regna_n4_execution.py
19 passed in 0.71s; exit 0

UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q review/regna-n4-correction-independent-20261102/test_actual_receipt_and_symlinks.py
10 passed in 0.33s; exit 0

git diff --check
exit 0
```

각 명령의 command/stdout/stderr/exit 파일은 `evidence/`에 있다. 먼저 각 기존 test module도 별도로 실행해 8개와 11개 통과를 확인했으며 해당 원본 로그도 보존했다.

대조한 source/spec/원본 증거:

- Connector source: `src/ttakkari/research/regna_n4_combo_first6.py` (target 및 현재 checkout SHA-256: `a67579049167432a21df72be26b815f33f1add77c89339abf8aad3b637b1b4bf`). 핵심 분기: `_historical_reuse_ineligibility()`와 `review_prior_attempts()` (F1/F2), `_validate_private_path()`·`_prepare_private_directory()` 및 `run_operation()` 사전 경로 검사(F3).
- N4 contract: `docs/api-contract.md`의 N4 CPU operation 항목, `docs/adr/0037-execution-capabilities-and-host-gpu.md`의 제한된 N4 operation 결정.
- 실제 schema receipt: `/Users/winterholic/development/hyperez/regna-ios/docs/research/practical-20261029/execution/runs/n3-qwen3-1p7b-q4km-batch01-r4-20261101/receipt.json`과 그 receipt가 가리키는 `raw/` evidence. schema 요약은 기존 `review/regna-n4-independent-20261102/evidence/historical-ledger-schema.json`에 있다.
- raw verification producer 의미: `/Users/winterholic/development/hyperez/regna-ios/verification/n4-r2-independent-20261102/frozen-source-snapshot/tools/practical_research/n5/lifecycle.py`의 `verify_receipts()`·`RawStore.store_json()` 및 `execution/independent_holdout_n3_runtime_r4.py`의 raw/usage receipt 생성 코드.
- Historical request: `/Users/winterholic/development/hyperez/regna-ios/docs/research/practical-20261029/lead-review/continue-to-completion-20261102/original-user-request.txt`, `prior-contract.json` (외부 저장소 read-only).
- Reused owner/MCP/N3/pin evidence: `review/regna-n4-connector-20261102/evidence/03-host-operations-owner.*`, `04-mcp-fixed-allowlist.*`, `02-regna-n3-regression.*`, `pin-sha-before.json`, `pin-sha-after.json`; correction source/pin identities are recorded in `review/regna-n4-correction-20261102/evidence/source-pin-identity.json`.

## 실행 경계와 변경 사항

요청된 route는 `engine=auto`, `task_type=verification`이다. 이 Run에서 보이는 executor는 Codex이며, 실제 run별 selected routing/model receipt와 effort 필드는 도구에서 노출되지 않아 미확인으로 기록했다. effort 미노출은 source 불합격으로 보지 않았다.

검증은 source inspection, fake HTTP/child/lease 및 tmp 파일·symlink, 실제 historical receipt/raw evidence read-only 확인으로 제한했다. 실제 host operation/process/model/asset load/lease acquisition, push/deploy는 0회다. 금지된 ps/sysctl/Metal/preexec 경로는 호출하지 않았다.

새 파일과 모든 실행 증거는 이 디렉터리에만 추가했다. source, tests, 기존 report/raw/oracle/scorer/pins는 수정하지 않았고 commit/push/deploy도 하지 않았다.

## 문서 전용 후속 commit 범위

후속 문서 commit `27d706d962ac41d7fdf7592296f93c2449b0a02b` (`docs: record N4 correction follow-up evidence`)의 변경 목록은 `review/regna-n4-correction-20261102/REPORT.md`와 그 evidence의 `06-follow-up-corrections.*`, `07-follow-up-n4-execution.*`뿐이다. 이 후속은 문서와 8/11개 focused test 실행 영수증을 추가했고 `src/`·`tests/`를 바꾸지 않았다.

따라서 commit 범위는 분리해 해석한다: target `68283b6597a5af180bfcd2616fcd554f144cd21d`에 속한 producer `01–05` 영수증은 correction source의 최초 테스트/lint/format/diff 결과다. `27d706d9`에 속한 `06/07`은 동일 source의 별도 후속 focused 실행이다. 이 두 묶음을 하나의 commit 영수증으로 합산하지 않았다. 본 독립 검증의 결합 19개와 추가 10개 영수증은 이 report 디렉터리의 별도 실행이다.

독립 검증 단계의 최초 HEAD 동일성 assert 실패와 old target 경로에서 새 `06` 파일을 읽으려 한 실패는 문서 전용 후속 commit을 고려하지 못한 audit/checker 계획 오류다. 어느 것도 source 실행 실패가 아니며 source 불합격 판정 근거가 아니다. target source SHA와 현재 문서 HEAD는 위에서 별도로 기록했고, source/tests 동일성 비교 exit 0을 보존했다. 이 보충에서는 테스트, source 검증, 전체 gate를 재실행하지 않았다.
