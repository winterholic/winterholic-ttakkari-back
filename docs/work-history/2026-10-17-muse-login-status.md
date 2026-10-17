# Muse 계정 목록의 잘못된 로그인 안내 수정

사용자는 로그인 완료·계정 등록 후에도 인증되지 않았다고 표시되는 문제를 보고했다.

운영 `/api/version`은 `9d85c8a7c27decac80439feb20f352a7bc3457c0`이었다. 해당 release의 코드를 사용하고 운영 계정 행을 읽어 공식 SDK 사용량 전용 조회를 실행했다. `내 Muse`의 `config_dir=null`은 앞서 인증한 muse-default 프로필과 일치한다. SDK 로그인 결과는 `ok`였지만 `_out`은 조회 전후 모두 `unknown`과 로그인 명령을 반환했다. 화면은 `unknown`을 로그인 필요로 표시하므로 재인증 문제가 아닌 API 상태 불일치다. 계정 정보·인증 파일·토큰은 변경하지 않았다.

계정 목록은 Muse에 한해 수집기의 읽기 전용 조회와 TTL을 재사용하고 `_out`은 그 로그인 관측값을 사용하도록 수정했다. 사용량 창이 없어도 로그인 상태는 유지한다. 계정 경로 변경 후 이전 캐시가 섞이지 않도록 커밋 후 해당 캐시를 폐기한다. 프론트의 진행 중 변경은 수정하지 않았다.

TDD: 로그인/로그아웃 재현 검사 2건이 기존 코드에서 `unknown != ok/missing`으로 실패했다. 캐시 경로 변경 검사도 `ok`가 남는 실패를 확인한 뒤 수정했다.

검증: `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_muse_login_test_20261017 .venv/bin/pytest -q --tb=short tests/test_muse_api.py tests/test_accounts.py tests/test_usage_collector.py tests/test_account_selection.py` → 93 passed, 1 warning in 18.50s. 경고는 기존 Antigravity 422 상수 deprecation이다. `PYTHONPATH=src .venv/bin/python -m unittest discover -s unit_tests -p 'test_muse*.py'` → 34 tests / OK. 변경 Python 2개 파일 Ruff 통과.

수정 후보와 실제 계정 행·공식 읽기 전용 조회를 대조한 결과: `official_probe_login=ok`, `account_list_login=ok`, `shows_login_command=false`. 이는 후보 코드 검사이며 배포된 API 응답 검증과 다르다. 실제 모델·코딩·재개·MCP 도구 호출은 이번 수정에서 실행하지 않았다.

실행 근거는 outbox `01257aa1-0fb1-4ec3-8124-2e74548e9237`의 재현 JSON·후보 검사 JSON·테스트 로그에 있다. 검증된 clean 커밋을 push한 뒤 generation 배포를 예약하며, 예약 수락은 배포 완료가 아니다.
