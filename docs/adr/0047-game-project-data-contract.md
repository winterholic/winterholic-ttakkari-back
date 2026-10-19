# ADR 0047: 게임 프로젝트 최소 데이터 계약과 게임 저장소 위치

- **상태**: Phase 2 구현 중 (2026-10-19). TASK-G02·G05 결정. Phaser 첫 어댑터(ADR 0046)와 같은 검증 조건을 따른다.
- **설계 정본**: [`docs/design/game-studio.md`](../design/game-studio.md) §4
- **구현**: `src/ttakkari/games/contract.py`, `alembic/versions/0047_game_studio.py`, 검증 `tests/test_game_contract.py`, `tests/test_game_migration.py`

## 결정

게임 하나는 기존 모델에 아래와 같이 걸치고, 기존 모델이 보존하지 못하는 항목만 새 표 3개에 둔다. 전용 작업 큐와 자체 버전관리는 만들지 않는다.

| 데이터 | 정본 | 비고 |
|---|---|---|
| 게임 소스와 버전 | 게임 저장소 자체 git (games_root/slug) | 따까리가 커밋 이력을 복제하지 않는다. 빌드는 source_commit만 기록한다. |
| 작업 디렉터리·접근 권한 | Workspace (게임 저장소 루트) | 기존 broker 정책(validate_workspace_root)을 그대로 통과해야 한다. |
| 프로젝트 관리·WBS | PmsProject (선택) | 없어도 게임은 동작한다(pms_project_id NULL 허용). |
| 개발·검증 작업 | 기존 ChatSession/Run | 빌드·피드백 처리는 run_id로 가리킨다. |
| 번들·스크린샷 파일 | Artifact | 번들 정본은 data_dir/game-builds/ 읽기 전용 사본이고 artifact_id는 그 연결이다. |
| 잠금 결정 목록 | 저장소의 ttakkari-game.json | 에이전트가 편집할 수 있는 파일이다. |
| 잠금 결정 승인본 | game_projects.locked_decisions_sha256 + locked_revision | 사용자가 승인한 해시만 DB에 고정한다. 빌드는 이 해시와 현재 매니페스트를 비교해 lock_verified를 기록한다. |
| 빌드 스냅샷 | game_builds | 빌드 ID, 게임별 seq, source commit/dirty, 매니페스트 해시, 번들 경로·해시·크기, 상태, 로그 꼬리. |
| 시각 피드백 연결 | game_feedbacks | 종류, 코멘트, 구조화 payload(JSONB), 스크린샷 Artifact, 처리 Run, 대상 빌드. |

### 새 표가 필요한 이유 (기존 모델로 보존되지 않는 사례)

- game_projects: Workspace에는 엔진 식별자와 잠금 승인 해시를 둘 곳이 없고, 사용자 승인본은 에이전트가 쓸 수 있는 저장소 파일 밖에 있어야 한다.
- game_builds: Run은 빌드 ID, 번들 해시, 소스 commit, 매니페스트 잠금 검증 결과를 구조화해 갖지 않는다. 롤백(G12)이 이전 빌드를 직접 가리켜야 한다.
- game_feedbacks: Artifact에는 빌드, 좌표 payload, 처리 Run 연결이 없다.

### 전용 큐·버전 표를 두지 않는 이유

작업 상태는 이미 Run이 가지며 중복 큐는 두 상태가 어긋나는 원인이 된다. 소스 이력은 게임 저장소 git이 정본이므로 따까리 쪽 revision 표는 같은 사실의 사본이다. 잠금 결정에도 변경 이력 표를 만들지 않고 locked_revision 카운터와 해시만 둔다. 변경 승인 이력이 필요해지면 그때 감사 로그(audit.record)를 쓴다.

## 게임 저장소 위치

config.games_root(기본 ~/development/projects/games) 아래 slug 디렉터리. 근거는 다음과 같다.

1. 기존 allowed_roots가 이미 ~/development를 포함하므로 새 허용 루트를 열지 않고 broker 정책 안에서 등록된다. game_dir()은 games_root가 allowed_roots 밖이면 거부한다.
2. slug는 소문자·숫자·하이픈 3~40자만 받아 경로 이탈을 문법 단계에서 막고, 등록은 validate_workspace_root 실경로가 예상 경로와 같을 때만 허용한다(심볼릭 링크 거부).
3. 게임 저장소는 따까리 저장소와 별개 git 저장소다. 따까리 코드 트리나 data_dir 안에 두지 않아 비밀·DB·다른 프로젝트와 섞이지 않는다.
4. 빌드 번들만 data_dir/game-builds/game_id/build_id/ 에 읽기 전용으로 복사한다. 재빌드가 이전 번들을 바꾸지 못하게 하기 위한 분리다.

## Migration (0047_game_studio)

- 신규 표 3개와 인덱스만 추가한다. 기존 표·컬럼은 건드리지 않으므로 이전 세대 코드가 그대로 동작하는 online-safe 변경이다. deploy/generation-contract.json의 schema_revision을 올리고 online_migration_from에 0046_notebooklm을 추가했다(커밋 1840b96 규칙).
- workspace_id는 ON DELETE RESTRICT로 게임이 있는 workspace의 삭제를 막고, 빌드·피드백은 게임 삭제 시 CASCADE, Run/Artifact 연결은 SET NULL이다.
- downgrade는 표 3개만 지우며 0046의 표 목록으로 정확히 돌아가는지 테스트한다. 게임 데이터가 있는 상태의 downgrade는 데이터 삭제를 뜻하므로 운영에서는 쓰지 않는다.

## 후속 변경

- 0048_game_active_build: `game_projects.active_build_id`(활성 빌드)와 `game_builds.restored_from_id`(복구 출처) 두 NULL 허용 컬럼을 추가했다. 근거와 규칙은 [ADR 0050](0050-game-worker-flow-and-rollback.md).

## 미확인

- 운영 DB에 대한 migrate 실행은 이 Run에서 하지 않았다(배포 절차에서 적용). 테스트 DB 기준 upgrade/downgrade만 확인했다.
