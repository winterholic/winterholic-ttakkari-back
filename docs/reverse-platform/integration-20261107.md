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

## Release 캡처 명령 경로와 FIFO 아카이브 읽기 교정 (2026-11-07)

### 실행 신원·직전 기준

- 저장소 접근 전 `ttk get /api/sessions/7e594972-a53a-4a52-aedc-aba29faeb219/runs`의 현재 running Run은 `480438b5-ee23-4d5e-8e33-fbdcdb372dcf`, profile은 `회사 코덱스 ai2 (ai2@hyperez.io)`였다. `/api/accounts`의 같은 이름 단일 ID는 `6b76a5c4-a622-4345-b7e6-fb9e76b74b9a`다. 계정 토글·폴백·하위 위임은 하지 않았다.
- 시작 revision `f0a382ec733022c4e5ddd2cb3540b24df96fa067`은 clean이었다. 바로 앞 `fix(reverse): gate completion on implementation evidence` 완료 gate 커밋과 해당 계약·테스트·기록을 유지했다. 이전 gate 검사 전체는 반복하지 않았다.
- 기준 감사는 `efd15f9224024768203018f5cce1c1dac2354feb`; 감사 자료 커밋은 `25c1c5b215a4515956969277bd11e0dd02450728`이다. 독립 감사 worktree와 probe 파일은 수정하지 않았다.

### 실패 재현과 교정

- 감사의 배치 결함은 `_prompt`가 interpreter를 항상 `<checkout>/../back/.venv/bin/python`으로 만들고, scripts/PYTHONPATH는 `reverse_tasks.py`가 속한 checkout 또는 release root에서 찾는 불일치다. 고정 `efd15f92` 소스를 모의 `release/<sha>`에 두고 그 root의 `.venv`와 `scripts/`를 준비해 이전 prompt를 호출했다. prompt가 잘못 계산한 `releases/back/.venv/bin/python`을 실행하며 exit 127 및 `No such file or directory`를 반환했다. release packaging은 `Releases.archive()`가 tracked 소스/scripts를 release root로 추출하고 runtime은 그 root의 `.venv`를 제공하는 배치다.
- `_prompt`가 현재 실행 프로세스의 `sys.executable`을 쓰고 같은 모듈 위치에서 capture/verify scripts와 `src`를 찾게 했다. venv symlink 경로를 resolve하지 않는다. web-evidence 지침에 개발 checkout 및 immutable release 경로와 이 이유를 기록했다.
- FIFO는 descriptor `open(O_RDONLY)` 단계에서 대기한 뒤 `fstat`에 도달하지 못했다. bounded reader의 최종 파일 open에만 `O_NONBLOCK`을 추가해 FIFO를 열더라도 즉시 `not a regular file`로 거부한다. containment, descriptor별 `O_NOFOLLOW`, 정규 파일 검사, 1 MiB 일반 한도, SHA/byte 재검산과 PNG 전용 12 MiB 한도는 그대로다. source, check log, 웹 raw JSON/PNG의 archive read는 기존 동일 `_bounded_evidence_bytes`를 사용한다.

### 좁은 회귀 및 ZIP 감사 결과

테스트 우선 반례는 수정 전 실패했다.

```text
$ PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py -k 'web_prompt_gives_role_specific or prompt_capture_and_verify_commands_run_for_both_roles or bundle_rejects_fifo_without_blocking'
4 failed, 40 deselected; FIFO child raised TimeoutExpired after 2 seconds
```

모의 release의 고정 과거 prompt 실행은 다음을 반환했다.

```text
revision: efd15f9224024768203018f5cce1c1dac2354feb
expected: release/<sha>/.venv/bin/python; emitted: release-parent/back/.venv/bin/python
exit: 127; stderr: No such file or directory
```

수정 뒤 같은 좁은 프롬프트/FIFO 묶음은 `4 passed, 40 deselected`다. 이 안에서 공백이 든 작업 폴더에 개발 checkout과 모의 `release/<sha>` 배치를 만들어 구현자·검증자 각각의 capture와 verify CLI를 직접 실행했다. 네 capture/verify 조합이 모두 exit 0이었다. release mock의 `.venv` 링크는 기존 의존성 환경을 가리킨 채 사용했고 Pillow import를 포함해 두 스크립트가 동작했다.

고정 ZIP 감사 probe의 제품 경로를 그대로 불러오고 오래된 baseline HEAD assertion만 건너뛰어 독립 재검산했다. 양 역할 raw JSON·PNG·comparison·check log를 ZIP에서 추출하고 manifest SHA/크기를 다시 계산했으며, functional `match`, visual `mismatch`, integrity `verified`, provenance `unverified`를 유지했다. 잘못된 Run ID와 캡처 후 PNG 변조는 거부됐다. hostile file 검사에서 정확히 1 MiB 일반 파일은 포함, 1 MiB 초과는 거부됐고 traversal·절대경로·symlink·`.env`·directory를 거부했다. FIFO subprocess는 timeout 없이 반환했다.

```text
$ PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py -k 'web_prompt_gives_role_specific or prompt_capture_and_verify_commands_run_for_both_roles or bundle_rejects_fifo_without_blocking or bundle_rechecks_and_archives_role_separated_web_evidence or bundle_refuses_web_evidence_with_tampered_sha_wrong_run_or_symlink or bundle_manifest_records_traversal_absolute_symlink_size_and_secret_rejections or bundle_keeps_verified_png_over_one_mib_under_png_specific_limit or bundle_does_not_include_web_capture_mutated_after_creation_or_symlinked or bundle_rejects_png_over_web_specific_twelve_mib_limit or collect_outputs_bundles_implementation_and_verifier_reports_separately or test_zip_contains_sources_but_excludes_target_and_secrets or bundle_refuses_symlink_as_zip_destination'
13 passed, 31 deselected in 1.41s
$ ../back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py unit_tests/test_reverse_tasks.py
All checks passed!
$ git diff --check
exit 0 (no output)
```

### 미검증·다음 단계

- DB/API 저장·dispatch, 실제 configured-browser provenance, ReverseJob 운영 E2E는 미검증이며 실행하지 않았다. `initdb shmget` 및 `.vite` 삭제 거부를 반복하거나 우회하지 않았다.
- 운영 runtime은 변경하지 않았다. push·배포를 하지 않았다. 이 커밋 뒤 고정 revision 독립 재감사와 정상 배포가 다음 단계다.

## 189 고정 감사 및 최신 origin/main 보존 통합 (2026-11-07)

### 시작 기준·원격 통합

- 작업 전 지원 API에서 이 Run의 `running` profile은 `회사 코덱스 ai2 (ai2@hyperez.io)`였고, `/api/accounts`의 동일 이름 단일 계정 ID는 `6b76a5c4-a622-4345-b7e6-fb9e76b74b9a`였다. 다른 계정 전환·폴백·위임은 하지 않았다. GitHub 지원 조회는 `winterholic`과 `daeseon-dev`를 반환했고, 이 저장소 fetch/push에는 per-command `winterholic` 자격을 사용했다.
- 출발 HEAD `18917626934ab38302bddfee1c7f4b9ebe6dde73`, 초기 worktree clean, `f0a382ec733022c4e5ddd2cb3540b24df96fa067`는 출발 HEAD의 조상이다. 기존 브랜치는 출발 시 `origin/main` 대비 ahead 21 / behind 32였다.
- 통합 전 `origin/main` SHA는 `b56057c03c944d93cbd08857318955cc9cbeac1c`였다. 실행 명령과 출력:

```text
$ GH_AS_USER=winterholic GH_AS_TOKEN="$(gh auth token --hostname github.com --user winterholic)" git -c credential.helper= -c 'credential.helper=!f() { echo username=$GH_AS_USER; echo password=$GH_AS_TOKEN; }; f' fetch origin main
From https://github.com/winterholic/winterholic-ttakkari-back
 * branch main -> FETCH_HEAD
$ git merge --no-edit origin/main
Merge made by the 'ort' strategy.
36 files changed, 6783 insertions(+), 70 deletions(-)
```

- 일반 merge 결과 SHA는 `43238a8a76fa094c7552a73258fceb941f353793`, 부모는 `18917626934ab38302bddfee1c7f4b9ebe6dde73` 및 `b56057c03c944d93cbd08857318955cc9cbeac1c`다. 충돌은 없었다. 이 시점의 `git rev-list --left-right --count HEAD...origin/main`은 `22 0`이며 `origin/main`은 merge 결과의 조상이다.

### 고정 감사 대조 및 보존 파일

- 통합된 원격 변경은 ArtCraft account pin을 위해 `ChatSession.account_id`와 manager 선택 경로를 추가하고, runtime lease mapping 보고를 보강한다. Reverse 생성 경로는 기존 caller `pool`을 `queue_run`까지 전달하며 `account_id`를 지정하지 않아 기본 `None`을 쓴다. 원격의 pin 분기는 `account_id is not None`일 때만 적용된다. 고정 감사의 callback 완료 판정, pool 경계, release 경로 및 FIFO 가정은 이 변경으로 달라지지 않는다. Reverse 파일/전용 테스트는 `189...`와 merge 결과 사이에서 변경되지 않았다. remote main 쪽 ArtCraft/manager 변경의 직접 회귀는 아래에서 별도로 실행했다.
- 감사 원본은 제품 파일을 가져오지 않고 `docs/reverse-platform/audit-integrated-18917626/`에 보고서, 보완 기록, 기획 신원 JSON, 두 raw probe JSON을 보존했다. 원본과 복사본의 SHA-256이 이 5개 자료에서 모두 일치했다. 재현 스크립트는 원래 독립 감사 worktree에 그대로 두었다. 기존 `docs/reverse-platform/audit/`, `audit-core-7266a2a1/` 등 과거 감사 경로는 삭제하지 않았다. push 게이트 Ruff에서 검출된 미사용 import/noqa·변수명·정렬만 정리했으며 probe assertion이나 감사 범위는 바꾸지 않았다.
- 고정 감사의 결과를 이 단위에서 재사용한 경계: DB 비의존 callback positive/negative control, release/dev capture·verify 8회 exit 0, FIFO 4/4 timeout 없음. PNG 12 MiB 한계는 감사 보완에 기록된 이전 `efd15f92` 결과를 그대로 재사용했다. DB/API 저장·dispatch 및 운영 ReverseJob E2E는 계속 미검증이다. `initdb shmget` 및 `.vite` 삭제 거부는 재시도·우회하지 않았다.

### 이번 merge 후 실행한 검사

```text
$ PYTHONDONTWRITEBYTECODE=1 TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py unit_tests/test_reverse_static.py unit_tests/test_reverse_web_core_integration.py unit_tests/test_reverse_web_evidence.py
90 passed in 3.18s
$ PYTHONDONTWRITEBYTECODE=1 TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src ../back/.venv/bin/python -m pytest -q unit_tests/test_artcraft_tasks_unit.py unit_tests/test_artcraft_update.py unit_tests/test_collection_runtime_report.py
73 passed in 5.70s
$ ../back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py src/ttakkari/reverse_web_evidence.py src/ttakkari/api/reverse.py src/ttakkari/runs/manager.py src/ttakkari/operations/inline.py src/ttakkari/artcraft.py src/ttakkari/artcraft_catalog.py src/ttakkari/artcraft_tasks.py src/ttakkari/artcraft_update.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py unit_tests/test_reverse_static.py unit_tests/test_reverse_web_core_integration.py unit_tests/test_reverse_web_evidence.py unit_tests/test_artcraft_tasks_unit.py unit_tests/test_artcraft_update.py unit_tests/test_collection_runtime_report.py
All checks passed!
$ git diff --check
exit 0 (no output)
```

단위 검사는 DB 비의존이다. pre-push 게이트에서는 변경된 DB/API 계약 검사도 격리 test DB로 실행됐다. production DB/API·generation dispatch·운영 상태 검사는 수행하지 않았다. 이전 임시 PostgreSQL 초기화의 `pg_sandbox.start_cluster() → initdb → shmget: Operation not permitted` 제한을 반복하거나 우회하지 않았다.

### 최종 푸시·배포 후속

- 제품 및 통합 커밋 SHA: `6dbc20ba70af9e32fa3806298d42f47dbe36589d`. 실행 명령·결과:

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-189-integration GH_AS_USER=winterholic GH_AS_TOKEN="$(gh auth token --hostname github.com --user winterholic)" git -c credential.helper= -c 'credential.helper=!f() { echo username=$GH_AS_USER; echo password=$GH_AS_TOKEN; }; f' push origin HEAD:main
사전 검증 통과: contract 87 passed, changed tests 91 passed, Ruff 통과
b56057c0..6dbc20ba  HEAD -> main
$ git rev-parse HEAD && git rev-parse origin/main
6dbc20ba70af9e32fa3806298d42f47dbe36589d
6dbc20ba70af9e32fa3806298d42f47dbe36589d
```

- 같은 full SHA로 `request_backend_deploy`를 호출했다. 반환: reservation ID `27cb9619-68cc-42a5-b944-be72a1463b2b`, revision `6dbc20ba70af9e32fa3806298d42f47dbe36589d`, status `requested`, `skip_tests=false`, strategy `generations`. 수락은 배포 성공이 아니며 상태를 폴링하지 않았다.
- 이번 푸시의 integration 문서 기록 커밋 및 원격 결과는 이 절 다음의 문서 전용 변경으로 남긴다. backend 배포 후보는 위 제품 SHA다. production DB/API·실제 ReverseJob agent E2E·운영 health는 배포 뒤 별도 실행 단위다.

## Catalog seed 배포 게이트 fixture 후속 (2026-11-07)

- 지원 배포 로그에서 예약 `27cb9619-68cc-42a5-b944-be72a1463b2b`는 revision `6dbc20ba70af9e32fa3806298d42f47dbe36589d`의 release pytest 단계에서 실패했다. 실제 실패는 `tests/test_catalog_seed_readiness_path.py::test_system_seed_promotion_rejected_and_user_scope_promotion_preserved_across_sync`이며, `src/ttakkari/research/service.py:1049`의 `best_result_metric_not_measured_on_selected_run`이다. 컨트롤러 결과는 `1 failed, 871 passed, 2 skipped`였다. 운영 revision `529022861120e86f80400ffaec53dd3f3f7f81c6`은 유지됐다.
- 실패 fixture는 `Run(status="succeeded")`만 만들고 측정값을 넣지 않은 채 `adopt_best_result(... primary_metric="decode_tps")`를 호출했다. 추가로 Experiment의 `primary_metric`은 기본값 `recall_at_k`에 맡겨져 있었다. 서비스는 선택 Run·Experiment·Configuration·요청 primary metric으로 Measurement를 조회하고 유한 값을 요구한다. fixture 계약 불일치가 원인이라는 것은 코드 대조에 따른 추정이며 후속 컨트롤러 GREEN 전까지 확정하지 않는다.
- fixture는 Experiment의 primary metric을 `decode_tps`로 명시하고 같은 Experiment·Configuration·succeeded Run을 참조하는 유한 `Measurement(metric_key="decode_tps", value=42.0)`를 추가한다. system seed 직접 승격 거부, BestResult 채택, user-scope 승격 및 catalog sync 보존의 기존 assertion은 유지했다. 제품 service와 모델은 수정하지 않았다.
- 직전 Codex command sandbox Run `a94b4a34-f071-4702-9638-fec98b12dc46`에서 수정 전 단일 테스트를 실행했으나 `tests/conftest.py → pg_sandbox.start_cluster() → initdb` 단계의 `CalledProcessError` exit 1로 pytest가 수집 전 중단됐다. 출력에는 initdb stderr가 없었다. 이 실행에서 `shmget` 오류를 관측했다고 기록하지 않는다. 이번 준비에서도 해당 DB 초기화를 유발하는 검사는 재실행하지 않았다.
- 657 준비본 기획 독립 검토는 실제 7369 두 파일 diff와 서비스의 측정값 query·모델 연결을 대조하고, 기존 assertion이 유지됐음을 확인했다. 제품 service/model 변경은 없으며 DB/API GREEN은 검토 범위에도 포함되지 않았다.
- 이번 통합 시작은 clean `7369a08346b1d51e638673c3798b71f0530e405c`, 부모 `7a10d0ccf71c9f3e5ce7f2314bc3db5c1513517c`였다. 일반 fetch 뒤 `origin/main`은 `7a10d0ccf71c9f3e5ce7f2314bc3db5c1513517c`로 동일했고 `git rev-list --left-right --count HEAD...origin/main`은 `1 0`이었다. 원격의 새 커밋이 없어 추가 merge는 없었다. 실제 `git diff origin/main..HEAD`는 이 fixture와 통합 문서 두 파일만 포함한다.
- 첫 표준 push는 Ruff에서 import 순서와 기존 fixture adapter class list의 `RUF012` 12건으로 실패했다. assertion·값·동작을 바꾸지 않고 import를 정렬하고 해당 class 속성에 `ClassVar[list[str]]` 타입만 선언했다. 재시도한 표준 pre-push는 Ruff 통과, contract `87 passed`, 변경 테스트 파일 `34 passed`였고 각 DB reset은 selective 87/34, forced full/fallback 0이었다. 우회 옵션이나 테스트 생략은 사용하지 않았다.
- 정상 push 출력은 `7a10d0cc..97f6f34d HEAD -> main`이며, push 직후 `git rev-parse HEAD`와 `git rev-parse origin/main` 모두 `97f6f34de23bb47c6d58a1db4b325705a2168c24`였다. 해당 SHA는 `7a10d0ccf71c9f3e5ce7f2314bc3db5c1513517c`의 후손이며 원격 신규 커밋은 없었다.
- `request_backend_deploy(revision="97f6f34de23bb47c6d58a1db4b325705a2168c24")` 수락 응답은 reservation `e00ecfd5-76ac-45e6-9678-5b445b50f043`, `skip_tests=false`, strategy `generations`, status `requested`다. 예약 상태를 기다리거나 폴링하지 않았으므로 배포 성공은 확인하지 않았다.
- 이전 Codex 직접 pytest/conftest/initdb 실행은 반복하지 않았다. 표준 pre-push의 변경 파일 DB 테스트는 위와 같이 통과했지만 운영 DB, 운영 health, 실제 CLI/API/웹 ReverseJob E2E는 미검증이다. fixture 원인은 여전히 코드 대조에 따른 추정이며 컨트롤러 release GREEN이 확인되기 전까지 확정하지 않는다.

## Scope merge release fixture 후속 (2026-11-07)

- 작업 전 현재 실행 Run profile `회사 코덱스 ai2 (ai2@hyperez.io)`와 동일 이름의 유일 enabled 계정 `6b76a5c4-a622-4345-b7e6-fb9e76b74b9a`를 확인했다. 시작 HEAD와 `origin/main`은 clean `eafb7176fe748110791ad9770f28744f3c71b089`였다.
- 지원 `ops_deploy_log(0131e8cf-ebf6-4ee4-9b6c-a5a4f66deaf3, summary)`의 실제 release pytest RED는 후보 revision `72116f9fc75b2aa785651eae7aa8814bce60be88`에서 발생했다. `tests/test_collection_scope_merge.py::test_operational_merge_dry_run_rolls_back_and_apply_is_idempotent`가 `scope_merge.operate()`의 `schema_not_ready`에서 실패했고 결과는 `1 failed, 1000 passed, 9 skipped, 6 subtests passed in 145.52s`였다.
- 실패 후보의 `deploy/generation-contract.json`은 `schema_revision=0077_artcraft_account_pin`이고 같은 revision의 `_operation_generation()` 기본값은 `0076_kb_v3_collection_merge`였다. 테스트는 `operate()`까지 호출하고 제품은 alembic head가 `Releases.contract(...)["schema_revision"]`와 정확히 같아야만 진행한다. 따라서 성공 fixture가 구버전 값을 넣은 것이 직접 원인이며 제품 변경은 필요하지 않았다.
- `_operation_generation()` 기본값과 draining/standby/offline executor/inactive 거부 사례를 모두 현재 revision `0077_artcraft_account_pin`으로 맞췄다. 실제 구버전 schema 거부 `0073_research_reuse_source`, `0075_collection_research_merge`는 유지했다. dry-run rollback, apply 재실행 멱등성, unresolved apply 전체 rollback, cutover control-row lock 및 기존 결과 검증 assertion은 삭제·완화하지 않았다.
- 변경 전 RED는 위 예약의 실제 release 로그와 실패 후보의 고정 Git tree를 근거로 기록했다. 이 Codex command sandbox의 알려진 `tests/conftest.py → pg_sandbox.start_cluster() → initdb → shmget ... Operation not permitted` 경로는 재실행하지 않았다.
- fixture와 이 기록을 소유한 커밋은 `3c038cf757bcebb1bedad03afbe6bee2d2f786c1`이다. 실행한 표준 검증:

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-scope-merge-20261107 ./scripts/verify_deploy_gate.sh --commit HEAD --base origin/main
대상 커밋: 3c038cf757bc
ruff (변경된 파이썬 1개): 통과
계약 묶음: 87 passed, 6952 deselected in 16.33s; db reset 87 selective, forced full/fallback 0
바뀐 테스트 파일: 13 passed in 1.10s; db reset 13 selective, forced full/fallback 0
사전 검증 통과: 계약 묶음과 바뀐 테스트 파일 범위만 확인했습니다. 전체 통과를 뜻하지 않습니다.
```

- 이 검증은 Codex 표준 deploy gate의 계약 및 변경 파일 범위다. 예약의 release 전체 pytest, 운영 DB/health, 실제 배포 결과는 이 실행으로 검증하지 않았다.

### Push gate 진단·교정

- 첫 일반 push에서 pre-push 게이트가 `/Users/winterholic/.cache/uv` 초기화를 `Operation not permitted`로 거부했다. 게이트를 생략하지 않고 `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-189-integration`으로 허용된 cache 경로를 지정해 다시 실행했다.
- 첫 게이트 실행은 코드 변경 32개 Ruff, 계약 묶음(`86 passed, 1 failed`), 변경 파일 검사(`82 passed, 2 failed`)에서 멈췄다. 계약 실패는 reverse PATCH 두 라우트의 `ROUTE_SCOPE` 누락이었다. 변경 파일 실패는 release CLI fixture가 sibling checkout의 `back/.venv`를 가정한 점과, DB/API 테스트가 완료 callback 뒤 같은 Run에 파일을 추가해 재호출한 점이었다. 같은 Run의 늦은 callback은 의도된 멱등성으로 무시되므로, 테스트를 새 후속 Run과 v2 raw output 스키마에 맞게 보완했다.
- Ruff 실패는 새 제품 결함이 아니라 통합된 역방향 감사·fixture 스크립트들의 import 정렬, 미사용 이름, shebang 실행 비트 및 테스트 subprocess 옵션이었다. 동작·assertion을 바꾸지 않는 정리만 적용했다.
- 교정 후 실행: reverse DB 비의존 회귀 `90 passed in 2.93s`; 원격 ArtCraft/manager 회귀 `73 passed in 5.62s`; API/callback, release fixture, route inventory 집중 검사 `4 passed in 1.51s` (DB reset 2 selective, fallback 0). 상세 pre-push 전체 결과와 최종 push SHA는 아래에 추가한다.

## e00 precise evaluator 상태 assertion 후속 (2026-11-07)

- 지원 `ops_deploy_log(e00ecfd5-76ac-45e6-9678-5b445b50f043, summary)`에서 revision `97f6f34de23bb47c6d58a1db4b325705a2168c24`의 release pytest RED를 확인했다: `tests/test_collection_mission_stage2.py::test_validated_semantic_axis_rescues_a_lexical_hold_and_is_recorded_everywhere`, assertion line 63에서 item engine `unavailable` 기대와 `waiting` 실제가 달랐다. 결과는 `1 failed, 931 passed, 8 skipped`. 이는 위의 별도 catalog fixture failure(예약 `27cb9619`, revision `6dbc20ba`)와 다른 실패다.
- 값의 경로: `runner.consume_once()`는 source job 실행 후 `precise_eval.consume_pending_once()`도 한 건 소비한다. 테스트는 collector pipeline handler를 호출하지만 `collector_precise_runtime.install()`은 하지 않는다. 미등록 runtime은 `evaluate_document_guarded()`에서 `EvaluatorUnavailable`가 되고, 소비 경로는 상태를 `pending`으로 보존하면서 item의 `engine.status="waiting"`와 예외 detail을 기록한다. `view_engine_status()`는 별도로 현재 전역 등록 상태를 읽어 mission summary의 engine을 `unavailable`로 유지한다.
- 등록 경계는 `generation_service._collector_consume()`와 `_collector_control()`에서 `consume_once()`보다 먼저 `collector_precise_runtime.install()`을 호출하는 것이다. API lifespan/test fixture에서 이 등록을 자동 수행하지 않으며, test settings는 collector control을 끈다. 이는 테스트 실행 경로가 startup worker와 다르다는 코드 근거다. 현재 backend 코드로 운영 원인을 확정하지 않는다. 다른 테스트의 전역 `_runtime` 잔존은 가능한 오염 요인이지만 이번 RED 값은 직접 소비 경로만으로 설명되므로 root cause로 단정하지 않는다.
- 관련 테스트에는 autouse 격리 픽스처를 추가해 시작 때 runtime을 명시적으로 비우고 teardown에서 이전 값을 복원했다. Semantic rescue·envelope·mission summary·pending count 및 precise pending 보존 assertion은 유지했다. item assertion은 “미등록 evaluator를 실제 한 번 시도한 뒤에도 pending이며, 정확한 detail을 가진 waiting”으로 고정했고 runtime 등록 별도 테스트는 유지했다. 제품 API, envelope, mission summary 및 evaluator 런타임은 수정하지 않았다.
- 수정 전 RED 증거는 위 지원 release log다. 이전 직접 `env -u TTAKKARI_TEST_DB ... pytest/conftest/initdb` 시도는 반복하지 않았다. 수정 후 표준 스크립트 `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-e00-precise ./scripts/verify_deploy_gate.sh --commit HEAD --base origin/main`은 Ruff 통과, 계약 `87 passed, 6952 deselected`, 변경 파일 `4 passed`로 exit 0이었다. 선택 DB reset은 계약 87회·변경 파일 4회 모두 selective, full/fallback 0이었다. 이건 소유 테스트 GREEN이며 release 전체 GREEN을 뜻하지 않는다.
- 재통합 전 `origin/main=3cce2588fff1a53c161d5a480498a81957d298ff`; 보존 merge 커밋 `cb26faa6b4cb037df0a24e711c14567a9847a5e3`의 부모는 수정 커밋 `c3e9b1893f12d141a81b110dc96cfaa61c4df0f7`와 원격 SHA다. `origin/main..HEAD` 변경 파일은 지정 테스트와 이 문서뿐이며 `git diff --check`는 출력 없이 통과했다. 일반 push와 `skip_tests=false` 예약 결과는 성공 후 이 절에 추가한다. 운영 E2E 및 신규 release 전체 GREEN은 확인 전까지 미확인이다.
- 정상 push의 pre-push 출력은 Ruff 통과, 계약 `87 passed, 6952 deselected`, 변경 테스트 `4 passed`이며 DB reset은 선택적 87/4, full/fallback 0이다. 푸시는 `3cce2588..72116f9f HEAD -> main`; push 직후 HEAD와 origin/main은 모두 `72116f9fc75b2aa785651eae7aa8814bce60be88`이고 worktree clean이었다.
- 해당 full SHA 예약 응답: ID `0131e8cf-ebf6-4ee4-9b6c-a5a4f66deaf3`, `skip_tests=false`, strategy `generations`, status `requested`. 이는 예약 수락이며 배포 성공을 뜻하지 않는다. 상태를 기다리거나 폴링하지 않았다. 이 기록을 위한 후속 doc-only 변경은 별도 SHA로 정상 push하고, 예약 대상은 위 제품·테스트 SHA 그대로 둔다.
- 검증 범위는 표준 계약 게이트와 변경된 소유 테스트 파일이다. 직접 `env -u TTAKKARI_TEST_DB ... pytest/conftest/initdb`는 재실행하지 않았다. 새 release 전체 GREEN, 운영 evaluator 상태, 운영 E2E는 미실행·미확인이다.
