# ADR 0050: 게임 worker 연결, 잠금 결정 강제, 활성 빌드와 롤백 (TASK-G08, G12 백엔드)

- **상태**: G08 구현됨, G12 백엔드(롤백 API) 구현됨. G12 프론트(비교·롤백 UI)는 미구현 (2026-10-19)
- **설계 정본**: [`docs/design/game-studio.md`](../design/game-studio.md) §5.1, §6.1
- **관련**: ADR 0047(데이터 계약), ADR 0049(게임 origin)

## G08: 개발·수정·검증 요청은 기존 세션·Run으로 넘긴다

전용 큐를 만들지 않는다. 게임 요청 1건은 게임 workspace의 `ChatSession(kind=worker, engine=auto)`와 그 Run 하나다. 모델·계정 선택, 한도 대기, 결과 보고는 기존 `queue_run`·`worker_routing`·`RunManager` 그대로다.

| 경로 | 호출자 | 동작 |
|---|---|---|
| `POST /api/games/{id}/requests` | 인증된 사용자(PWA) | `kind`(develop·modify·verify)와 요청 문장. worker 세션과 Run을 만들고 `{session_id, run_id, ...}`를 돌려준다. |
| `GET /api/games/{id}/requests` | 사용자 | `Run.usage.game.game_id`로 이 게임의 요청 Run만 나열한다(별도 표 없음). |
| MCP `spawn_worker(game_id=, game_build_id=)` | 오케스트레이터 | worker가 게임 workspace에서 돌고 prompt 앞에 프로젝트·빌드·잠금 결정이 붙는다. 개인 모드 오케스트레이터만 가능(회사 범위 403). |

- 상태 읽기: `GET /api/sessions/{id}`, `GET /api/runs/{run_id}`, MCP `get_worker`(기존 도구).
- TaskType은 기존 값만 쓴다: develop·modify → `general`, verify → `verification`(독립 검토이며 소스를 고치지 않는다고 prompt에 명시).
- prompt는 self-contained다(`games/worker.py`): 게임 slug·엔진·저장소 경로, 대상 빌드(seq·commit·번들 해시 앞 16자·잠금 검증 여부), 잠금 결정, 금지 사항(호스트에서 npm 실행 금지, 매니페스트 id/engine/locked_decisions 변경 금지, 다른 경로 접근 금지), 사용자 요청.
- Run에는 `usage.game = {game_id, kind, build_id, lock}` 표식을 남겨 요청 목록·후속 피드백 연결(G11)의 근거로 쓴다.

### 잠금 결정은 prompt가 아니라 승인 해시가 강제한다

- 에이전트는 저장소의 `ttakkari-game.json`을 고칠 수 있으므로 prompt 문구는 권고일 뿐이다. 사용자가 승인한 결정의 sha256(`game_projects.locked_decisions_sha256`)과 현재 매니페스트가 다르면 `builds.prepare_build`가 `lock_violation`(409)으로 거부하고 빌드 행도 만들지 않는다. 실행 직전(`execute_build`)에도 다시 검사해 prepare 이후의 변경은 failed 빌드로 닫는다. 아직 승인한 적이 없으면 막을 대상이 없다(`lock_verified=false`로만 기록).
- 변조 상태에서도 요청 접수는 허용한다. prompt에 경고와 복구 방법(`git checkout -- ttakkari-game.json`)을 넣어 에이전트가 되돌릴 수 있게 해야 교착되지 않는다. 되돌리기 전에는 빌드와 적용(활성화·롤백)이 거부된다.
- 승인본을 바꾸는 유일한 경로는 사용자 인증이 필요한 `POST /api/games/{id}/lock/approve`다. 에이전트 토큰은 이 라우트에 닿지 않는다.

### 이 단위가 하지 않는 것

- worker 완료 후 자동 빌드 트리거: 빌드는 아직 사용자/관제탑이 `POST /api/games/{id}/builds`로 시작한다. Run 완료 → 빌드 연결은 G11·G14에서 한다.
- 시각 피드백(GameFeedback) 저장과 요청 연결: G11.

## G12 백엔드: 활성 빌드와 롤백 API

### 데이터 (migration 0048_game_active_build, 추가형)

| 컬럼 | 용도 |
|---|---|
| `game_projects.active_build_id` | 관제탑이 여는 빌드. NULL이면 가장 최근 성공 빌드. `game_builds`와 순환 참조라 FK를 걸지 않고 읽을 때 존재·상태를 확인한다. |
| `game_builds.restored_from_id` | 소스 복구로 만든 빌드의 복구 대상(자기 참조 FK, 대상이 지워져도 SET NULL). |

두 컬럼 모두 NULL 허용이라 이전 세대 코드와 공존한다(`deploy/generation-contract.json`의 schema_revision과 online_migration_from 갱신). 새 표·전용 버전 큐는 만들지 않는다. 소스 이력은 게임 저장소 git이 정본이다.

### 활성 빌드 규칙

- 성공한 새 빌드는 활성 빌드가 비어 있거나 직전 최신 성공 빌드를 따라가던 중이면 활성이 된다. 사용자가 롤백으로 오래된 빌드에 고정했으면 이후 새 빌드(에이전트가 만든 것 포함)가 고정을 덮지 않는다. 롤백으로 만든 복구 빌드는 항상 활성이 된다.
- `POST /api/games/{id}/play`의 기본 대상은 활성 빌드다.

### `POST /api/games/{id}/rollback` (`build_id`, `restore_source=false`, `confirm=true`)

- `confirm: true`가 없으면 422다. 되돌리기 어려운 동작이라 UI 최종 확인 대화상자의 결과를 명시로 요구한다. 같은 호출로 더 새로운 빌드로의 이동도 가능하다.
- 대상 검증(`builds.check_activatable`): 이 게임의 성공 빌드, 번들 해시 재계산 일치(`snapshot_tampered`), 승인된 잠금 결정과 일치(`lock_violation`). 잠금은 빌드의 source commit 시점 매니페스트(git 객체)로 다시 판정하므로, 이후 사용자가 결정을 새로 승인했다면 옛 빌드는 거부된다. commit이 없거나 더러운 트리로 만든 빌드는 빌드 당시의 `lock_verified`만 믿는다(한계).
- **포인터 롤백(기본):** 활성 포인터만 옮긴다. 소스·번들·zip Artifact·권한은 건드리지 않는다(테스트가 롤백 전후 파일별 sha256, 모드, Artifact sha256, commit을 비교한다). 작업 트리가 더러워도 가능하다.
- **소스 복구(`restore_source=true`):** 대상 commit의 트리를 현재 HEAD의 자식으로 하는 새 복구 커밋을 `git commit-tree`로 만들고 `git merge --ff-only`로만 반영한다. 기존 커밋·브랜치 이력은 지우지 않고, 공유 작업 트리에 checkout을 직접 하지 않는다. 작업 트리에 커밋되지 않은 변경이 있으면 `dirty_worktree`, 진행 중인 빌드가 있으면 `build_in_progress`, 더러운 트리로 만든 빌드이거나 commit이 없으면 `not_restorable`로 거부하고 아무것도 바꾸지 않는다. 소스가 이미 같으면 새 커밋 없이 현재 HEAD를 다시 빌드한다. 이후 복구 빌드(`restored_from_id`)를 백그라운드로 시작하고 성공하면 활성이 된다. 응답은 대상 빌드를 즉시 활성으로 두고 복구 빌드를 `building` 상태로 돌려준다.
- 모든 거부·성공은 audit에 `game.rollback`으로 남는다.

### 알려진 한계

- 활성 포인터 이동과 git 복구는 한 트랜잭션이 아니다. 복구 커밋 이후 빌드 행 생성이 실패하면 소스만 복구된 채 오류가 나간다(다시 호출하면 소스가 이미 같은 경우로 처리된다).
- 에이전트가 승인 없이 바꾼 잠금 결정이 이미 커밋된 상태에서는 작업 트리가 깨끗하므로 `restore_source`가 승인된 빌드의 소스(매니페스트 포함)로 복구하는 경로가 된다(`test_committed_tampering_is_repaired_by_restoring_an_approved_build`).
- 프론트의 전후 비교·롤백 확인 UI는 이 ADR 범위가 아니다.
