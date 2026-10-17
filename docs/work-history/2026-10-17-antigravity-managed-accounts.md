# Antigravity 추가 계정: 전용 HOME·전용 키체인 관리 프로필

사용자 목표는 두 번째 Google 계정을 따까리의 독립 실행 주체로 쓰는 것이다. 기존 [조사](2026-10-17-antigravity-account-isolation.md)의 결론대로 별도 HOME과 암호화된 native 키체인을 후보로 구현했다. 실제 두 번째 계정 로그인은 사용자가 하며, 이 기록은 인증 추가 직전 상태까지다.

구현: 호스트 로그인(1번 계정, `config_dir=null`)은 그대로다. 추가 계정은 `accounts/(id)/antigravity-home`(0700, 매니페스트)과 그 안의 전용 `login.keychain-db`를 쓴다. 키체인 비밀번호는 무작위 48바이트이며 호스트 키체인에만 보관하고 argv·환경·파일에 쓰지 않는다. 실행·사용량 조회는 HOME을 바꾸고 SSH·API 키 환경과 XDG 변수를 제거하며, 기본/검색 키체인이 전용 파일 하나뿐인지 매번 검사한다. 맞지 않으면 `keychain_mismatch`로 닫는다. 로그인 후 `/usage`를 한 번 호출하고 사용자가 입력한 Google 계정으로 `ready`가 된다. `/usage` 성공만으로 다른 계정이라고 판정하지 않는다. 유료 크레딧 설정(`useG1Credits`, `useAiCredits`)은 준비와 매 실행에서 꺼 둔다. 실행 중 인증 실패는 해당 계정만 `auth_expired`로 내리고 네트워크 오류는 상태를 바꾸지 않는다.

이번 이어받기에서 고친 것: 새 테스트 3개의 실패 원인을 구현 누락으로 확인했다. 호스트 설정 없이 유료 크레딧 비활성 설정 파일을 생성하고, Run 종료 시 계정별 `record_run` 훅을 `manager.py`에 연결했으며, 키체인 비밀번호 보관 실패 시 만들어진 키체인 파일을 지워 재시도가 가능하게 했다. 공용 `accounts` 폴더는 다른 엔진 홈도 있으므로 0700이 아니라 소유자·그룹/타인 쓰기 금지만 요구하도록 `allocate`를 완화했다(통합 테스트에서 발견). `POST /api/accounts/(id)/antigravity/check`를 추가해 앱에서 로그인 계정 확인을 할 수 있게 했고 프론트 계정 목록에 안내와 입력을 넣었다.

이전 구현 worker는 임시 HOME의 실제 macOS 키체인 스모크에서 두 결함을 찾아 수정했다고 보고했다. 첫째, 파일명이 사용자 기본 키체인과 같은 `login.keychain-db`이면 만든 비밀번호로 `SecKeychainUnlock`이 -25293으로 거부됐고(다른 이름은 통과했다고 기록), 전용 키체인을 `ttakkari-agy.keychain-db`로 바꿨다. 둘째, HOME에 `Library/Preferences`가 없으면 `security default-keychain -s`가 성공을 반환하고도 저장하지 않았다고 기록해 `prepare`가 0700으로 만든다. 수정 뒤 생성·재잠금해제·기본/검색 목록 단일화·`execution_env`가 통과했다고 보고했으나, 이번 독립 검증에서는 원 명령 및 출력물을 확보하지 못해 **독립 증거로 확인하지 않았다**. 비밀번호 `store/load` 왕복도 구현 보고에만 있으며 이번에는 재실행하지 않았다.

검증은 합성 CLI 기준이다. 위 키체인 스모크 외에는 합성 키체인이다. 실제 두 계정 로그인, 실제 모델 응답, 두 계정 동시 실행, 토큰 갱신, 재부팅 후 키체인 복구, 계정 삭제 뒤 키체인 항목 정리는 검증하지 않았다. 계정 삭제는 목록에서만 지우며 전용 HOME과 키체인은 남긴다.

## 독립 검증 후속 (2026-10-17)

### 판정

인증 전 배정 차단, managed HOME·Keychain namespace fail-closed, 계정별 conversation/MCP 경로는 자동화 테스트에서 확인했다. 단, 실제 두 Google identity·실제 키체인 수명·모델 응답은 확인하지 않았다. 구현 단계에 남아 있던 dirty diff에서 모호한 `authentication failed or timed out`를 인증 만료로 오분류하던 보강을 확인하고 regression으로 고정했다. 이 문구는 transport/auth를 구별하지 못하므로 `network_error`로 반환하고 기존 인증 readiness를 보존한다. 명확한 인증 실패 문구는 계속 해당 계정만 만료시킨다.

### 검증 증거

- 실행 기반: Mac Studio의 nested Codex command worker sandbox. `../back/.venv/bin/python` 사용. API 통합 전체 suite에는 새 독립 DB `ttakkari_agy_verification_20261017` 지정.
- import 경계: `../back/.venv/bin/python -c "import ttakkari.antigravity_accounts as m; print(m.__file__)"` → canonical `/Users/winterholic/development/projects/winterholic-ttakkari/back/src/ttakkari/antigravity_accounts.py`; `PYTHONPATH=src ...` → 검증 worktree의 `back-antigravity-accounts/src/ttakkari/antigravity_accounts.py`.
- RED: 두 모호 메시지 regression이 canonical 원본에서 각각 실패(기대 network/ready, 실제 auth_required/auth_expired). 이는 worktree `PYTHONPATH=src`를 빠뜨린 첫 시도였고, 검증 범위는 canonical baseline에 한정한다.
- 집중 green: `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_agy_verification_20261017 PYTHONPATH=src ../back/.venv/bin/python -m pytest unit_tests/test_antigravity_managed_profiles.py unit_tests/test_antigravity_account_isolation.py tests/test_accounts.py tests/test_antigravity_run.py tests/test_antigravity_adapter.py tests/test_antigravity_model_routing.py tests/test_worker_routing.py tests/test_account_selection.py tests/test_usage_collector.py -q` → **216 passed, 1 warning, 35.26s**. warning은 기존 FastAPI 422 constant deprecation.
- 전체 green: 같은 DB/PYTHONPATH에서 `../back/.venv/bin/python -m pytest -q` → **1299 passed, 4 skipped, 2 xfailed, 6 subtests passed, 1 warning, 249.02s**. 네 migration 테스트가 첫 full run에서 worktree에 없는 `.venv/bin/alembic`을 호출해 `FileNotFoundError`를 냈다. 임시 `.venv -> ../back/.venv` symlink로 도구 경로를 제공해 전체를 다시 실행했고 모두 통과한 뒤 symlink를 제거했다.
- `../back/.venv/bin/ruff check src tests unit_tests alembic` → `All checks passed!`; `git diff --check` exit 0.
- Frontend worktree 전체 `npm test -- --run` → **80 passed, 1 skipped; 540 passed, 1 skipped**. `npm run build` 성공(큰 chunk 경고 포함); `npm run lint` 성공(ui-lint 기준선 141건, 신규 증가 없음).
- 운영 backend: `curl http://127.0.0.1:8787/api/version` → revision `21acf683847dd15d66b57bcf17d1bb1731fbb5f8`, `pid=73626`; `/api/health` → `{"ok":true}`. `.ttakkari/deploy/current`와 release directory도 21acf revision 및 두 managed-account 모듈 존재로 확인. 선행 reservation `8e0c8bad-da89-410c-816f-6ec54ce63650`는 `succeeded`, activated revision 9d85c8a; 현재 런타임 증거는 version API의 21acf를 기준으로 한다.
- Configured browser에서 설정 경로를 열자 세션이 없어 `/login?next=%2Fchat`로 이동했다. `/api/auth/refresh`는 401. 실제 인증된 계정 설정 UI는 미확인이고 프론트 component 테스트만 확인했다.

### 미검증과 부작용

- 이전 구현 세션의 실제 Keychain 스모크에 대한 원 명령·stdout/stderr는 session 기록에서 찾지 못했다. 이 Run에서는 알려진 nested sandbox `OSStatus -25308` 경로를 재실행하지 않았고 자격증명/Google 토큰도 읽지 않았다. macOS native keychain 생성·lock/unlock·host keychain 불변은 이번 독립 검증에서 **미확인**이며 synthetic Vault 단위 테스트 결과와 구분한다.
- 서로 다른 두 Google 계정 로그인, 실제 model response, refresh, reboot 뒤 복구, account별 conversation resume 및 실제 인증 MCP 도구 호출은 사용자 인증 후 별도 수행이 필요하다. 이메일은 사용자가 입력한 값이고 `/usage` 성공과 함께 provider identity의 독립 증명이 아니다.
- 테스트 명령 두 차례에서 `TTAKKARI_TEST_DB`를 빠뜨린 채 API 테스트를 시작했다. `tests/conftest.py` 기본값 `ttakkari_test`에서 schema `drop_all/create_all` fixture가 실행됐을 수 있어 공유 **테스트 DB**가 초기화됐을 가능성을 기록한다. 이후 모든 통합 검증은 새 독립 DB에서만 했다. 운영 DB를 지정한 적은 없다.
- `antigravity_accounts.py`의 timeout/auth 구분과 regression 변경은 아직 이 worktree의 미커밋 diff다. 이 수정은 runtime revision 21acf에 포함되지 않았다. 배포 보고 전에 새 commit SHA와 reservation status를 확인한다.

### 안내서

과장된 완전 분리·무노출·자동 복구 주장을 제거하고 self-confirmed email, 실제 미검증 범위, keychain 생성 시점과 수동 복구 제약을 명시한 새 안내서: `/Users/winterholic/.ttakkari/data/outbox/67d1a6b6-c10e-4db7-ba1e-eea8b0be4c3a/antigravity-second-account-guide-reviewed.md`.

### 구현 worker 권고 결과

`verification_outcome: conditional_pass_after_fix` — mock/unit/API·통합 회귀와 frontend 검사는 green. 모호한 CLI auth/timeout 결과의 account expiry 오판을 보강하고 전체 suite를 재실행했다. 실제 Keychain 수명 및 두 Google identity 증명은 사용자 인증 전까지 open으로 유지하며 기능 전체를 real-account verified로 표시하지 않는다.
