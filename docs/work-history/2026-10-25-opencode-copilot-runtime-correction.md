# OpenCode/Copilot 재검증 후 런타임 진입점 교정

기준: back d898ead / front e771c5e, 검증자 9c5377ff failed 보고. 보고문을 완료 증거로 사용하지 않고 실행 경로를 다시 추적했다.

## 발견 및 변경

- AccountIn에 copilot 누락, WorkerIn에 두 엔진 누락. 계정 UI/MCP 스키마의 선택지가 실제 API에서 거절되던 경로를 복구.
- Copilot raw 모델 ID를 native 엔진으로 오인하여 auto로 교체하던 worker primary target 및 API 검증을 명시 엔진 카탈로그 기준으로 교정.
- 두 신규 엔진의 Seatbelt를 필수화. 기존 allow-default 정책에 쓰기 루트 제한을 추가. 기본 Copilot 환경의 자기 인증 홈 차단 및 OpenCode 실행 바이너리 차단을 제한된 예외로 교정.
- Copilot 시스템 계정의 비밀 없는 상태 manifest를 추가. 파일 존재와 config 조회 성공은 인증 증거가 아님. 모델 응답 probe 또는 successful_run만 authenticated로 인정. 구버전의 근거 없는 authenticated는 unknown.
- CP 환경의 토큰/BYOK override와 COPILOT_ALLOW_ALL=true 제거. MCP 설정 @파일 접두사, 스트리밍/자동 업데이트 비활성화, 최종 이벤트 없는 부분 응답의 fail-closed 적용.
- 실제 premium allowance 문구 누락 및 quota 안내 오탐 교정.
- 새 엔진 usage가 Codex 계정 조회로 내려가던 경로 제거. 잔여량 미확인을 유지하며 런타임 한도 오류 기반 쿨다운은 별도 유지.
- 프론트 인증 미확인 응답의 성공 toast 제거. 기존 기본 인증 먼저 확인, 확인 요청의 구독 소모 안내, 관측 불가한 사용 상한 입력 제거.

## 검증

- 새 회귀를 먼저 실패로 재현한 뒤 교정. 마지막 일괄 회귀(새 실행 파일 예외 전): 156 passed (계정·선택·새 API/fallback·보안 inventory·OC/CP adapter/account 묶음), Ruff 통과.
- 최종 묶음: `pytest tests/test_accounts.py tests/test_account_selection.py tests/test_copilot_integration.py tests/test_worker_routing.py tests/test_security_gate_inventory.py tests/test_seatbelt.py unit_tests/test_copilot_runtime_regressions.py unit_tests/test_copilot_accounts.py unit_tests/test_copilot_adapter.py unit_tests/test_opencode_accounts.py unit_tests/test_opencode_adapter.py unit_tests/test_opencode_defects.py unit_tests/test_opencode_seatbelt.py unit_tests/test_account_fallback.py` → **258 passed, 1 skipped**, 21.53s. 커널 테스트는 앞서 관측한 중첩 sandbox_apply 거부를 반복하지 않도록 `TTAKKARI_TEST_SEATBELT_UNAVAILABLE`로 명시 제외했다.
- 추가 실행 파일 예외 후 관련 27 passed. 기존 Seatbelt 정적·decoy 묶음 44 passed / 1 skipped. 커널 test는 중첩 sandbox_apply 제약으로 skip. 새 정책의 커널 적용 증거가 아님.
- 새 통합 4개: 시스템/격리 계정 등록, 두 엔진 worker API 모델 보존(실제 dispatch 차단), 기존 manager의 401 후 다음 계정 전환/만료 상태 저장(실제 provider attempt만 mock).
- Front AccountsSection 15 passed. eslint/ui-lint/tsc/vite build 통과. pnpm wrapper는 node_modules hoist가 실제 디렉터리가 아니라 설치 단계에서 실패하여 이미 설치된 직접 바이너리를 사용. UI 브라우저 실측은 하지 않음.

## 미검증 / 다음 작업

- Copilot 실제 read-only 응답 시도: Codex 중첩 샌드박스, /opt/homebrew/bin/copilot -p ... --output-format json --available-tools=view, session initialization에서 exit 1, `Operation not permitted (os error 1)`, JSON 이벤트 없음. 같은 거부를 다른 경로로 재시도하지 않음. 운영 계정 유효성/모델 응답 증거가 아님.
- 실제 인증, 모델 카탈로그, 대화 재개, 세션 MCP 호출, 신규 필수 Seatbelt의 커널 차단 증거는 각각 남아 있음.
- 두 엔진은 auto ROUTES 제외 유지. 기존 다른 엔진도 계정 화면에 모델 선택기는 없으므로 기존 세션/API/MCP model 경로를 사용.
- 이전 배포 예약 948d3a1f는 이번 조회 시 testing / error null. 성공 주장이 아님. 교정 revision의 배포 예약은 별도 확인.

프론트 교정은 `ad6149d200304017d9bf04afe41bca0609373a04`로 origin/main에 push. 프론트 운영 배포 상태는 아직 미확인.

## 배포 차단 감사 분류 누락 교정 (2026-10-25 15:36 KST)

- 독립 검증자 9c5377ff는 1614882의 코드 경계/좁은 회귀/API 검사를 통과로 보고했다(28 passed 보고). 실제 응답·재개·MCP·로그인 모델 목록·다중 identity·커널 격리는 여전히 미검증이다. 다른 Run outbox의 원문은 파일로 읽지 않았으며 get_worker/read_session의 지원된 결과 범위만 확인했다.
- 배포 예약 1833c8df는 조회 당시 testing/error=null이며 성공이 아니다. 이전 예약 948d3a1f는 pytest에서 실패했다. 지원 도구 ops_deploy_log의 실패 원인은 test_report_schema_separates_extraction_classification_scope_coverage의 all_collected_classified=False였다(그 시점 4603 passed 후 실패).
- 1614882에서도 해당 테스트를 단독 실행해 같은 1 failed를 재현했다. run_audit의 unmatched는 정확히 configs의 opencode_bin/copilot_bin, source_files의 opencode_accounts.py/copilot_accounts.py 네 항목이었다. 다른 범주의 unmatched는 없었다.
- 두 실행 파일 설정을 exec.run_engine, 두 계정 파일을 core.accounts_quota로 분류하는 교정을 검증했다. push 시 다른 작업의 같은 교정 4f2e03a3424055b3e8ad8301784c2d243d0760bb가 main에 먼저 반영된 것을 확인하여 해당 구현을 그대로 재사용한다. 본 추가 커밋에는 신규 회귀와 기록만 포함한다. catch-all, coverage, 제품 건강 판정은 변경하지 않는다.
- 신규 4개 회귀를 먼저 4 failed로 재현했다. 교정 후 `TTAKKARI_NO_ENV_FILE=1 .venv/bin/python -m pytest -q unit_tests/test_platform_v2_inventory.py` → **55 passed in 61.01s**. 동일 파일 Ruff 및 git diff --check 통과. 이전에 유효했던 엔진 회귀258개는 이 분류 규칙 전용 변경으로 무효화되지 않아 재실행하지 않았다.
- 최종 추가 커밋은 감사 회귀 테스트와 작업 기록만 바꾸며, 분류기는 선행 4f2e03a를 재사용한다. 기존 런타임 수용 미검증 항목은 닫지 않는다. 교정 revision의 push 및 배포 예약은 영속 목표 계약에서 추적한다.
