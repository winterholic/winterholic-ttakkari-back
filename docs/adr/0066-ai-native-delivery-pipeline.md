# ADR 0066: AI-Native 개발·검증·통합·배포 파이프라인(설계 초안)

## 상태
제안(2026-10-21). 1단계(현행 분석·설계·WBS) 산출물이다. 구현하지 않았다.
상세 근거·측정치·방법론 비교는 보고서
`ai-native-pipeline-phase1.html`(따까리 보관함)과 `docs/design/ai-native-pipeline/`에 있다.
ADR 0036(Run 실행기와 API 수명 분리)과는 별도 과제다. 이 ADR은 코드가 main에 들어와
배포 후보가 되기까지를 다루고, 0036은 배포 뒤 실행 중 작업의 수명을 다룬다.

## 배경(근거 요약)
측정 시각 2026-10-21 00시대 KST, main HEAD `052bc6c`.

- 작업 격리 없음: 모든 작업 세션이 같은 비 git 루트 `~/development`(workspaces 표
  `기본`, `is_git=false`)에서 시작한다. `spawn_worker`는 기획 세션의 workspace를 그대로
  물려준다(`api/orchestrator.py` spawn_worker). main 체크아웃에는 서로 다른 세 작업
  (수집 플랫폼, 지식 저장소 결함, 테스트 DB 격리)의 미커밋 변경이 동시에 있었다
  (`git status`, 파일 mtime 22:48~00:02). 에이전트가 손으로 만든 worktree가 57개다
  (`/tmp`, 형제 디렉터리, `~/testing` 혼재, 대부분 정리 안 됨).
- 세션↔커밋 연결 없음: 커밋 트레일러에 세션·Run id가 없다. Run 체크포인트
  (`runs.checkpoint_ref`)는 workspace가 git이 아니라서 최근 3일 2,204 Run 중 0건이다.
- 통합 = main 직접 push: 훅 없음. 10-16 이후 main 커밋 317개, 시간당 최대 26개.
  최근 3일 동시 실행 worker Run 최대 16개.
- 배포 게이트는 사후·직렬·누적이다. 예약 SHA는 main HEAD의 조상이어야 하고
  (`deployment.py` ensure_revision), 대기 중 예약만 새 예약으로 대체된다(`JobStore.request`).
  그래서 배포 대상은 항상 "그 시점까지의 main 전체"다. 깨진 커밋 하나가 뒤의 모든 변경을
  막는다. 10-19 22:17 UTC 성공 뒤 main 40커밋이 미배포 상태다. NotebookLM 수정 예약
  `41414724`는 대체된 뒤 무관한 Docker 잔여 네트워크 실패에 함께 막혔다.
- 게이트는 `pytest -q -x --timeout=120`을 알파벳 순서로 돌린다(`generation_deployment.py`
  run_pytest). 첫 실패에서 멈추므로 공존하는 다른 실패는 보이지 않는다. 실패하면 `--lf`로
  한 번만 다시 돌린다. 성공 실행 기준 2,917개, 863초(b1a3d162 로그).
  (2026-10-23 갱신: 첫 실행은 `--sw` stepwise로 바뀌었다. `docs/design/generation-deployment.md` 참조.)
- 게이트가 검증한 SHA와 배포 아티팩트는 일치한다. `git archive <sha>`로 만든 릴리스에서
  테스트하고, `verify_test_binding`이 릴리스 표식·tested_revision·계약 지문을 대조한다.
  이 부분은 유지한다.
- 최근 실패 원인 유형(건별 근거는 보고서 2절): 계약 미갱신 회귀(MCP 도구 카탈로그,
  세대 계약↔마이그레이션), 동작 변경 뒤 테스트 기대값 미갱신, 공유 호스트 자원 충돌
  (고정 Docker 네트워크 이름 `ttk-game-scripts1` 잔존), 시간 폭탄 테스트(고정 날짜),
  비결정 브라우저 E2E.
- 같은 날 배포 복구 작업이 `scripts/verify_deploy_gate.sh`(계약·카탈로그 검사, `--fast`,
  pre-push 훅 설치 옵션)와 잔여 격리 네트워크 정리(2af105f)를 넣었다. 그 작업의 독립 검증
  (8ad9d12b)은 failed였다. 지적 세 가지를 코드로 확인했다.
  - `--fast`는 손으로 고른 목록이라 `test_route_inventory`가 빠진다
    (`verify_deploy_gate.sh:53-62`). 052bc6c에서 이 테스트가 실패하는데도 `--fast`는 통과한다.
    바로 다음 예약 2af105f가 이 테스트로 실패했다(LOG-145).
  - `isolation.py:359-364`는 같은 이름 네트워크가 있으면 지우고 다시 만든다. 이름이 고정이라
    동시에 돌던 다른 실행의 네트워크를 지울 수 있다.
  - 검증 중 동시 pytest 28개가 관측됐다(이 ADR 측정 중에는 6개 이상, load 8~27).

## 결정
### D1. 격리: 하네스가 관리하는 작업별 worktree(코드 변경 작업에 한해)
- 따까리가 관리하는 git 저장소에 쓰기 작업을 하는 worker는 하네스가
  `~/.ttakkari/worktrees/<session>`에 `work/<session>` 브랜치 worktree를 만들어 준다.
  사용자는 브랜치·worktree를 만들거나 정리하지 않는다.
- 조사·검증(읽기 전용)·문서 외 저장소 작업은 강제하지 않는다. 검증 worker는 지정 SHA의
  분리(detached) worktree를 읽기 전용으로 받는다.
- 비용 실측: `git worktree add` 0.15초·13MB, `uv sync --frozen`(캐시 적중) 0.23초.
  57개 수동 worktree를 정리하는 비용이 이미 더 크다.
- 수명: 통합 완료 또는 세션 보관 시 하네스가 제거한다. 미통합 변경이 있으면 지우지 않고
  `abandoned`로 표시한다.

### D2. 추적: change set 표와 커밋 트레일러
- PostgreSQL에 `change_sets`(session_id, run_id, repo, base_sha, head_sha, status,
  단계별 결과, 실패 귀속)를 둔다. 새 인프라는 없다.
- worktree 설정(`extensions.worktreeConfig`)으로 커밋에 `Ttakkari-Session:` 트레일러를
  자동으로 붙인다.

### D3. 통합: PostgreSQL 기반 merge queue, main 쓰기는 통합기만
- worker는 main에 push하지 않고 `submit_change`(MCP)로 제출한다.
- 통합기는 큐에서 꺼내 최신 main 위로 rebase하고, 충돌 시 기계적 해결(마이그레이션 번호·
  계약 목록 같은 정해진 형식)만 자동으로 하고 나머지는 원 세션에 충돌 내용을 돌려준다.
- Stage 2를 통과한 것만 main에 fast-forward한다. 큐가 밀리면 여러 change를 묶어 한 번에
  검증하고(배치), 실패하면 이분 탐색으로 원인 change만 빼고 나머지를 통과시킨다.
- 통합기는 상태를 DB에만 두는 별도 상주 루프다. ADR 0036의 실행기와 같은 원칙(세대와
  무관, 재시작해도 DB에서 이어받음)을 따르고 배포 감독과 프로세스를 공유하지 않는다.

### D3a. 첫 구현(W1): 지금 막힘을 줄이는 세 가지
배포 감독 코드를 바꾸지 않고 먼저 넣는다.
1. 계약형 레지스트리 일관성 검사를 빠른 단계로 옮긴다. 도구 카탈로그, 라우트·MCP 인벤토리,
   세대 계약↔마이그레이션 head, 보안 게이트 인벤토리 테스트를 손 목록이 아니라 pytest 마커로
   묶고, 마커 누락을 검사한다. 052bc6c 실측으로 5개 파일 58개, 합계 12.1초다.
2. hermetic 자원 이름: 테스트의 Docker 자원 이름에 실행별 고유 접두사와 실행 id 라벨을 쓴다.
   정리는 자기 라벨 자원만 한다(남의 것을 지우는 재시도 금지).
3. 공용 머신의 동시 pytest 경합을 제어한다. 호스트 전역 입장 제어(PostgreSQL advisory lock
   또는 flock 세마포어)로 전체 실행·Docker·브라우저 묶음의 동시 수를 제한하고 배포 게이트에
   우선권을 준다. 빠른 단계(1)는 제한 밖에 둔다.

### D4. 4단계 검증
| 단계 | 어디서 | 내용 | 목표 시간 |
|---|---|---|---|
| 1 변경 직후 | 작업 worktree | ruff, 계약 묶음(도구 카탈로그·세대 계약·마이그레이션 단일 head), import 그래프 기반 영향 테스트 | 2분 이내 |
| 2 통합 | 통합기 | rebase 결과에서 영향 테스트 + 고위험 영역 필수 묶음(보안·인증·마이그레이션·데이터 무결성) | 5분 이내 |
| 3 배포 후보 | 기존 배포 게이트 | 불변 SHA 아카이브에서 전체 테스트(선택 실행으로 대체 금지) + 보안 게이트 | 현행 유지, 병렬화로 단축 |
| 4 운영 반영 | 기존 세대 배포 | readiness, 전환, drain, 롤백 + 배포 후 스모크와 오류 이슈 감시 | 현행 |

- 배포 대상은 "main HEAD"가 아니라 "Stage 3을 통과한 가장 최근 main SHA(last green)"다.
  Stage 3 실패는 직전 green 이후 change들 사이에서 원인을 찾아 되돌리기(revert) change로
  빼고, 나머지로 다시 후보를 만든다.
- 선택 실행(TIA)은 Stage 1·2의 빠른 피드백에만 쓴다. 운영 배포 승격은 항상 전체 실행이다.

### D5. 자율 수정 루프와 상한
- 실패 → 로그·변경 수집 → 유형 분류(회귀/계약 미갱신/테스트 결함/환경·공유 자원/비결정)
  → 원인 change와 세션 귀속 → 그 세션(없으면 새 worker)에 수정 배정 → Stage 1·2 재검증.
- 상한: 같은 실패 서명(테스트 id + 예외 유형)당 자동 수정 2회, change당 총 3회, worker
  토큰 예산 상한. 넘으면 change를 큐에서 빼고 사용자에게 알린다.
- 금지(통합기가 diff로 기계 검사): 테스트 삭제·skip/xfail 추가·timeout 상향만으로 통과,
  근거 없는 기대값 변경(같은 change에 동작 변경 설명이 없으면 거부), 예외 삼키기.
- 수정 완료 판정은 재현 가능한 테스트 결과(같은 명령 재실행 출력)로만 한다.

### D6. 비결정 테스트
- 같은 SHA 반복 실행과 배포 로그 이력으로 후보를 찾고, 원인(시간·동시성·공유 자원·순서)을
  고친다. 삭제·자동 무시는 하지 않는다. 격리 목록에 오른 테스트도 Stage 3에서는 계속 돌리고
  결과를 기록하되, 격리 기간·담당·종료 조건을 둔다(사용자 승인 없이 게이트 제외 안 함).

### D7. 지표
`change_sets`와 기존 배포 job·ops 이슈에서 계산한다. 기준값은 보고서 3절.

## 채택하지 않거나 보류한 것
- Kafka·별도 메시지 큐: 문제는 메시지 전달이 아니라 격리·통합 순서·게이트 정책이다.
  PostgreSQL 잠금(`FOR UPDATE SKIP LOCKED`)으로 충분하다.
- 커버리지 기반 TIA: 커버리지 수집이 없고 수집 비용(전체 실행 1회 이상 + 유지)이 크다.
  1차는 import 그래프 + 계약 규칙, 커버리지 맵은 측정 뒤 결정.
- 전체 실행 결과 캐시를 배포 판정에 쓰는 것: 같은 트리 해시에서만 허용하는 안을 검증 뒤
  결정(문서 전용 커밋이 22%다). 부분 캐시는 Stage 1·2에만.
- Mutation testing: 비용 대비 지금 문제와 무관. 보류.

## 위험과 롤백
- 통합기 장애 시 main이 멈춘다 → 기존처럼 직접 push 경로를 비상 스위치로 남긴다.
- worktree 강제가 엔진 샌드박스 쓰기 경로와 충돌할 수 있다 → `extra_writable_roots`
  자동 추가와 실측 후 단계 적용.
- 배포 게이트 변경(-x 제거, last-green 승격, 병렬화)은 진행 중인 배포 복구 작업과 겹치므로
  그 작업 결과를 받은 뒤 별도 change로 한다. 기존 복구 경로(rollback, recovery_pending)는
  유지한다.
