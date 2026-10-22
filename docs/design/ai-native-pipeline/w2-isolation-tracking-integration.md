# AI-Native 파이프라인 W2: 격리·추적·직렬 자동 통합 구현 보고서

> **2026-10-21 후속 검증 정정:** 아래 최초 Flash worker 보고는 구현 완료를 주장했으나, 부모 검토에서 운영 진입 경로에 무검증 통과 validator, merge-tree 결과 SHA 오사용, 실제 candidate 미생성, 공유 checkout 검사 등 차단 결함이 확인됐다. 아래의 “완료” 표기와 문서상 통합 조건은 유효한 완료 증거가 아니다. 후속 수정·검증 상태는 [`w4-w6-hardening-progress.md`](w4-w6-hardening-progress.md)를 기준으로 한다.

## 1. 개요 및 구현 범위
- **ADR 기준**: ADR 0066 (AI-Native 개발·검증·통합·배포 파이프라인) D1, D2, D3, D4, D5.
- **PMS 과제**: winterholic-ttakkari (e99a75de), WBS 23 (8c83d14d) > P7(23.7) 자동 통합 및 오류 복구.
  - W4 (23.7.2): 후속 보강 중 — opt-in 하네스 연결 및 실제 DB 통합 검증 대기
  - W5 (23.7.3): 첫 slice 후속 보강 중 — PostgreSQL 경로 및 독립 검증 미완료
  - W6 (23.7.4): 부분 구현 — 수리 예산·피드백 경로 보강, 실제 전체 루프 검증 미완료
- **금지사항 준수**:
  - runtime executor 및 generation ownership(세션 8bfac763 담당) 미변경
  - 추가 worker 생성 금지 준수
  - main 직접 push/배포 예약 금지 (부모 세션에 반환)
  - Kafka 등 신규 인프라 배제, 기존 PostgreSQL + Git + 하네스 기반 구현
  - 공유 체크아웃의 타 세션 변경(수집기/TTS) 오염 방지

---

## 2. 모듈별 구현 상세

### 2.1 하네스 관리 격리 Worktree (`src/ttakkari/delivery/worktree.py`)
- **선택적 격리 (`should_isolate_worker`)**:
  - 따까리 관리 Git 저장소에 실제 쓰기(write) 작업을 하는 worker에만 한정.
  - 비git 작업, 읽기 전용/조사 작업, 게임 작업(ADR 0058), 외부 저장소는 격리 제외.
- **Session별 Worktree 및 브랜치**:
  - 경로: `~/.ttakkari/worktrees/<session_id>`
  - 브랜치: `work/<session_id>` (base_ref: 최신 main HEAD)
  - `git config extensions.worktreeConfig true` 및 세션 식별값 분리.
- **Writable Roots 검증 (`validate_writable_roots`)**:
  - 샌드박스 쓰기 허용 목록(`extra_writable_roots`)에 대상 worktree 경로가 포함되도록 보장.
- **자산 손실 방지 및 안전한 수명주기 (`cleanup_session_worktree`)**:
  - 미커밋 dirty 파일이 있거나 아직 merge되지 않은 커밋이 존재할 경우 삭제 거부 (`preserved=True, removed=False`).
  - 오직 `status == merged` (또는 명시적 force) 상태에서만 worktree remove 및 브랜치 삭제.

### 2.2 Change Set Registry & Provenance (`src/ttakkari/delivery_models.py`, `src/ttakkari/delivery/provenance.py`)
- **독립 모델 `delivery_models.py`**:
  - 공용 `models.py` 충돌 방지.
  - `ChangeSet` 테이블: session_id, run_id, repo_path, branch_name, worktree_path, base_sha, head_sha, target_branch, status, stage1_result, stage2_result, diff_guard_result, failure_signature, repair_attempts, signature_repair_attempts, rejection_reason, conflict_files, is_dirty, submitted_at, merged_at.
- **커밋 출처 추적 (Provenance)**:
  - Git 트레일러 표준: `Ttakkari-Session: <session_id>`, `Ttakkari-Run: <run_id>`.
  - 커밋 메시지 파싱 및 출처 검증(`verify_commit_provenance`).

### 2.3 기계적 Diff Guard (`src/ttakkari/delivery/diff_guard.py`)
통합기가 변경 diff를 기계적으로 분석하여 다음 사항을 엄격히 거부 (`rejected`):
1. 테스트 함수/케이스 무단 삭제 (`no_test_deletion`)
2. pytest skip 또는 xfail 마커/호출 무단 추가 (`no_skip_or_xfail`)
3. timeout 값만 임의 상향 (`no_timeout_bump`)
4. 예외 삼키기 (`except: pass`, `except Exception: pass`) 무단 추가 (`no_swallowed_exceptions`)
5. 충분한 동작 변경 설명 없는 테스트 기대값(assert) 변경 (`no_unexplained_assertion_change`)

### 2.4 Bounded Repair Policy (`src/ttakkari/delivery/repair_policy.py`)
- **실패 서명 정규화**: `test_id::exception_type` (예: `tests/test_route.py::test_inventory::KeyError`).
- **엄격한 수리 상한**:
  - 동일 실패 서명당 최대 2회 (`MAX_REPAIRS_PER_SIGNATURE = 2`)
  - ChangeSet당 총 수리 시도 최대 3회 (`MAX_REPAIRS_PER_CHANGE = 3`)
- 상한 도달 시 `rejected`로 확정하고 원 세션에 구조화된 피드백을 전달하여 무한 루프 및 불필요한 토큰 낭비 방지.

### 2.5 직렬 통합 루프 (`src/ttakkari/delivery/merge_queue.py`)
- **점유 제어**: PostgreSQL `FOR UPDATE SKIP LOCKED` 기반 FIFO 선입선출 큐.
- **충돌 및 격리 분리**:
  - 최신 main에 대해 가상 merge/rebase 점검.
  - 충돌 발생 시: 사전문서에 정의되지 않은 일반 소스/테스트 충돌은 기계적 자동 해결을 금지하고 즉시 `conflict` 상태로 분리.
  - **정상 변경 보호**: 충돌/실패한 change는 큐에서 빠져나와 별도 격리되므로, 뒤따르는 다른 정상 변경들의 통합을 가로막지 않음.
- **단계별 검증 및 승격**:
  - Diff Guard -> Stage 1 (계약 묶음/lint/영향 테스트) -> Stage 2 (통합 필수 묶음)
  - 통과 시 main에 fast-forward 승격 (`git merge --ff-only`), worktree 안전 정리.

### 2.6 CLI 인터페이스 (`src/ttakkari/delivery/cli.py`)
- `status`: 현재 ChangeSet 목록 및 단계별 상태 출력
- `submit`: 특정 ChangeSet 제출
- `process-once`: 큐에서 1건 꺼내어 직렬 검증 및 병합 실행
- `inspect-provenance`: 커밋 SHA로부터 트레일러 출처 확인

### 2.7 Alembic 마이그레이션 (`alembic/versions/0062_delivery_change_sets.py`)
- 수집기(0060), TTS(0061) 작업과의 충돌을 피하기 위해 `0062_delivery_change_sets`로 준비.
- 신규 표 추가만 수행하므로 무중단 배포(online-safe) 요건 충족.

---

## 3. 최신 공식 1차 자료 기반 기술 선택 검토 보강

| 기술 영역 | 주요 메커니즘 및 1차 자료 근거 | 본 시스템(Ttakkari W2) 적용 및 차별점 |
|---|---|---|
| **Merge Queue** | **Bors / GitHub Merge Queue**: 임시 staging 브랜치에서 CI 통과 후 target branch로 fast-forward.<br>**GitLab Merge Trains**: Speculative / Optimistic pipelining (MR-A + MR-B 병렬 예측 실행) 및 실패 시 즉시 drop & restart. | 1차 구현에서는 복잡한 투기적 병렬(Speculative) 대신 **PostgreSQL FOR UPDATE SKIP LOCKED 기반의 결정적 직렬 큐**를 채택. 실패 시 즉시 `conflict` 또는 `rejected`로 격리하여 뒤따르는 변경을 막지 않는 GitLab 식 drop 격리 개념을 결합. |
| **Test Impact Analysis (TIA)** | 소스 코드 diff와 정적 import 의존 그래프(또는 커버리지 맵)를 분석하여 영향받는 테스트만 동적 선별 실행. | Stage 1/2의 빠른 피드백(2~5분)에만 TIA 및 계약 묶음을 선별 사용. **운영 배포 승격(Stage 3)은 불변 아카이브 전체 실행**을 강제하여 false pass를 절대 방지 (ADR 0066 원칙). |
| **Bounded Repair** | AI/Automated Program Repair(APR)에서 수정 루프의 탐색 공간과 재시도 횟수를 제한하여 비결정적 코드 파괴 및 리소스 고갈 방지. | 동일 실패 서명당 2회, ChangeSet당 3회의 **하드 리미트** 적용. 금지 diff(테스트 삭제, skip 추가 등)를 기계적으로 차단. |

---

## 4. 실행 환경 Denial 및 제약 사항 보고
- **범위**: `antigravity` 엔진의 `run_command` 작업자 샌드박스에서 Git 쓰기/DB 네트워크가 차단됐다고 보고됐다. 관측 범위는 해당 작업자 실행 환경이다.
- **미검증**: 원 보고서의 인라인 AST/정규식 검사로는 프로젝트 import, pytest, Git 통합, PostgreSQL 런타임을 입증하지 못한다. “100% Syntax & Logic” 문구는 취소한다.
- **후속 실행**: 부모 Codex 환경에서 feature worktree 생성 및 파일 쓰기가 성공했다. 그 별도 환경의 pytest/PostgreSQL 검증 상태는 새 진행 보고서에 명령·출력과 함께 기록한다.

---

## 5. 통합/운영 활성화 및 롤백 조건

### 5.1 운영 활성화 조건 (Opt-in 단계)
1. **마이그레이션 적용**: 수집기(0060) 및 TTS(0061) 머지 완료 후, `0062_delivery_change_sets`의 down_revision 확인 후 `alembic upgrade head` 실행.
2. **하네스 연결**: worker 생성 시 `should_isolate_worker`가 참인 경우 `create_session_worktree`를 호출하여 세션 workspace를 격리 worktree로 교체.
3. **통합기 상주 루프 기동**: 백엔드 프로세스와 분리된 독립 워커 루프 또는 cron/schedule로 `process_single_change` 실행.

### 5.2 롤백 조건 및 비상 경로
- **롤백 트리거**:
  - 통합기 큐 지연 발생 또는 DB 교착 상태 관측 시.
  - worktree 생성 실패율이 5%를 초과할 경우.
- **비상 경로 (ADR 0066 위험 관리)**:
  - 통합기를 거치지 않고 main에 직접 push하는 기존 경로를 유지.
  - `TTAKKARI_DELIVERY_ISOLATION_ENABLED=false` 플래그로 즉시 레거시 공유 작업공간 모드로 폴백 가능.

---

## 6. WBS 잔여 과제 및 선행 관계
1. **W1 (23.5) 배포 막힘 완화** [현재 세션 73e39523 진행 중]:
   - 계약 마커 묶음 및 누락 검사, 고유 Docker 리소스 hermetic 격리, 동시 pytest 제어.
2. **W2 (23.6.1) 게이트 신뢰성·속도**:
   - Stage 3 배포 게이트 -x 제거, 병렬 실행.
3. **W3 (23.7.1) Last-Green 승격과 원인 change 자동 revert**:
   - 선행: W1, W5.
   - Stage 3 통과 최신 SHA 추적 및 깨진 커밋 이분 탐색 자동 제외.
4. **W7 (23.6.2) Flaky 테스트 탐지 및 격리 정책**:
   - 비결정 테스트 자동 수집 및 이력 관리.
5. **W8 (23.6.3) TIA 규칙 고도화**:
   - import 그래프 및 정적 분석 기반 TIA 셀렉터 최적화.
