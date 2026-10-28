# Research Lab v2 및 플랫폼 V2 실제 미게시 차이 통합 및 검증 (교정 후속)

- **날짜**: 2026-10-29
- **작업 브랜치**: `integ/research-platform-v2-20261029`
- **기준 `origin/main`**: `f95cef4b036bf64cb25f5a4317f2bc21ee274352` (`chore(inventory): classify fd_limit.py as core runtime`)
- **기능 통합 커밋 SHA**: `dd747e661605e714652c78da0c78a0dca0fcad10`
- **최신 merge HEAD SHA**: `277f71e319bf5228514930be34927b508f7ce682`

---

## 1. 독립 감사(GPT Luna e3bdd110 / Run edb3e5d6) 판정 수용 및 교정

1. **감사 판정 분석**:
   - `test_research_rules.py` 기존 19건은 도메인 규칙 불변식 함수만 검증하며, 카탈로그 리뷰(`SelectionIn` 및 카탈로그 후보 실패/조건 처리)와 스케줄 변경(`profiles.Profile` 계약)의 실행을 포함하지 않음.
   - 플랫폼 V2 `test_executor_safety.py` 21건은 contract별 `TTAKKARI_RUN_EXECUTOR_MODE`가 생성된 API plist에 들어가는 배선 검증으로 유효함 (재실행 불필요).
   - 실제 launchd 기동 및 런타임 접수는 별도 미검증(`미검증`)으로 분리.
2. **후속 커밋 실제 변경 대조 및 focused 회귀 테스트 4건 추가**:
   - `dac9737b` (`feat(research): include registry methods and conditioned failures in review`):
     - `test_catalog_selection_request_model_accepts_catalog_source_and_rejects_invalid`: `SelectionIn`의 `candidate_source="catalog"` 및 `"research"` 검증, 미지원 source 차단 검증.
     - `test_catalog_selection_novel_candidate_without_cost_requires_review`: source_node_id 없는 카탈로그 후보(`cost_seconds=None`, `hypothesis_stage`)가 `needs_review`로 안전하게 분류되고 `expected_effect="unknown_until_measured"` 유지 검증.
     - `test_catalog_selection_direct_failures_distinguish_applicable_and_conflicting_conditions`: `selection_failures`의 직접 카탈로그 실패 연결 시 일치 조건(needs_review) / 비호환 조건(candidate 유지) / 선언-관측 불일치 충돌(needs_review) 분리 및 `_EXECUTION_RECEIPT_FIELDS`(`research_execution_owner` 등) 메타데이터 분리 검증.
   - `beea9a89` (`test(schedules): use runtime profile contract in persisted quota wait`):
     - `test_schedule_quota_wait_profile_contract_and_model_capacity_key`: `profiles.Profile` dataclass 계약(`.engine`, `.model`) 기반 `model_capacity_key` 유효 UUID 생성 및 구형 `FakeProfile`(model 누락 시 AttributeError) 회귀 방지 검증.
3. **Focused 회귀 실행 결과**:
   ```sh
   PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest unit_tests/test_research_rules.py unit_tests/test_executor_safety.py
   ```
   - **출력**: `44 passed in 0.80s`
     - `unit_tests/test_research_rules.py`: **23 passed** (기존 19 passed + 신규 focused 4 passed)
     - `unit_tests/test_executor_safety.py`: **21 passed** (기존 유효 실행 유지)

---

## 2. 미게시 차이의 실제 범위 대조 및 확정

- **공통 조상 검증**:
  - `5a0b4ed151cfe588e0e6f03eca0d50ed975d40b1`은 운영 `12347b3f` 및 후속 FD `2e75876d`의 공통 ancestor로 이미 확인됨 (`git merge-base --is-ancestor 5a0b4ed1 origin/main` -> 참). "Research 전체 미통합" 가정은 명백히 취소됨.
- **백엔드 (`back-research-v2`)**:
  - `origin/research-v2`(`5a0b4ed1`) 대비 HEAD(`beea9a89`)는 6커밋 앞섬.
  - 이 중 1커밋(`1083a0ce`)은 이미 main에 병합되어 있으므로, 실제 unreleased 커밋은 4건(`dac9737b`, `3c3b1640`, `9548e0ce`, `beea9a89`).
  - 플랫폼 V2 `ba14ee63` 1건과 결합하여 총 5건의 기능/문서 차이를 `integ/research-platform-v2-20261029`에 체리픽 이식 후 신규 focused 회귀 커밋(`dd747e66`) 및 최신 `origin/main`(`f95cef4b`) 머지(`277f71e3`) 완료.
- **프론트엔드 (`front-research-v2`)**:
  - `origin/research-v2`(`02d871c`) 대비 HEAD(`57730bd`)는 17커밋 앞섬.
  - 그러나 16개 커밋은 이미 `origin/main`(`b2b3600`)에 병합 완료됨 (`git rev-list --left-right --count origin/main...HEAD` -> `0 1`).
  - 따라서 프론트엔드의 **진짜 미게시 차이는 단 1개 커밋**(`57730bd`, `feat(research): review catalog methods against study context`, +156 lines)임.

---

## 3. 플랫폼 V2 런타임 수신 및 운영 경계

- **검증 범위 한정**:
  - 21개 테스트는 contract별 `TTAKKARI_RUN_EXECUTOR_MODE`가 생성된 API plist에 정확히 들어가는 배선까지만 검증함.
  - 실제 launchd 기동 및 런타임 접수는 **미검증** 상태로 분리 보존.
- **지원되는 안전한 런타임 확인 경로**:
  - 에이전트 Run 내부에서 `launchctl` 또는 백엔드 재시작 스크립트 실행은 엄격히 금지됨.
  - 안전한 런타임 상태 확인 경로: MCP 도구 `backend_deploy_status`, `ops_deploy_log`, `ops_search_logs` 및 `/api/status`, `Slots.snapshot`을 통한 generation 상태 관측 확보.
- **배포 및 통합 소유권**:
  - `INFRA 441ae34d`가 유일한 `main` 통합 및 배포 소유자임.
  - 검증 브랜치(`integ/research-platform-v2-20261029`) 및 full SHA(`277f71e319bf5228514930be34927b508f7ce682`)를 기획 세션(`af14105d`)에 인계함.

---

## 4. 전수 감사 및 누락·좀비 항목 소유권 정리

- **최근 100세션 감사 범위 및 연결**:
  - 독립 검증자가 전수 재감사를 돌리지 않았음을 확인하고, 본 작업자가 대조한 범위/목록(4건 발굴: 권한 c8be9694, GitHub 403, OpenCode ecc176d9, 좀비 2ea0d05a)을 repo 기록에 보존.
  - 권한 완화(`c8be9694`) 및 고아 프로세스 취소(`439bc9af`), 좀비 Run(`2ea0d05a`)은 이미 `INFRA 441ae34d`의 통합/운영 검증 범위에 속해 있으므로 **신규 누락으로 계수하지 않음**.
  - 좀비 Run `2ea0d05a`의 프로세스 부재 및 lease 상태는 독립 감사에서 미입증(`미입증`)되었으므로 임의 단정하지 않음.
- **OpenCode / Copilot 작업 분리**:
  - Copilot 담당(`a56dce83`)이 원 세션(`ecc176d9` / Run `8cba5845`)의 Claude 독립 검증 요구를 복원 중이므로 중복 배정하지 않음.

---

## 5. GitHub 403 및 인증 환경

- **인증 상태 사실 한정**:
  - GitHub 403은 10/26 발생한 과거 사건이며, 최근 168시간 read-only 로그 0건은 현재 권한 상태의 정상/비정상을 입증하지 않음 (`미확인`).
  - 현재 미관측 상태를 영구적 불가로 단정하지 않고, 지원 인증 경로의 사전조건(개인 계정 토큰/키체인 브로커 연동, repo origin 구성)을 확인.
  - 전역 gh CLI 계정 강제 변경, 비밀/토큰 파일 직접 읽기, 회사 자격으로 개인 repo push 금지 규칙을 엄격히 준수.
- **기획 인계**:
  - 인증 경로가 확보될 때까지 개인 repo 직접 push를 시도하지 않으며, 승인된 격리 브랜치와 full SHA를 기획 세션에 보고/공유하여 후속 단계를 닫음.
