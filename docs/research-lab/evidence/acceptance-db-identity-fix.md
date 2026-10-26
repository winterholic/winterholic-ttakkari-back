# 인수 테스트 DB identity 고정 가정 교정

2026-10-26. b8bb518b/eb3bcf6의 필수 controller gate가 실패했다.
지원 `backend_deploy_status` 및 `ops_deploy_log(summary)`에서 실제 오류를 확인했다:

```text
tests/test_research_v2_combination_acceptance.py:18
assert tuple(identity) == ("ttakkari_test", "ttk_test")
actual: ('ttakkari_gen_b8bb518b27684fe5a8782701d73459d2', 'winterholic')
1 failed, 2881 passed, 11 skipped, 2 xfailed, 6 subtests passed in 903.88s
```

이 검사는 실제 조합 실행 전에 실패했다. 제품 조합 실행 장애를 관측한 것이 아니다.
리드가 추가한 검사의 환경 가정 오류다. 앞선 dispatch 교착 및 logging 격리 오류와 다른 원인이다.

## 원인과 범위

`generation_deployment.py:prepare`는 승인된 앱 연결의 DB 이름만 `ttakkari_gen_<jobhex>`로 바꿔
검사용 DB를 만들고 `test_environment`가 그 URL을 `TTAKKARI_TEST_DB`로 주입한다.
Run worker는 자체 `ttakkari_test / ttk_test`를 사용하므로 두 경로의 이름·사용자가 다를 수 있다.
`tests/pg_sandbox.py`의 기본 URL은 username도 생략할 수 있다.

이번 추가 두 파일의 고정 tuple 가정을 같은 방식으로 교정했다.

- 주입된 `TTAKKARI_TEST_DB`를 `make_url`로 파싱하고 실제 `current_database()`와 대조한다.
- 명시 username이 있으면 실제 `current_user`와도 대조한다. 생략된 username을 임의 추측하지 않는다.
- 운영 DB `ttakkari` 차단을 각 검사와 기존 conftest에서 유지한다.
- 실제 identity는 기존 원시 영수증에 계속 기록된다. DSN·비밀번호는 출력하지 않는다.
- 전체 tests 검색에서 동일한 고정 tuple assertion은 더 남지 않았다.

제품 src·migration·배포 정책·gate 통과 기준은 바꾸지 않았다.

## 실제 재현과 검증

[재현 helper](repro-acceptance-generation-db.py)는 현재 Run에 주입된 loopback
`ttakkari_test / ttk_test` 및 실제 SELECT 일치를 먼저 단언한다.
그 연결 안에서 UUID로 자기 소유 임시 `ttakkari_gen_*` DB를 만들고 두 실제 API/PG 인수 검사를 실행한 뒤
자기가 만든 DB 하나만 finally에서 제거한다. 새 PostgreSQL 서버, 운영 DB, 다른 Run DB는 사용하지 않는다.

```sh
PYTHONPATH=src .venv/bin/python docs/research-lab/evidence/repro-acceptance-generation-db.py
```

교정 전: 실제 generation-shaped DB 두 검사 모두 같은 이름 가정에서 `2 failed in 0.50s`.
교정 후: `2 passed in 1.02s`, 자기 소유 DB 제거 메시지 및 exit0.
이 재현의 DB user는 ttk_test이며 배포 호스트 user winterholic로 실행한 결과는 아니다.
username 생략 URL 경로도 이번 재현에서는 실행하지 않았다.

기존 Run DB:

```sh
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest tests/test_research_v2_combination_acceptance.py tests/test_research_source_refresh_acceptance.py tests/test_security_gate.py -k 'chunking_hybrid_grid or source_refresh_retains or test_db' -q --timeout=30 --tb=short
```

`2 passed, 9 deselected in 0.99s`. `test_db` 표현은 production_db 두 검사를 선택하지 않았으므로 별도 실행했다:

```sh
PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest tests/test_security_gate.py -k production_db -q --timeout=30 --tb=short
```

`2 passed, 7 deselected in 0.55s`. query parameter가 붙은 운영 DB도 거부한다.
두 수정 파일 ruff → `All checks passed!`; `git diff --check` → exit0.
후속 main/feature FFpush 및 새 full SHA 예약 후 controller의 실제 전체 gate 결과를 별도로 확인해야 한다.
새 runtime/Metal·frontend 운영·MCP 최종 인수는 아직 미완료다.
