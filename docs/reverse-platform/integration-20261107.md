# Reverse platform 통합 준비 기록 — 2026-11-07

## 기준과 범위

- `origin/main` fetch 결과: `40e88581cc1010287ef2b82e13188e2d6c6e8df9`.
- 통합 worktree: `back-reverse-release-integration-20261107`.
- 브랜치: `integ/reverse-release-integration-20261107`.
- 통합은 원격 `main`에서 분기한 뒤 필요한 커밋을 cherry-pick했다. push·배포는 하지 않았다.
- 코드 통합 직전 HEAD는 `c31527e3581842af7c765dbc900afb8168a3e48b`. 이 문서 커밋이 준비본의 마지막 변경이다.
- 원본 `back-reverse-evidence-20261107`의 `unit_tests/test_reverse_compare.py` unstaged 웹 fixture는 수정·스테이징하지 않았다. 원본 상태는 `M unit_tests/test_reverse_compare.py` 그대로다.

## 원본 → 통합 커밋

| 원본 SHA | 통합 SHA | 내용 |
|---|---|---|
| `5b65f34553159cb20689133ccaf648fc78776f43` | `5d6bbb22fe599e46cd230ae1508d9f76ddb3531e` | reverse v2 core |
| `c016b6e61fb407738141c7093b59f71cf97e2869` | `9f107705d323cfd22f70f146bb488bf5adc2d37d` | 비교 하네스 |
| `90bfefea1eb78dd805b938ae538defa72d541736` | `15273d024d711aa8c318787eb1d7fd845a1950c4` | basename 동작 교정·캡처 |
| `5c45fc0bbf0c638677a8c5dae2c416f5f0f4c877` | `a0f0a6c71b1611093d21bb5c08dde3093f2637e8` | API 증거 검증·직접 캡처 |
| `96becddb1170e6750560f2d9bd7f7f97bcbe2bc0` | `65cb7c0dd08befb7218fe2c509b21905b872e2e7` | 독립 E2E 대상 |
| `7266a2a19332935a6e32d10108a7e021956ef910` | `0051e863886f66586666a5f2d3849d56832477c0` | 독립 검증·제한 repair·응답 계약 |
| `df4ff746f0b2c4588a8b409dc153e7d1ffbb0f34` | `b9b3f76f9b51a2c6fb2229760008ebbfd84394c2` | verifier callback 멱등성 |

감사 경로만 별도 보존했다. `0e628ee11579a16e59f001650504c77fa45a58f0` → `cf367abdaaeeb5380f2287bf5346a8e22f20acc7` (`docs/reverse-platform/audit/**`), `dd83984d7cfc07fed2642af13e419e13821c6d9b` → `5389086a9e7887bc86460ae777925aabedb12a9c` (`docs/reverse-platform/audit-core-7266a2a1/**`). 감사 worktree의 제품 dirty diff는 이식하지 않았다.

통합 중 main의 caller pool 전달과 v2 시작 옵션이 겹친 충돌을 함께 보존했다. 후속 회귀에서 wrapper가 `pool`을 실제 시작 함수까지 전달하지 않음을 확인해 교정했다: `c31527e3581842af7c765dbc900afb8168a3e48b`. 기존 workspace/pool 경계 검사도 유지했다. 문서 실행 명령의 잘못된 API 테스트 파일명은 `tests/test_reverse_compare_api.py`로 수정했다: `3fa616e9a91bc5ba31fb69112fb4a9c1903478a9`.

## 검증 증거

DB를 사용하는 pytest 묶음은 실행 시작 단계에서 중단됐다.

```text
$ env -u TTAKKARI_TEST_DB PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py tests/test_reverse_compare_api.py tests/test_reverse_reconstruction.py tests/test_reverse.py unit_tests/test_reverse_static.py
ImportError while loading tests/conftest.py → pg_sandbox.start_cluster() → initdb
Command '['/opt/homebrew/opt/postgresql@17/bin/initdb', '-D', '/tmp/ttakkari-test-18300-e70d0f96/data', '-A', 'trust', '-E', 'UTF8', '--locale=en_US.UTF-8', '--no-sync']' returned non-zero exit status 1.
exit 4; pytest가 `CalledProcessError`의 stderr를 출력하지 않음.
```

`tests/pg_sandbox.py`의 `_run()`이 `capture_output=True`로 호출한다는 점을 확인하고 같은 지원 fixture 경로의 진단 호출로 stderr를 표시했다.

```text
$ env -u TTAKKARI_TEST_DB PYTHONPATH=src:tests ../back/.venv/bin/python -c 'import subprocess; from pg_sandbox import start_cluster; 
try:
    cluster = start_cluster()
except subprocess.CalledProcessError as exc:
    print("COMMAND:", repr(exc.cmd)); print("RETURN CODE:", exc.returncode); print("STDERR:", repr(exc.stderr)); raise SystemExit(exc.returncode)
else:
    print("TEMP CLUSTER STARTED:", cluster.port); cluster.stop()'
COMMAND: ['/opt/homebrew/opt/postgresql@17/bin/initdb', '-D', '/tmp/ttakkari-test-20613-2ca357df/data', '-A', 'trust', '-E', 'UTF8', '--locale=en_US.UTF-8', '--no-sync']
RETURN CODE: 1
STDERR: '2026-11-07 02:10:16.061 KST [20733] FATAL:  could not create shared memory segment: Operation not permitted\\n2026-11-07 02:10:16.061 KST [20733] DETAIL:  Failed system call was shmget(key=448412855, size=56, 03600).\\nchild process exited with exit code 1\\ninitdb: removing data directory "/tmp/ttakkari-test-20613-2ca357df/data"\\n'
```

분류: **현재 Codex command sandbox의 임시 PostgreSQL 초기화가 macOS shared-memory `shmget` 권한 거부로 실패**했다. 이전 worker 기록에는 exit 1만 있고 stderr가 저장되지 않아 이전 프로세스의 원시 stderr 자체는 확인할 수 없다. 현재 동일 fixture 경로에서 확보한 출력은 같은 실패 단계의 원인을 설명한다. 운영 PostgreSQL 상태를 나타내지 않는다. DB/API 회귀와 PostgreSQL을 쓰는 독립 core probe는 미검증으로 남겼다.

DB 비의존 검사와 lint:

```text
$ env -u TTAKKARI_TEST_DB PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py unit_tests/test_reverse_static.py
54 passed in 1.55s
$ ../back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py src/ttakkari/api/reverse.py src/ttakkari/reverse.py src/ttakkari/reverse_compare.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py tests/test_reverse_reconstruction.py tests/test_reverse_compare_api.py
All checks passed!
$ git diff --check
(출력 없음, exit 0)
```

pool 전달 교정은 단위 회귀를 먼저 추가해 RED를 확인했다 (`pool: work` 누락으로 `1 failed`), 교정 후 같은 테스트 `1 passed`; 전체 단위 묶음은 `54 passed`다. 넓은 범위로 모든 도입 Python을 한 번에 Ruff 검사했을 때 27개 lint finding이 보고됐다. 대상은 harness 스크립트·감사 probe·증거 fixture와 단위 테스트의 import 정렬 1건이었다. 단위 테스트 import 정렬은 교정했고 문서의 제품 코드·reverse 테스트 Ruff를 통과시켰다. 독립 감사 probe·하네스·캡처 자료는 변경하지 않았다.

## 남은 통합

- 이 통합 준비 브랜치는 push·배포하지 않았다. 배포 revision, 운영 동작, 실제 플랫폼 agent E2E는 미확인이다.

## 웹 증거 서버 연결 후속 (2026-11-07)

### 프로필 및 입력 커밋

- 저장소 접근 전에 `ttk get /api/sessions/7e594972-a53a-4a52-aedc-aba29faeb219/runs`에서 이번 실행 중인 Run `f16394e1-c8cb-4128-9ebe-c7fdc9ec140e`의 `profile`이 `회사 코덱스 ai2 (ai2@hyperez.io)`임을 확인했다. `ttk get /api/accounts`의 같은 이름은 `6b76a5c4-a622-4345-b7e6-fb9e76b74b9a`, `enabled=true`였다.
- 시작 HEAD `7b68e586136aa8fcbd58619f3e19d797c6d237e1`은 clean이었다. 준비 과정을 반복하지 않았다.
- 웹 원본 `e80062ea066c6265262f21068c67808920e73daf` → 통합 `373c6fe068d56e0d898582ae4754e1ebf323223d`; 모듈·캡처·증거·검사 21파일을 그대로 보존했다.
- 감사 원본 `3287181780f25cdf6ec3f8a746b6894f541d5273` → 통합 `4911c14e299f291a988bceb91cd119b09271f51d`; 감사 전용 두 파일만 보존했다.
- 이 후속의 core 코드·회귀 테스트 커밋은 `cb7a2ad3f1ad1ee514da4d7049554df89695caf4`다. 이 기록 반영은 별도 문서 커밋이다.

### 서버 연결과 계약

- `reverse_tasks.verify_comparisons`는 정확히 14개 입력 키를 받는 기존 계약을 유지하고, `target_type=web`의 단일 `web_capture`만 `verify_web_capture`로 재열어 재계산한다. 캡처 누락·중복, 다른 Run, 원시 JSON 또는 스크린샷 변경은 `unverified`에 머문다. 입력의 `result`와 시각 선언은 verdict에 사용하지 않는다.
- 서버 출력 comparison에 `functional_result`, `visual_result`, `visual_assessment`를 제공하고, capture ref·무결성·provenance를 evidence에 보존한다. 무결성이 확인되지 않은 시각 상세 선언은 결과에서 제거한다. 기능 `direct_comparison`과 별도 `visual_comparison`의 수치는 합산하지 않는다.
- 웹 작업은 기능과 시각 결과 모두 `match`여야 독립 검증을 통과한다. 시각 mismatch/unverified 및 기능 실패는 기존 구현 세션의 repair predicate로 가며 `attempts < max_repair_attempts`, source, observation 조건을 지킨다. verifier 비교·시각 요약은 구현자 결과와 별도 저장한다.
- 새 연결 반례는 구현 전에 실패했다: core 미연결 재계산 검사 `2 failed, 1 passed`; bounded visual repair 계약은 helper 부재로 `1 failed`. 구현 뒤 동일 연결 묶음과 검사 전체가 통과했다.

### 실행한 DB 비의존 검증

```text
$ PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_web_core_integration.py
7 passed
$ PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py unit_tests/test_reverse_static.py unit_tests/test_reverse_web_core_integration.py
61 passed in 1.56s
$ ../back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py src/ttakkari/reverse_web_evidence.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py unit_tests/test_reverse_static.py unit_tests/test_reverse_web_core_integration.py
All checks passed!
$ git diff --check
(출력 없음, exit 0)
```

저장된 캡처 두 건을 실제 통합 경로로 재검산했다. 둘 다 기능 mismatch·시각 mismatch였으며, 첫 사례는 15,169픽셀, 경계 사례는 15,295픽셀이 달랐다. 두 건 모두 integrity `verified`, provenance `unverified`였다. 개발자 데모는 ReverseJob 에이전트 E2E로 승격하지 않았다. 이전 웹 모듈 단위 8개는 변경 없이 반복하지 않았다.

### 미검증 및 후속 범위

- DB/API 및 manager 회귀와 `docs/reverse-platform/audit-core-7266a2a1/probes/test_core_audit.py`는 미검증이다. 이 Codex command sandbox의 기존 기록에서 `tests/conftest.py → pg_sandbox.start_cluster() → initdb` 단계가 `shmget ... Operation not permitted`로 실패했다. 지시에 따라 같은 초기화나 다른 엔진으로 재시도하지 않았다. 운영 DB 상태의 증거가 아니다.
- 실제 API 상세 응답·DB에 저장된 구현자/검증자 별도 라운드와 callback repair enqueue는 이 실행에서 미검증이다. DB 비의존 검사와 코드 경로 검토만 수행했다.
- 구현자/verifier 프롬프트와 캡처 작성자 안내, ZIP에 검증 웹 raw JSON·PNG·비교 manifest를 포함하는 확장은 이번 단위에서 하지 않았다. 기획의 결과 검토 후 별도 후속으로 연결한다.
- 원본 core의 unstaged `unit_tests/test_reverse_compare.py`는 수정하지 않았다. 필요한 웹 서버 재계산·별도 시각 판정 반례는 통합 준비본의 `unit_tests/test_reverse_web_core_integration.py`에 소유 테스트로 추가했다.

## 웹 캡처 안내와 안전한 ZIP 연결 (2026-11-07)

### 실행 신원·기준

- 저장소 접근 전 `ttk get /api/sessions/7e594972-a53a-4a52-aedc-aba29faeb219/runs` 결과를 `status=running`으로 필터링해 현재 Run `626a2fe1-9b63-4e0d-88df-7c1cd3061e74`와 프로필 `회사 코덱스 ai2 (ai2@hyperez.io)`를 확인했다. `/api/accounts`에서 같은 이름의 단일 계정 ID는 `6b76a5c4-a622-4345-b7e6-fb9e76b74b9a`였다. 다른 계정 전환은 하지 않았다.
- 시작 HEAD `9fb233bdb2f58c67ac4aee19c97d209e46b331cb`, 초기 worktree clean을 확인했다. 이전 61개 회귀와 모듈 8개는 반복하지 않았다.
- 구현 및 안내 변경 커밋: `f554f667f4c27f2692eb73fc9234999ff1f89325` (`reverse_tasks.py`, 소유 단위 테스트, reverse-platform harness/web-evidence 문서).

### 프롬프트·수집·아카이브

- 웹 구현/검증 프롬프트가 설정된 `ttakkari_browser`로 동일 입력·사전 상태·viewport·환경을 관찰하도록 안내한다. 검증자는 구현자 증거를 재사용하지 않고 자기 Run ID로 새 관찰·비교를 만든다. 직접 관찰·수기 전사·2차 조회·추론을 구분하고, 원본 부재/브라우저 오류는 `unverified`와 필요한 `needs_input`으로 남긴다.
- 프롬프트 명령은 실제 job 폴더와 준비본의 `back/.venv/bin/python`, checkout `src` 및 절대 CLI 스크립트 경로를 사용한다. job 경로와 인자는 `shlex.quote`로 인용한다. venv 심볼릭 링크를 풀면 Pillow 의존성을 잃는 것이 임시 CLI에서 확인되어, 프롬프트는 venv 링크 경로 자체를 보존한다.
- `collect_outputs`는 현재 Run과 저장된 최신 구현자/검증자 report를 모으고 각 역할의 Run/session ID를 `bundle`로 전달한다. ZIP에는 검증 후 재계산한 웹 비교 JSON, 그 raw capture, 원본·복제 PNG와 별도 `reverse-web-evidence-manifest.json`을 담는다. 원시 JSON의 상대 PNG 경로는 ZIP에서도 유지한다. 체크 로그는 `evidence/roles/{implementation,verifier}/checks/`에 역할별로 둔다. 비교 기능/시각 결과, 요약, 무결성, provenance 및 거부 사유를 구분한다.
- 아카이브 시 root 내부 경로를 descriptor 기반 `O_NOFOLLOW`로 열고 정규 파일·바이트 수·SHA-256을 재검사한다. 웹 raw JSON은 1 MiB, PNG만 별도 12 MiB이며 일반 파일 상한은 올리지 않았다. 경로 탈출·절대경로·symlink·민감·과대·SHA/크기 불일치·Run 불일치 및 캡처 후 변조는 포함하지 않고 manifest에 사유를 남긴다. ZIP 자체도 중복/비정상 경로·CRC·symlink를 다시 검사한다. 기존 14키 비교 입력과 실행/의존성/소스/테스트/CLI/API 자료는 보존한다.

### 확인한 결과와 경계

```text
$ PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py -k 'web_prompt_gives or bundle_rechecks_and_archives_role or bundle_refuses_web_evidence or bundle_manifest_records or bundle_refuses_symlink_as_zip or bundle_keeps_verified_png or bundle_does_not_include_web_capture or bundle_rejects_png or collect_outputs_bundles or test_zip_contains_sources or bundle_includes_comparison_manifest'
11 passed, 21 deselected in 0.61s
$ ../back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py unit_tests/test_reverse_tasks.py
All checks passed!
$ ../back/.venv/bin/python -m py_compile src/ttakkari/reverse_tasks.py
exit 0
$ ../back/.venv/bin/python /Users/winterholic/.claude/dev-experts-skills/dev-python/scripts/pitfall_scan.py src/ttakkari/reverse_tasks.py
total: 0 finding(s)
$ git diff --check
exit 0
```

- 프롬프트의 capture/verify 명령을 `/private/tmp/Reverse Web Job smoke 20261107`에서 직접 실행했다. capture CLI는 `.reverse/comparisons/cli-smoke-case-cli-smoke-run.web.json`을 만들었고 verify CLI는 기능 `match`, 시각 `mismatch`(192/192 pixels), integrity `verified`, provenance `unverified`를 출력했다. 입력 PNG와 관찰은 synthetic module fixture이며 설정 브라우저 관찰이나 ReverseJob E2E가 아니다.
- ZIP 회귀는 실제 ZIP을 열어 역할 manifest·비교 JSON·raw JSON·PNG를 확인하고, PNG SHA/바이트 및 디코딩, 역할별 로그, 경로·민감·크기·변조·symlink·잘못된 Run 반례를 확인했다. 1 MiB 초과 PNG는 별도 12 MiB 한도에서 포함되고 12 MiB 초과 PNG는 거부됐다.
- `ruff format --check`는 기존 두 파일 전체를 재포맷해야 한다고 출력해 적용하지 않았다(이 파일의 기존 포맷까지 대량 변경하면 범위 밖). 변경 영향 파일의 `ruff check`는 통과했다.
- 이전 기록의 Codex command sandbox `initdb → shmget ... Operation not permitted` 제한을 따라 DB/API 저장 및 dispatch 검증은 이번에도 실행하지 않았고 미검증으로 둔다. 실제 configured-browser 원본/복제 관찰과 실제 ReverseJob E2E도 이번 단위 범위 밖이라 미실행이다. push·배포하지 않았다.

## 구현자 실패가 verifier match로 가려지는 완료 판정 교정 (2026-11-07)

### 실행 신원·기준

- 저장소 접근 전 실행 중 Run의 profile은 `회사 코덱스 ai2 (ai2@hyperez.io)`였다. 같은 이름의 유일한 계정은 `6b76a5c4-a622-4345-b7e6-fb9e76b74b9a`다. 계정 전환·폴백·위임은 하지 않았다.
- 시작 HEAD `efd15f9224024768203018f5cce1c1dac2354feb`, worktree clean. 이전 웹 프롬프트·ZIP 안내 변경 `f554f667f4c27f2692eb73fc9234999ff1f89325`와 그 파일은 유지했다.
- 수정 범위는 `src/ttakkari/reverse_tasks.py`, `unit_tests/test_reverse_tasks.py`, 이 계약 및 통합 기록뿐이다. 감사 worktree·고정 probe·원본 core fixture·main·프론트는 수정하지 않았다.

### 원인과 교정

- 현재 callback의 입력은 verifier Run ID/role이고 `read_report()`가 그 verifier 전용 `.reverse/<run>.json`을 다시 검사한다. verifier 쪽 direct/visual 요약·raw report는 `verification_*`로 저장되지만 기존 `verifier_passed`는 verifier report만 검사했다. 따라서 저장돼 있던 구현자의 `direct_comparison`/`visual_comparison`이 mismatch 또는 unverified여도 verifier match가 job을 `done`으로 만들었다.
- 이 경로는 고정 9fb 감사 probe를 현재 `efd15f9` 통합 코드에서 실행해 재현했다. 기능 match+시각 mismatch, 구현 comparison unverified, repair 뒤 mismatch 유지의 세 callback 모두 이전에는 `done`이었다. 독립 감사 probe 자체는 revision `9fb233bdb2f58c67ac4aee19c97d209e46b331cb`를 출력한다. 이는 probe 기준 revision이지 현재 코드 revision 증거가 아니다.
- 최종 gate가 최신 구현/repair 회차의 `report.ready`, 검증된 source/execution, 직접 비교 1건 이상 전체 match, web 시각 비교 전체 match와 verifier 자신의 준비/필수 검사/직접 비교/시각 결과를 모두 요구하도록 했다. `independent_verification`은 verifier 자신의 verdict를 보존한다. 따라서 verifier가 자체 기준을 통과해 `passed`여도 구현 gate가 실패하면 job은 `partial`이다.
- 각 역할의 raw report·summary·checks·problems·비교 요약을 별도 필드로 보존한다. repair 입력은 최신 구현자와 verifier의 결함 자료를 함께 포함하며 기존 구현 세션 및 pool, `max_repair_attempts` 한도를 유지한다. 한도 소진 뒤 실패는 `partial`; 동일 구현/repair/verifier callback은 history·row·repair 횟수를 늘리지 않는다. analyze mode는 독립 검증 없이 분석 완료로 남는다.

### 회귀와 실행 결과

변경 전 callback 회귀에서 5개 완료 gate 반례가 `partial` 대신 `done`을 반환해 실패했다. 초기 실행 중 repair fixture 준비 오류 1건은 fixture를 보완한 후 구현 검증을 진행했다.

```text
$ PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_web_core_integration.py
..............................................                           [100%]
48 passed in 0.76s
$ PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_static.py
........................                                                 [100%]
24 passed in 0.27s
$ ../back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py unit_tests/test_reverse_tasks.py
All checks passed!
$ git diff --check
(출력 없음, exit 0)
$ ../back/.venv/bin/python -m py_compile src/ttakkari/reverse_tasks.py
exit 0
```

callback 회귀에는 웹 구현 기능 match/시각 mismatch, 구현 unverified, repair 후 mismatch 지속, 두 역할 모두 match, CLI/API direct mismatch와 direct 비교 0건, analyze mode, 구현·verifier 중복 callback을 포함한다. callback은 fake DB 경계이며 실제 transaction/API/dispatch 증거가 아니다.

고정 독립 probe를 재실행했다:

```text
$ PYTHONPATH=src ../back/.venv/bin/python /Users/winterholic/development/projects/winterholic-ttakkari/back-reverse-webcore-audit-20261107/docs/reverse-platform/audit-webcore-9fb233bd/probe_webcore.py
revision: 9fb233bdb2f58c67ac4aee19c97d209e46b331cb
implementation_visual_mismatch: partial expected=partial; implementation_visual_unverified: partial expected=partial
implementation_visual_mismatch_after_repair: partial expected=partial; attempts=1; fresh verifier started
database_api_dispatch: unverified: fake DB, no API/DB or generation dispatch
```

그 probe의 `duplicate_callback.status_unchanged` 값은 `job.status == "done"`으로 고정된 오래된 기대값이라 이제 `partial`에서 false로 표시된다. `result_unchanged=true` 및 `no_rows_added=true`는 유지되고, 소유 callback 회귀가 실제 이전 상태와 비교해 status/history/rows/repair 횟수의 불변을 확인한다.

### 정적 검사·미검증·다음 범위

- Ruff 첫 실행에서 새 테스트 fixture의 미사용 import와 mutable class list 4건이 나와 fixture를 고쳤다. 변경 영향 Ruff, `git diff --check`, `py_compile`은 위 출력처럼 통과했다.
- PostgreSQL/API 저장·dispatch는 기존 `initdb → shmget ... Operation not permitted` 제한 때문에 재시도하지 않았다. 실제 ReverseJob E2E·브라우저 provenance·generation dispatch는 미검증이다. worker f61의 고정 efd15f92 ZIP/배포 경로 감사 가설은 범위 밖이며 수정하지 않았다.
- 다음 단계는 독립 재감사다. 이번 단위에서는 push, deploy, 실제 ReverseJob E2E를 하지 않았다.
