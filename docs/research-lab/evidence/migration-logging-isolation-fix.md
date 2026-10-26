# 배포 gate: programmatic migration의 로그 격리 (2026-10-26)

기준 backend `bc0889f060b794738b2fd1a0dace34eff1f699d6`(누적 main77c1238a 포함). 이번 수정은 `tests/test_research_v2_migration.py`의 시험 helper와 회귀 검사만 바꾼다. 제품 코드, Alembic env/INI, 마이그레이션 및 전송 검사 통과 기준은 바꾸지 않는다.

## 배포 환경에서 관측한 실패

`backend_deploy_status(2f8896ee-fc2e-4af9-8693-6ebc5fbf179e)`와 지원 `ops_deploy_log` summary에서 revision `77c1238a2b8ade01c096dc74b4f4da3a52c89849`의 release-stage pytest 실패를 확인했다. `1 failed, 3161 passed, 11 skipped, 2 xfailed, 3 warnings, 6 subtests passed in 944.18s`이며 실패는 `test_raw_diagnostics_report_received_and_saved_file_without_logging_token`이다. HTTP201, 수신 바이트, 실제 JPG 저장 검사는 이미 통과했고 진단 이벤트 집합이 empty였다. 운영 파일 저장 장애를 입증하는 출력은 아니다. 이전 d371의 method shadow 실패와 다른 원인이다.

## 재현과 원인

Codex 명령 환경에서 표준 Run DB의 URL 및 실제 SELECT 결과가 `ttakkari_test / ttk_test / loopback`임을 단언한 뒤 실행했다. DSN/password는 출력하지 않았다.

```sh
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short tests/test_transfers.py -k raw_diagnostics -x
# 6 passed, 23 deselected in 0.79s

PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short tests/test_research_v2_migration.py tests/test_transfers.py::test_raw_diagnostics_report_received_and_saved_file_without_logging_token -x
# RED: 1 failed, 2 passed in 1.00s; required event names <= set() fails
```

`get_alembic_config()`가 `Config("alembic.ini")`를 쓰므로 같은 pytest 프로세스에서 실행한 Alembic env가 `fileConfig(config.config_file_name)`을 호출한다. 설치된 Python의 실제 signature는 `fileConfig(fname, defaults=None, disable_existing_loggers=True, encoding=None)`다. INI에는 root/sqlalchemy/alembic만 있어 기존 application logger가 비활성화된다. 이후 caplog의 level 설정은 disabled 상태를 되돌리지 않는다. 이 경로는 migration→transfer 순서의 실제 실패와 일치한다.

마이그레이션 후 `uvicorn.error.transfer`에 marker를 기록해 caplog에서 관측하는 새 회귀 검사를 제품 변경 전에 추가했다.

```sh
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short tests/test_research_v2_migration.py::test_programmatic_migration_preserves_application_diagnostic_logging
# RED: 1 failed in 0.67s; marker not present in caplog.records
```

## 최소 교정과 GREEN

시험 helper만 programmatic `Config()`와 repo 절대 `script_location`을 사용하게 했다. 기존 Settings DB URL 우선순위를 유지한다. config_file_name이 없어 CLI logging 초기화를 실행하지 않으며 실제 migration 경로와 0072/0073 roundtrip·v1 보존·SET NULL 검사는 그대로 실행한다. 운영 CLI 로그 설정을 바꾸거나 caplog에 가짜 기록을 넣지 않는다.

```sh
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short tests/test_research_v2_migration.py tests/test_transfers.py -x
# GREEN: 32 passed in 1.93s
```

ruff가 기존 Alembic import를 project first-party로 분류한 I001을 정리한 뒤, 최종 파일에서 새 logging 회귀→실제 upload 진단 순서를 다시 실행했다.

```sh
.venv/bin/ruff check tests/test_research_v2_migration.py
# All checks passed!
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short tests/test_research_v2_migration.py::test_programmatic_migration_preserves_application_diagnostic_logging tests/test_transfers.py::test_raw_diagnostics_report_received_and_saved_file_without_logging_token
# 2 passed in 0.65s
git diff --check
# exit0
```

기존 Research174/contract84/frontend75/build/lint 및 실UI/runner 증거는 해당 코드가 그대로여서 범위 내 재사용한다. 새 전체 회귀는 필수 controller gate가 담당한다. 이 32/2건은 전체 배포 성공·Metal·운영 MCP의 증거가 아니다. 다음은 clean commit/normal main push/full SHA 예약이며 `skip_tests=false`를 유지한다. 알려진 Codex hostops sysctl/preexec 거부를 재시도하거나 다른 엔진으로 우회하지 않는다.

## 동시 main 교정 재사용

로컬 교정을 a05f23c로 커밋한 뒤 fetch했더니 main에 동일 원인을 교정한 `a7e95cbf9fe5512fa887cfa6e61cca4751cd7db3`가 도착했다. 기존 `docs/work-history/2026-10-26-p0-migration-log-isolation.md`와 diff를 직접 대조했다. Config()/절대 script_location 구현은 같으며 main 회귀는 logger enabled·root handler identity·probe 수집까지 검사한다. 중복 회귀를 쌓지 않고 main의 더 강한 회귀를 정본으로 택했다. 충돌은 helper 주석 두 줄에만 있었고 merge `01171744e1eb35529efcd303bc67670793ed942a`에서 해당 파일을 main과 완전히 같게 해결했다. 위 임시 이름의 회귀가 최종 정본에도 남아 있다고 해석하지 않는다.

최종 소스에서 `test_embedded_migration_preserves_application_logging` → 실제 업로드 진단 검사를 같은 pytest 호출로 실행해 `2 passed in 0.75s`, ruff `All checks passed!`를 받았다. `git diff --quiet origin/main -- tests/test_research_v2_migration.py`와 `git diff --quiet a7e95cbf -- src alembic deploy`는 exit0이었다. 위 32건은 로컬 동일 helper 교정의 실행 근거이고 최종 main 회귀는 이 2건 및 별도 controller로 구분한다.

지원 목록 및 예약 조회로 이미 존재하는 누적 배포 `c6bedeba-f8d0-4816-9e6a-ab15b593ce51` / a7e95cbf / generations / testing / skip_tests=false / error=null을 확인했다. 따라서 앞 문단의 신규 예약 계획은 이 기존 실행을 추적하는 것으로 대체했다. 중복 예약·대기 폴링은 하지 않았다. 해당 후보는 Research 변경을 포함한 e9e3260의 자손이고 제품 runtime 코드가 같아 기존 연구 인수 증거를 그대로 재사용한다. backend 활성, frontend 배포, actual Metal 및 운영 MCP는 여전히 별도 미완료다.
