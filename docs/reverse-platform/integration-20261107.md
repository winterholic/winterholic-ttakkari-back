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

- 웹 worker `20ce553e` 결과를 받은 뒤 웹 신규 파일과 기존 비교기 연결을 별도 통합·검증한다.
- DB/API 회귀와 `docs/reverse-platform/audit-core-7266a2a1/probes/test_core_audit.py`는 권한 있는 임시 PostgreSQL 실행 환경에서 다시 검증한다.
- 이 통합 준비 브랜치는 push·배포하지 않았다. 배포 revision, 운영 동작, 실제 플랫폼 agent E2E는 미확인이다.
