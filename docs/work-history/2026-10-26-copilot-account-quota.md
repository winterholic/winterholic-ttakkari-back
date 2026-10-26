# OpenCode·Copilot 계정 사용량 조회

현재 범위: O13 사용량 조회 코드와 표시 연결. 실제 계정 RPC·운영 활성화는 미검증이다. 원래 OpenCode 일반 오류, 모델 응답·재개·MCP 검증과는 별개다.

## 원인과 변경

`runs/usage.py:_fetch_live`는 OpenCode/Copilot에서 조회 없이 고정 unknown 오류를 반환했다. 설치 OpenCode 1.19.4 `--help`와 공식 TUI/CLI 문서를 확인했다. `stats`는 로컬 세션 토큰/비용 통계이고, `/usage`의 OAuth quota 제안 PR9545는 Closed 상태다. 제안의 존재를 출시 근거로 쓰지 않는다.

공식 `github-copilot-sdk==1.0.26`의 `account.getQuota`를 사용한다. `start` → 선택 계정 quota RPC → `force_stop`만 실행하며 모델 세션이나 프롬프트를 만들지 않는다. SDK account quota API는 experimental이다.

- OpenCode는 선택된 관리 프로필의 GitHub Copilot OAuth만 서비스 안에서 읽는다. 실제 인증 원문은 에이전트 도구로 조회하지 않았다. 임시 HOME/XDG/COPILOT_HOME, empty mode, `use_logged_in_user=False`로 다른 계정 차용을 막는다. 토큰은 argv/관측 결과/오류 메시지에 싣지 않는다.
- Copilot은 기존 관리 프로필 COPILOT_HOME 또는 명시된 시스템 기본 계정을 사용한다. 관리 프로필 UUID를 검사하고 상속 GitHub 토큰과 BYOK 변수를 제거한다. Copilot 기본 계정의 keychain 사용을 위해 이 경로만 copilot-cli mode를 쓴다.
- 시작+RPC 20초, 종료 5초 제한과 취소 정리를 둔다. SDK 예외 원문은 외부로 내보내지 않는다.
- 공급자 remainingPercentage를 보존하고, 미제공/null/유효하지 않은 수치를 0으로 만들지 않는다. 잔여 0은 실제 소진으로, 무제한은 퍼센트 없는 quota_unlimited로 구분한다. quota_unlimited는 구독의 모든 모델이 무료라는 뜻이 아니다.
- 엔진·프로필 변경 시 캐시를 차용하지 않는다. 조회 실패 시 이전 관측 시각을 유지하고 오류·stale을 표시한다.
- 기존 sink/DB/REST/MCP로 잔여율과 quota_unlimited를 전달한다. 기존 JSON windows에 숫자 없는 unlimited 항목을 보존하므로 DB migration은 없다. 구버전 reader는 percent 없는 항목을 무시한다.
- 프론트 별도 worktree `front-copilot-usage`에서 잔여율·공급자 출처·무제한 표시를 연결한다. 기존 5시간/주간 routing cap과 무제한 비상 경로는 확대하지 않는다.

## 검증

- 최초 RED: `pytest unit_tests/test_copilot_usage.py --maxfail=1` → 1 failed (copilot_usage 모듈 없음).
- 계정 사용량 파싱/격리/취소/SDK 호출 순서/캐시/REST/MCP 등 추가 검사와 관련 회귀: `TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=/private/tmp/ttk-copilot-sdk-test-1026:src:. ../back/.venv/bin/python -m pytest unit_tests/test_copilot_usage.py unit_tests/test_copilot_runtime_regressions.py unit_tests/test_usage_failure_visibility.py unit_tests/test_opencode_adapter.py tests/test_usage_collector.py tests/test_antigravity_usage_states.py -q` → 126 passed (6.21s).
- SDK 1.0.26을 임시 target에 설치해 두 mode의 실제 CopilotClient 생성 확인. CLI 시작/실제 인증/RPC 성공 증거는 아니다. 공유 backend venv는 변경하지 않았다.
- 초기 묶음의 실패 2개는 수정 전 bf8d31f9 임시 worktree에서도 동일하게 재현했다. 2026-10-25 고정 reset epoch와 2026-10-26 15시가 지나서 발생한 테스트 clock 의존이다. 로컬에서 날짜를 상대값으로 바꿔 확인했다. rebase 중 origin/main에 같은 결함의 선행 교정이 있음을 확인해 upstream의 교정(미래/과거 reset 양쪽 검증 포함)을 재사용했다. 이 커밋에는 두 기존 테스트 수정이 없다.
- Front: 사용량 Vitest 34 passed, 변경 파일 ESLint, tsc -b, vite build, ui-lint 138건 기준선 통과. 실제 로그인 브라우저·운영 계정 값은 미검증이다.
- 저장/복원 DB 통합 1개와 감사 inventory 전체: `pytest tests/test_usage_collector.py::test_copilot_quota_persists_and_restores_without_inventing_unlimited_percent unit_tests/test_platform_v2_inventory.py -q` → 56 passed (54.06s).
- 전체 provider runtime 검증 및 원래 O2/O3/O7/O8/O9/O10은 이 검사로 완료되지 않는다.

## 근거

- https://opencode.ai/docs/cli/#stats
- https://github.com/anomalyco/opencode/pull/9545
- https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing
- 공식 wheel github-copilot-sdk 1.0.26의 generated AccountGetQuotaRequest/AccountQuotaSnapshot 및 client.py.
