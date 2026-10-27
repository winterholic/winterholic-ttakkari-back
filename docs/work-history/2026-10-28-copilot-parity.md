# Copilot CLI 운영 통합 누락 교정

전체 목표는 진행 중이다. 코드·로컬 통합 검사와 운영 계정 활성화·실제 배정 수용을 구분한다.

## 원래 요구와 확인한 원문

- 기획 대화 `f61dbaa4-adc0-460d-bd41-1652153d8ee4`의 10/25 최초 요청부터 10/27까지 전체 조회. 최초 요청은 다른 엔진과 같은 계정·전환·인증/만료·재인증 관리이며, 10/26에는 실제 자동 오케스트레이션 합류와 이미 인증한 공식 CLI 우선 사용을 지시했다.
- 10/27 대체 대화 `8db7bace-5bdc-4334-8a95-9de2f0791694` 결과는 로컬 커밋 `7ed134d5`까지였다. 푸시·배포·운영 계정 등록·배정은 그 결과로 입증되지 않는다. 해당 환경 probe 등록과 진단 스크럽 패치를 이번 수정에 포함했다.
- 개인 work-history 10/25 원문, ADR 0071/0072, 10/26 quota 작업 기록을 코드와 대조했다. 과거의 직접 CLI 성공 보고는 재현 명령/원본 아티팩트에 이 Run에서 접근하지 않았으므로 역사적 보고로만 사용한다.
- 운영 기준은 조회한 배포 `c70bce7ba8ab888ea4989f2ab3ef887ff41f0809`, `succeeded`, release_path 동일 SHA다. 10/28 07:06 KST `account_usage`에는 opencode 계정은 있지만 copilot 계정은 없었고 worker_routing에도 Copilot이 없었다.

## 실제 차이와 변경

1. 시스템 계정 quota 조회가 HOME을 임시 폴더로 바꿔 기존 CLI 로그인을 놓쳤다. Copilot 시스템 기본 계정만 실제 HOME을 보존한다. OpenCode/격리 계정의 토큰·HOME 격리와 ambient 인증 제거는 유지한다.
2. 월간 quota가 있어도 라우팅·상한·상태·계정 화면·수집 주기에서 5시간/주간 키만 검사했다. 월간 프리미엄을 정확한 공급자 잔여율로 반영하며, 상한 차감·소진/조회 실패·미확인/무제한은 분리한다.
3. 실계정 SDK 읽기 전용 조회가 `98.1%`와 관측보다 수 초 과거의 `resetDate`를 반환했다. 기존 코드는 새 퍼센트를 만료로 숨겼다. 새 관측의 과거 reset은 미확인으로 두고 잔여량을 표시/배정에 사용한다. 미래 reset은 보존하고 소진은 미확인 reset이어도 차단한다. SDK 이슈 2619와 관측이 일치하며, 날짜를 만들어 넣지 않는다. RPC마다 새 CLI 프로세스를 시작·종료해 프로세스 수명 캐시도 공유하지 않는다.
4. Copilot `auto`를 general/routine 첫 단계에 넣고 잔여량/부하를 기존 정책으로 비교한다. 정상 기획은 native 우선이며 Copilot을 비상 후보에 추가한다. 자동 모델 결과는 별도 GPT/Claude 검증 필요로 표시한다.
5. 개인 기획이 기존 시스템 로그인을 앱 계정으로 등록·확인할 수 있는 `activate_copilot_account` MCP/API를 추가했다. 계정 idempotency·인증 실패 비활성화·회사 scope 거부·quota 관측 저장을 통합 검사했다. CLI probe+quota 제한 시간을 감당하도록 이 호출의 HTTP timeout만 90초다.
6. 하네스 레이어 API가 Copilot/OpenCode를 422로 거부하던 누락과, 내부에서 Qwen 설명으로 떨어지던 누락을 고쳤다. 실제 prompt/MCP/Seatbelt 층을 표시한다. SDK 컨텍스트 이벤트를 기존 50/70% 판단 경로로 전달한다.

## 실행 증거와 한계

- 신규 RED: HOME/월간 capacity/상한/컨텍스트 4 failed, 활성화 endpoint 404 3 failed, 비상 기획 queued 및 하네스 422, 과거 reset 처리 2 failed를 각각 관측했다.
- 최종 관련 16파일 묶음: `pytest tests/test_copilot_integration.py tests/test_orchestrator_muse.py tests/test_worker_routing.py tests/test_model_policy.py tests/test_harness_view_api.py tests/test_route_inventory.py tests/test_usage_collector.py tests/test_execution_capabilities.py unit_tests/test_copilot_parity.py unit_tests/test_copilot_usage.py unit_tests/test_copilot_runtime_regressions.py unit_tests/test_copilot_adapter.py unit_tests/test_copilot_accounts.py unit_tests/test_opencode_adapter.py unit_tests/test_opencode_defects.py unit_tests/test_opencode_seatbelt.py -q` → `271 passed, 2 subtests passed in 24.73s`. 변경 파일 Ruff와 git diff --check도 통과했다.
- 추가 자동 배정 통합: `pytest tests/test_copilot_integration.py -q -k auto_worker_uses_monthly` → `1 passed, 11 deselected in 0.83s`. 합성 CLI가 사용된 제품 API 검사이며, 실계정 모델 응답 증거는 아니다. 월간 quota로 실제 manager가 Copilot을 선택하고 영수증·검증 필요 상태를 내는 경로를 확인했다.
- 초기 전체 묶음의 267 passed/1 failed는 기존 회귀가 실계정 RPC를 호출해 고정 unknown을 기대한 문제였다. 이 테스트의 RPC를 합성 오류로 격리해 실제 공급자를 테스트에서 호출하지 않게 했다.
- 수정된 코드로 새 실제 `account.getQuota`를 호출: nested Codex sandbox, 모델 세션 없음, error=null, monthly remaining=98.1, resets_at=null, state=current, stale=false, quota_known=true, headroom=98.1. 이것은 수정된 로컬 컴포넌트의 실계정 quota 증거이며 운영 배포·모델 실행·MCP/재개 성공 증거가 아니다.
- 설치 CLI 1.0.103의 help·skill list 실행: 공유 스킬을 발견한다. caveman은 description 1024자 초과로 CLI 로드 경고가 있다. 이 스킬의 보호된 원본은 변경하지 않았다. instruction list는 현재 저장소에서 소스 0개였다(저장소에 지침 파일 없음).
- 사용량 카드의 browser 검증, 운영 계정 등록·인증, 제품 MCP 왕복·재개, 실작업 자동 배정 영수증은 미검증이다. 배포 활성화 후 새 도구로 실제 등록하고 이 항목들을 따로 확인해야 한다. 기존 API/UI 회귀 통과를 운영 수용으로 대체하지 않는다.

## 참고

- https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing
- https://github.com/github/copilot-sdk/issues/2619 (타 버전/Windows 보고, 현 환경 실측과 범위를 구분)
- CLI `copilot skill --help`, `copilot instruction list --help`, `copilot --version`

## 병합·게시 (10/28 후속, 배포 지시)

- `origin/main` 11커밋(`c70bce7b`→`873a98d4`, 보상 처리·Muse 바인딩·수집 복구)을 병합. 충돌은 `tests/test_route_inventory.py` 2곳뿐이며 양쪽 추가 항목 합집합으로 해소(코드 변경 없음).
- 병합 후 검증: `unit_tests/` 8파일 `103 passed, 2 subtests passed`. `tests/` 8파일 169개는 수집 정상이나 DB 접속이 샌드박스 loopback 차단(`PermissionError`)으로 setup 불가. 이는 이 환경 한계이며, 병합 전 동일 묶음 `271 passed`는 이전 런에서 확인됨. 변경 파이썬 전체 Ruff 통과, `git diff --check` 통과.
- 병합 결과를 `main`에 직접 푸시 후 전체 SHA로 백엔드 배포를 예약한다. 배포 활성화 뒤 운영 확인(CP-USAGE/CP-PARITY/CP-ROUTING)이 남는다.
