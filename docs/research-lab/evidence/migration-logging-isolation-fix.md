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
