# Antigravity 다중 계정 조사·단일 인증 중복 등록 방지

사용자 목표는 두 Google 계정을 따까리의 독립 실행 주체로 운영하는 기능이다. 이번 결과는 조사와 선행 결함 방지이며 **전체 요구 미완료**다. 두 번째 계정 로그인·권한 범위가 준비되지 않았고 최종 인증 격리·토큰 갱신·logout 독립·병렬 코딩·재부팅 복구를 검증하지 않았다. 별도 macOS 사용자/Keychain 생성·권한 변경·기존 인증 복사/삭제·SSH 환경 가장·OAuth 추출은 수행하지 않았다.

기존 main의 다른 작업 변경을 보존해 `back-antigravity-accounts` / `feat/antigravity-accounts`에서 작업했다. 최초 41e007d에서 시작해 Qwen worker 변경 f98075a 위로 rebase했다. 원래 checkout의 미커밋 변경은 수정하지 않았다. 현재 배포 포인터 c1d7062의 release에 Antigravity adapter/API/profile 모듈이 실제 존재함을 확인했다.

선행 결함: 두 번째 Antigravity 계정 POST가 자동 config_dir를 만들지만 실행 Profile.env와 quota 수집이 이를 무시해 같은 기본 Google 인증으로 실행/표시할 수 있었다. API의 두 번째 생성은 409 `antigravity_isolation_unsupported`로 닫고 DB transaction advisory lock으로 동시 POST도 직렬화한다. 기존 잘못된 config_dir는 실행 전에 ValueError로 닫고 quota는 cache/기본 agy 조회 전에 오류를 반환한다. 이것은 다중 계정 기능 구현이 아니다.

TDD 증거: 새 unit 검사 3개가 경로 무시·기본 quota 호출·cache 재사용으로 실패, API 2개가 순차/동시 모두 201로 실패한 뒤 수정했다. 첫 수정 후 test 응답 계약을 detail 대신 제품의 {code,message}로 고쳤다. 최초 관련 회귀 143 passed in 28.21s. rebase 뒤 Qwen adapter/run을 포함한 최종 관련 회귀 160 passed in 31.03s. 명령은 전용 DB `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_agy_accounts_20261017 PYTHONPATH=src ../back/.venv/bin/python -m pytest tests/test_accounts.py tests/test_antigravity_policy.py tests/test_antigravity_adapter.py tests/test_antigravity_usage.py tests/test_antigravity_usage_contract.py tests/test_antigravity_model_routing.py tests/test_antigravity_run.py tests/test_worker_routing.py tests/test_qwen_local_adapter.py tests/test_qwen_local_run.py unit_tests/test_antigravity_account_isolation.py -q --tb=short --junitxml=<outbox>/evidence/safeguards-regression.xml`이다. Ruff 5파일과 git diff --check 통과. 합성 엔진/격리 DB 검사이며 실제 두 계정 실행 증거로 사용하지 않는다.

네 저장소를 SHA로 고정해 소스를 읽었다. mgy는 전체 CLI 상태 symlink·빈 password Keychain·별도 OAuth 갱신/추출 때문에 도입하지 않는다. ParaGravity와 Multigravity는 Electron/IDE 런처다. ParaGravity는 SSH 변수 가장과 파일 토큰 방식, 기본 host 설정/Keychain 공유도 확인했다. Multigravity는 전체 cp -R clone과 라이선스 미확인이다. 공식 저장소는 인증/엔진 구현 소스 공개가 아니라 문서·CHANGELOG·예제다. SHA·근거·대안·운영 초안은 `docs/antigravity/multi-account-investigation.md`에 있다.

실제 설치 agy 1.4.0에서 기본 HOME `/usage` SUCCESS/exit 0, 빈 HOME ERROR/exit 1 `authentication failed or timed out`. 읽기 전용 security default-keychain은 기본 HOME에서 기존 login keychain, 빈 HOME에서 default keychain 부재다. 이는 metadata/미인증 분리 관측이며 로그인 후 격리를 보장하지 않는다. 현재 agy는 --user-data-dir와 /usage 조합에서 exit2 unknown flag, help login은 exit1 unknown subcommand다. --version과만 조합하면 unknown flag도 성공해 플래그 검증을 대신할 수 없었다. macOS #1080 collaborator의 별도 HOME 안내를 찾았으므로 기존 ADR의 과거 '지원 경로 미확인'을 현재의 전면 불가능 판정으로 쓰지 않도록 보충했다.

ParaGravity의 pinned source `python3 -m unittest discover -s tests -v`는 fake 프로세스 기반 43개 중 38개 성공/5개 실패다. 실패 stage는 프로세스 탐지·종료·병렬 상태다. source가 ps/pgrep stderr를 버리므로 원인은 미확정이며 sandbox 영향 가능성은 코드 기반 추론이다. 실제 앱이나 운영 backend 실패로 일반화하지 않았다. fake app의 sleep300 자식은 검사 뒤 일부 수 분 남을 수 있어 즉시 종료를 보증하지 않았다. 원래 Antigravity 앱은 이 검사에서 띄우지 않았다. 같은 실패/거부를 우회 재실행하지 않았다.

산출물은 `/Users/winterholic/.ttakkari/data/outbox/ad7d1def-393e-4621-b8d7-4b1e560254f3/`의 HTML 조사 보고서·상세 설계/운영 초안·회귀 XML/text·소스 manifest·비밀 없는 CLI 증거다. 직접 browser preflight는 nested sandbox의 IPC denied로 blocked라 shell 브라우저는 띄우지 않았다. configured ttakkari_browser MCP에서 check_ui.py의 JS를 동일 출처 iframe에 실행해 1440/1180/390 × light/dark 6개 조합 모두 overflow/offscreen/clipped/overlap/invisible/tinyText 빈 목록, Mermaid SVG 1/error0, console error0을 확인했다. 스킨 토큰 교체가 첫 시도에 잘못된 마커 때문에 적용되지 않은 것은 actual CSS marker로 고친 뒤 6개 조합을 재검사했다. MCP 브라우저는 종료했다.

후속: 별도 HOME + 암호화 native Keychain은 다음 검증 후보일 뿐 최종 선정 아님. 공식 sign-in으로 서로 다른 identity 두 개를 확인하고 logout/refresh/재부팅까지 통과해야 한다. 계정 명시 지정·auth 상태 저장·worktree 충돌 강제·부작용 이후 자동 replay 제약도 미구현이다. 실제 모델 응답·conversation resume·MCP 실행은 /usage 성공과 별개로 미검증이다. 운영 인증을 보존하며 작업을 이어가야 한다.

## 최신 통합 검증

첫 원격 main push는 다른 세션의 Muse 변경이 먼저 반영돼 non-fast-forward로 거부됐다. force push 없이 fetch했고 Muse adapter·account·profiles·usage의 변경을 확인한 뒤 98433719addf6e2a8c7913ce3a3fa7c75c93dd22 위로 충돌 없이 rebase했다. 기존 diff가 우리 의도한 3개 구현 표면에만 남아 있음을 확인했다. 위 테스트 명령에 tests/test_muse_api.py, unit_tests/test_muse_registration.py, unit_tests/test_muse_usage.py를 추가한 최종 결과는 172 passed in 31.20s다. Ruff와 diff check도 다시 통과했다. 최종 /usage 재조회도 기존 HOME에서 SUCCESS/exit0으로 유지됐으며 모델 응답·재개·MCP·두 identity는 검증하지 않았다. configured MCP browser와 임시 localhost 보고서 서버 모두 종료했다. 검증된 선행 수정만 clean SHA로 push하고 generation 배포를 예약한다. 예약 수락을 운영 성공으로 보고하지 않는다.

## 배포 예약 거절 (운영 미반영)

요청 SHA `2cecd7a67b209224822726ef295709dc1fb821fc`는 원격 main과 일치·전용 worktree clean 확인 후 request_backend_deploy에 전달했으나 backend가 409 conflict `예약한 커밋과 작업 트리가 다릅니다. 변경을 커밋하고 다시 배포하세요.`를 반환했다. 예약 ID는 없고 미접수다. 확인한 canonical back은 HEAD9843371에 다른 작업의 untracked `unit_tests/test_qwen_local_contract.py`가 있었다. tracked 부분만 원격 main으로 fast-forward해 SHA를 맞췄고 해당 파일은 보존했다. deploy.ensure_revision은 untracked도 dirty로 거부하므로 동일 요청을 재시도하거나 source root·clean guard를 우회하지 않았다. 사용자 승인 질문 없이 안전한 원격 코드/보고서 전달까지 진행했다. 이 실패는 배포 예약 단계의 작업 트리 조건이며 Antigravity 엔진의 운영 실패가 아니다. 운영 기능·실계정 검증·배포는 미완료다. 후속 배포는 그 Qwen 작업의 파일이 정리된 canonical clean 상태에서 검증한 SHA로 재예약해야 한다.
