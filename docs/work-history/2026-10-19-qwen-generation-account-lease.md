# Qwen 세대 실행 계정 외래키 오류 · 2026-10-19

사용자가 앱에서 Qwen 작업자 사용을 켰다고 보고한 뒤 기존 실제 모델 수락 작업자
`4ec504cc-10b7-4530-ab43-eb922fe8e51a`에 재시도 Run
`c2919497-debd-4586-b4a4-9472b0422c5f`를 접수했다. 현재 배포 revision은
`b77ac7fa57ee434879dd2bd501d8617e4661ca57`이다. get_worker에서 이 시도는
`runtime_leases_account_id_fkey` ForeignKeyViolationError로 실패한 것을 확인했다.
실제 모델 응답·파일 수정·동일 Qwen 대화 재개는 아직 검증하지 못했다.

배포본과 같은 코드의 `GenerationRuntime.choose_profile`이 로컬 Qwen의 합성
프로필 UUID를 `engine_accounts`를 참조하는 lease.account_id에 저장하고 있었다.
별도 PostgreSQL DB `ttakkari_test_qwen_lease_20261019`에서 실제 세대 런타임을
사용하는 회귀 검사를 추가했다. 수정 전 계정 선택 검사와 가짜 Qwen 실행 검사가
둘 다 같은 외래키 오류로 실패했고 기존 legacy 검사는 통과했다(2 failed, 1 passed).
재현 출력은 Qwen 합성 ID `7655e1c2-bcdc-517b-a4b5-f8818fb66754`가
engine_accounts에 없다고 명시했다. 준비 검사 이후, 모델 실행 명령 생성 전의
계정 선택 경로에 있는 결함이다. OS 권한 실패로 일반화하지 않는다.

로컬 Qwen을 선택할 때 lease.account_id만 NULL로 저장한다. generation_id·token·lane
소유권, 외래키, 클라우드 계정 부하 집계, readiness·활성화·개인 범위 검사는 유지한다.
DB 스키마나 운영 설정을 변경하지 않는다. Qwen→클라우드→Qwen 전환 때 계정 연결을
설정/해제하는 것과 다른 세대의 선택 거부를 검사했다. 기존 가짜 Qwen 결과·resume
검사를 legacy와 generation 두 모드에서 실행하도록 확장했다.

검증 명령:
`TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_test_qwen_lease_20261019 .venv/bin/python -m pytest tests/test_generations.py tests/test_qwen_local_run.py tests/test_qwen_emergency_e2e.py tests/test_qwen_worker_settings.py tests/test_worker_quota_recovery.py -q --tb=short`

결과: `53 passed in 13.18s`. 지정 3개 Python 파일 Ruff 및 git diff --check 통과.
테스트는 가짜 Qwen 하네스이며 실제 모델 실행 성공을 의미하지 않는다.

배포 예약 후 컨트롤러의 활성화 결과를 확인하고 같은 실제 Qwen 작업자를 재시도해야
한다. fixture는 `/Users/winterholic/development/qwen-live-acceptance-20261019`이며
stats.py만 수정, test_stats.py의 기존 5개 검사를 보존한다. 실제 명령·모델 정체·diff·검사
출력을 독립 확인한 뒤 같은 worker에 후속 메시지를 보내 동일 Qwen 대화 resume를
검증하고 verification_outcome을 기록한다. 운영서버·보호 설정·가드를 우회하지 않는다.
