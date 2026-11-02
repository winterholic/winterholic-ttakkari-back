# Regna N4 CPU connector 독립 인수

**상태: 부분 — 검토는 마쳤으나 현재 source는 통합 인수 불가.**

현재 판정 대상 source는 `HEAD=a07ca4cff1136eb5825e740c1576e52543addb1c`다. producer 구현 commit `20764ae3ad38305d9ea85dcd92e883653a015df8`부터 이 HEAD까지 `src/`와 `tests/`는 동일하다. 이 후속 독립 검토에서도 connector source는 수정하지 않았고, 보충 반례·증거·보고서만 이 review 디렉터리에 기록했다.

## 판정

N4 fixed kind의 API/MCP 등록, work-session 제한, owner-bound 상태/취소, grant·lease·batch 중복 fence, 제한된 fake dispatch 연결은 source와 기존 receipt 및 새 반례로 확인했다. 그러나 원장 exact-baseline 판정이 실행 입력의 일치만으로 실패·다른 asset 기록까지 재사용 대상으로 간주하고, ledger inventory 변화는 재확인하지 않는다. 또한 `run_operation()`은 `runs` 부모 symlink를 따라 외부 경로에 private evidence를 쓸 수 있다. 첫 문제는 실제 고정 batch 실행을 false refusal할 수 있고, 나머지 둘은 별도 filesystem/동시 writer 조건에서 경계를 약화한다. 이 세 조건을 해소한 source delta가 다시 인수되기 전에는 다음 통합/배포 단위로 넘기지 않는다.

## Finding F1 — request bytes만으로 exact baseline 재사용을 판정

**도달 경로:** `run_operation()` → `issue_grant()` → `review_prior_attempts()` → `_walk_request_records()` (`src/ttakkari/research/regna_n4_combo_first6.py`). 동일한 request bytes SHA가 나오면 외부 record의 cell identity, model/runtime/template/tokenizer asset, 실행 상태, raw output validity를 판정하지 않고 `exact_baseline_matches`에 넣는다. 그 결과 `issue_grant()`는 `BaselineReuseError`를 내고 operation은 batch를 dispatch하지 않은 채 종료한다.

외부 Regna의 실제 N3 receipt schema를 읽었다. receipt는 `cells[].generations[].request_receipts[].request_body`에 실제 전송 body를 저장하고, asset identity는 별도 `runtime_assets`, 출력/완료 여부는 `execution_status`, cell status, raw integrity/usage 및 completion 필드에 둔다. backend scanner는 실제 중첩 구조를 재귀적으로 읽으므로 이 경로에 도달한다. HTTP request body의 동일성은 실제 loaded model asset이나 유효한 output의 동일성을 뜻하지 않는다.

새 테스트는 실제 schema 형태의 incomplete/runtime-error receipt를 만들고, 다른 model asset과 transport error가 기록된 상태에서 body만 같은 경우에도 `exact_baseline_reuse_found` 및 `BaselineReuseError`가 발생함을 재현했다. 별도 최소 dict record에도 status나 output이 없어도 동일 분류가 적용된다. 영향은 잘못된 output 재사용이 아니라 유효하지 않은 역사 기록 때문에 고정 batch 실행을 거부하는 false refusal이다. grant 전 거부라서 실제 child/host operation은 시작되지 않는다.

**필요한 교정:** exact reuse는 적어도 동일 cell/input identity와 execution asset/profile identity에 결속하고, 완료·raw integrity·usage·output 조건을 만족하는 재사용 가능한 결과인지 확인해야 한다. 실패 시도는 prior attempt로 기록하되 성공 baseline reuse로 판정하면 안 된다.

## Finding F2 — ledger 파일 목록 변경을 검토 끝에서 놓침

`review_prior_attempts()`는 `_ledger_files()`에서 얻은 최초 `files`만 순회한 뒤 같은 목록만 `_ledger_digest(files)`로 다시 읽는다. 첫 목록 스캔 중 새 `receipt.json`이 추가돼도 inventory를 다시 열거하지 않아 결과에 포함되지 않는다. 새 테스트가 실제 request body가 든 receipt를 중간에 추가해도 결과가 `no_exact_baseline_reuse`로 남는 것을 재현했다.

영향은 N4 connector 외부의 writer가 검토 도중 새 적격 원장을 추가할 때 duplicate batch를 막지 못할 가능성이다. 현재 backend 공용 resource lease는 지원되는 backend host operation들을 직렬화하므로 그런 외부 concurrent writer가 정상 경로에서 발생했다는 증거는 없다. 따라서 이를 관측된 운영 중복 실행이라고 주장하지 않는다. 다만 ledger는 backend lease 경로 바깥의 Regna repository 파일이며 source에는 inventory 안정화/재확인 잠금이 없다. 원장 정확성이 grant gate의 필수 입력인 만큼 목록을 끝에서 재수집해 처음과 비교하거나 고정 snapshot으로 읽어야 한다.

## Finding F3 — `runs` 부모 symlink가 외부 evidence 경로를 만든다

`run_operation()`은 `operation_roots(data_dir, job_id)` 이후 `output_root.parent.mkdir()`와 `output_root.parent.chmod(0o700)`을 수행한다. `data_dir/research/<kind>/runs`가 symlink면 `chmod`가 링크 대상 디렉터리에 적용되고, 이후 UUID job evidence도 그 외부 디렉터리에 생성된다. 새 테스트가 실제 `run_operation()`을 호출해 이를 재현했다. test의 grant/lease/asset/dispatch seam은 전부 fake였고 대상은 tmp 디렉터리였다. 테스트 결과로 외부 서비스 디렉터리는 0700이 되었으며 `operation-summary.json`은 configured data dir 밖의 링크 대상에 생겼다.

이 symlink를 API payload가 만들 수 있다는 증거는 없다. 이 finding의 전제는 service data dir 안의 filesystem precondition(예: 손상/같은 계정 프로세스의 변경)이다. 원격 호출자 취약점으로 확대 해석하지 않는다. 그래도 private prompt/HTTP/output evidence 경계를 코드가 보장하지 않고, 의도하지 않은 기존 디렉터리의 권한을 바꾸므로 실제 `run_operation()` 도달 가능한 path-containment 결함이다. 생성 직전 `runs` 경로의 symlink 거부와 resolved containment 검사가 필요하다.

## 현재 HEAD 후속 경계 확인

- **F1 의미:** 비교는 fixed expected baseline HTTP body와 과거 `request_body` bytes의 해시만으로 match를 만든다. 이는 과거 output을 새 실행에 재사용하는 동작이 아니라, `issue_grant()`가 새 batch를 거부하는 동작이다. 따라서 재현 피해는 **유효 output 없는 실패 시도나 다른 asset/cell의 같은 body가 false refusal을 일으켜 실행을 막는 것**이다. output을 잘못 재사용했다고 주장하지 않는다. 실제 중첩 receipt 및 bare dict 반례 모두 현재 HEAD에서 그 거부를 재현했다.
- **F2 후속 주장 범위:** producer의 “ledger mutation test 1 passed”는 기존 `attempts.jsonl` 파일의 bytes가 읽는 중 바뀌는 경우를 거부하는 테스트다. 그 결과는 inventory 변경을 해결하지 않는다. 별도 반례에서 최초 파일을 읽은 뒤 새 적격 `receipt.json`을 추가하면 최종 판정은 `no_exact_baseline_reuse`이고 새 파일은 `ledger_files`에 없다. backend `_reconcile()`에서 지원되는 N4 operation의 runner 종료 후 이 recovery 함수가 실제 호출되지만, Regna ledger는 그 공용 lease 대상이 아니므로 동시 writer가 운영상 확인됐다는 주장은 하지 않는다.
- **F3 chmod 범위:** 도달 가능한 변경은 `output_root.parent`, 즉 `.../<kind>/runs`가 가리키는 디렉터리에 `chmod(0700)`을 적용하는 것이다. 반례에서는 symlink target 디렉터리 mode가 바뀌고 `<target>/<job_id>/operation-summary.json`이 생성됐다. API 요청이 symlink를 만들 수 있다는 증거는 없으며, service data dir 아래 해당 symlink가 이미 있는 filesystem 조건에서만 재현된 경로다.
- **child/cleanup/recovery:** `run_operation()`은 `_dispatch()` 진입 직전에 `child_possible=True`로 두므로 dispatch 내부에서 batch receipt를 쓰기 전에 runner가 종료되면, failure receipt는 `child_may_exist=true`이고 lease는 release 대신 cancel 요청 상태로 남는다. 실제 caller `operations.jobs._reconcile()`은 runner process group을 정리한 뒤 N4 recovery를 호출한다. recovery는 `batch-01/receipt.json`을 요구하므로 failure receipt만 있고 batch receipt가 없는 경우 reconcile하지 않고 `cleanup_pending`/lease-retained 경로로 남는다. 추가 fake 반례는 이 보수적 거부를 확인했다. 이는 미확정 child를 두고 lease를 해제하는 문제는 아니며, no-child를 입증할 batch receipt가 없는 crash 경계에서 자동 복구가 막히는 가용성 한계다. 실제 runner/process/lease는 실행하지 않았다.

## 요구 항목별 확인

1. **kind/API/MCP/work-only/owner 경계 — 확인.** `regna_n4_combo_first6_cpu`만 입력 가능하고 extra field는 거부된다. API와 MCP는 work session만 허용한다. 같은 work owner의 status/read 및 cancel을 독립 호출로 확인했다. 기존 9개 host-operation receipt는 foreign/unknown 충돌 상세 비노출, foreign/unknown status/cancel 404 및 personal start 거부를 확인한다. 기존 MCP allowlist 1개 receipt와 함께 사용했다. 기존 `host_gpu_*` personal-only 경로와 N3 구현 코드는 변경되지 않았다.
2. **plan/pins/assets/mode/request/budget/grant — 확인 범위 제한.** raw plan/pins bytes SHA와 parsed plan canonical identity를 결속한다. 15 source pins, `batch-01` 6 cells, Qwen3 mode/profile, request identity, 6/120/900/30 budget이 grant 검증에 묶인다. fresh owner/job/lease ID+generation, expiry+cleanup reserve와 O_EXCL grant/batch claim이 있으며 같은 grant·batch 중복 claim 테스트가 있다. plan/pins는 byte hash 후 같은 raw bytes를 parse한다. `load_profile()`에서 source pins를 검증하고 dispatch의 `validate_plan()`이 pin set을 다시 검증한다. asset은 preflight hash 후 `MeasuredOwnedServer` 생성 직전 `_make_child()`에서 재검증된다. fake placeholder만 사용했으며 실제 asset load는 하지 않았다.
3. **baseline reuse / inventory — F1, F2.** 실제 receipt schema 및 재현은 위 Finding과 `historical-ledger-schema.json`, 독립 테스트 파일에 남겼다. 기존 scan은 malformed JSON을 거부하고 symlink leaf를 제외하며, 기존 중간 bytes mutation 테스트도 집중 11개 안에서 재실행되어 통과했다. 다만 목록 추가는 누락한다.
4. **fake HTTP/child·durable receipt·lease — 확인 범위 제한.** 지정된 focused suite에는 production `dispatch_batch()`와 `LlamaHTTPAdapter.complete()`를 실제 함수로 호출하고 HTTP/Popen/wait4를 mock한 성공·실패 경로가 포함된다. 성공 6-cell receipt, raw request/SSE/usage/output, terminal failure receipt, 해당 fake direct child의 positive wait4/reap을 확인한다. 새 `run_operation()` fake-seam에서 positive cleanup이면 fake lease release, unknown cleanup이면 cancel 요청 및 lease 미해제를 확인했다. runner exit recovery는 matching lease와 positive cleanup receipt일 때만 fake reconcile이 호출되고, runner absence 미확정이면 거부됨을 확인했다. 실제 child·lease acquisition·backend·모델은 실행하지 않았다.
5. **elapsed/outer wall — 시간 finding 없음.** Connector receipt는 attempt elapsed 합과 외부 monotonic wall 값을 별도 필드로 표시하고, 둘의 관계는 요약 boolean으로 보인다. prior N4 independent report의 production fake receipt에서 outer 값이 합보다 작을 때 validator가 받아들인 관찰은 재사용했으며, 이를 hard-deadline bypass로 승격하지 않았다. deadline enforcement, 실제 startup/cleanup timing은 재실행·실측하지 않았다.
6. **기존 검증/pins — 재사용 및 신규 좁은 실행.** 기존 N3 37, owner/API 9, MCP allowlist 1, source/lint/format/compile evidence는 producer의 raw receipt를 재사용했다. N3 31 및 N4 15 before/after pin evidence는 identity 비교로만 재사용했으며 과거 full N4 gate는 재실행하지 않았다. fullHEAD와 implementation commit 사이 `src/`·`tests/`는 동일하다. 새 counterexample test만 추가했다.

## 실행 증거

이 Run에서 실행한 명령과 핵심 출력:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_n4_execution.py
11 passed in 0.55s
EXIT=0

UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q review/regna-n4-independent-20261102/test_readonly_counterexamples.py
8 passed in 0.40s
EXIT=0

git diff --check
EXIT=0
```

Raw stdout/stderr/exit files: `evidence/focused-baseline.*`, `evidence/counterexamples.*`. Counterexample tests write only under pytest `tmp_path`; run-operation seams replace lease/assets/dispatch and launch no child. Producer's exact owner/API/MCP receipts are in `review/regna-n4-connector-20261102/evidence/03-host-operations-owner.*` and `04-mcp-fixed-allowlist.*`.

후속 focused 실행은 기존 ledger-content mutation, cleanup, recovery와 F1/F2/F3 반례를 함께 선택해 실행했다: `evidence/current-head-boundaries.stdout.txt`, `.stderr.txt`, `.exit.txt` (`9 passed`, exit 0). no-batch-receipt recovery 경계의 추가 fake-only 재현은 `evidence/current-head-no-batch-recovery.*` (`1 passed`, exit 0)이다. 모두 현재 HEAD source를 import했고 test filesystem은 `tmp_path`로 제한했다.

## Source, pins, routing

- Current HEAD is `a07ca4cff1136eb5825e740c1576e52543addb1c`; implementation code commit is `20764ae3ad38305d9ea85dcd92e883653a015df8`. `git diff --quiet <implementation> <fullHEAD> -- src tests` 반환은 exit 0이다. Source file SHA-256 및 worktree status는 `evidence/source-identity.json`에 있다. 대상 기존 tracked source는 수정하지 않았다.
- Producer `pin-sha-before.json` / `pin-sha-after.json`은 N4 15/15 및 N3 31/31 동일성을 나타내고 `identical_to_before=true`다. 이 검증자는 그 identity-only 증거만 읽었으며 external source/pin 재해시나 N4 전체 gate 재실행을 하지 않았다.
- 최초 독립 verifier의 당시 routing 한계는 `evidence/routing-receipt-independent.json`에 보존한다. 후속 correction owner `c471e595-2b30-4c5a-a130-4d984fc48cfa` / Run `89e6853f`에 대해서는 메인 `get_worker` 확인이 actual `codex / gpt-6-luna / work / companyai2 / quota_known`을 보완했다. 이 보완은 routing provenance에만 적용하며 F1/F2/F3 source 판정은 바꾸지 않는다. 이 Run에서 별도 worker receipt를 다시 조회하지 않았다. 보충 기록은 `evidence/routing-main-supplement.json`이다.
- 후속 SHA 대조에서 current HEAD는 계속 `a07ca4cff1136eb5825e740c1576e52543addb1c`, implementation `20764ae3ad38305d9ea85dcd92e883653a015df8` 대비 tracked `src/`·`tests/` diff exit 0이었다. 여섯 tracked connector entrypoint SHA는 기존 `evidence/source-identity.json` 값과 일치하며 출력은 `evidence/current-head-sha-confirmation.txt`에 남겼다. 현재 status에는 별도 untracked `tests/test_regna_n4_corrections.py`가 보였으나 이 파일은 열거나 수정·실행하지 않았다. 이 보충 Run에서는 테스트를 재실행하지 않았다.

## 범위 밖 / 다음 단계

- 하지 않음: source 수정/commit, 다른 저장소 수정, host operation, external process/model/asset load, actual lease acquisition, backend push/deploy, broad host operations suite, prepush 및 금지된 환경 gate.
- mypy는 producer의 기록에서 executable 부재(`Failed to spawn: mypy`, exit 2)로 미실행이며 재시도하지 않았다.
- 다음 통합 단위는 F1의 output/asset/input-aware reuse 판정, F2의 inventory snapshot/lock 경계, F3의 evidence path containment를 반영한 새 source SHA를 대상으로 한다. 이 REPORT와 현 대상 SHA를 먼저 인수 자료로 사용하면 된다.
- 이 판정은 **connector source 인수**에 한정한다. 실제 N4 연구 수행, host CPU 측정, Qwen3 품질/성능 또는 연구 완료를 뜻하지 않는다.
